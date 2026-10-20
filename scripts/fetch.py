"""Garmin Connect 中国区 · 带节流的抓取（v2：走浏览器内 fetch）

【v1 为什么失败 —— 2026-10-20 实测，别改回去】
v1 用 Playwright 的 `context.request.get()` 打 /gc-api：
    connect.garmin.cn  → 403（Cloudflare）
    connectus.garmin.cn → 401
但 sniff.py 抓真实流量发现，同一个 /gc-api 在浏览器里 **94 个 200**。
差别不在认证，在**请求指纹**：context.request 是 API 层，不带
sec-ch-ua / Sec-Fetch-* 这些只有页面发请求才有的 header，Cloudflare 一眼就看穿。

[ok] v2 的做法：先在页面里落地，再用页面内的 fetch() 发请求。
   请求由 Chromium 自己发出，指纹是全的。

【成本控制】
- 整页加载 **只有 1 次**（打开 /app/home），并且拦掉图片/字体/媒体
- API 请求之间强制 sleep 3~6 秒
- 单次运行最多 MAX_REQ_PER_RUN 个 API 请求
- [!] 任何非 200 立刻退出，绝不重试

【跑法】
    python fetch.py --dry                    # 只看计划，零网络
    python fetch.py --probe                  # 1 个请求，验证通路
    python fetch.py --only hr_zones,devices
    python fetch.py --level 1
"""

import sys

# [!] Windows 控制台默认 GBK，打印 [ok] [x] [!] 这类符号会抛 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import argparse
import json
import os
import random
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from endpoints import ENDPOINTS

# ── 参数 ────────────────────────────────────────────────────────
BASE = "https://connect.garmin.cn"          # sniff 实测：380/380 XHR 全打这个域
APP_URL = BASE + "/app/home"
DELAY_MIN_S = 3.0
DELAY_MAX_S = 6.0
MAX_REQ_PER_RUN = 6                         # 单次运行 API 请求上限
PAGE_TIMEOUT_MS = 90_000
# 调试端口。可用环境变量 GARMIN_CDP_PORT 覆盖（默认 9222，与 start-edge-debug.cmd 一致）
CDP_PORT = int(os.environ.get("GARMIN_CDP_PORT", "9222") or 9222)
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

# --morning：每天早上出课表只需要这 4 个。
# 心率分区 / 乳酸阈值变化很慢，不必每天抓（每周或数据变动时补一次即可）。
MORNING_KEYS = ["sleep", "heart_rate", "hrv_daily", "daily_summary"]

HERE = Path(__file__).parent
STATE_FILE = HERE / "storage_state.json"
RAW_DIR = HERE / "raw"
LOG_FILE = RAW_DIR / "_fetch_log.jsonl"


def _cleanup(mode: str, own_context, own_browser, temp_page) -> None:
    """按模式收拾摊子。CDP 模式下只关自己开的标签，绝不关他的浏览器。"""
    try:
        if mode == "cdp":
            if temp_page is not None:
                temp_page.close()
            if own_browser is not None:
                own_browser.close()     # 只是断开 CDP 连接，不会杀 Edge
        else:
            if own_context is not None:
                own_context.close()
            if own_browser is not None:
                own_browser.close()
    except Exception:
        pass


# [!] 核心（2026-10-20 破案）：手写 fetch 只带 Accept 会被 Cloudflare 403。
# 必须抄下应用自己请求的 header —— 关键那个是 Connect-Csrf-Token。
# 这段注入到页面加载之前，hook fetch / XHR，把 /gc-api 请求的 header
# 收集到 window.__ghdr（全程在浏览器内存里，不落盘、不回显值）。
CAPTURE_JS = r"""
window.__ghdr = {};
(function () {
    function merge(h) {
        if (!h) return;
        try {
            if (typeof Headers !== 'undefined' && h instanceof Headers) {
                h.forEach(function (v, k) { window.__ghdr[k] = v; });
            } else if (Array.isArray(h)) {
                h.forEach(function (kv) { window.__ghdr[kv[0]] = kv[1]; });
            } else if (typeof h === 'object') {
                Object.keys(h).forEach(function (k) { window.__ghdr[k] = h[k]; });
            }
        } catch (e) {}
    }
    var of = window.fetch;
    window.fetch = function (input, init) {
        try {
            var url = (typeof input === 'string') ? input : (input && input.url) || '';
            if (url.indexOf('/gc-api/') >= 0 && init && init.headers) merge(init.headers);
        } catch (e) {}
        return of.apply(this, arguments);
    };
    var oo = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (m, u) {
        try { this.__gapi = (u && u.indexOf('/gc-api/') >= 0); } catch (e) {}
        return oo.apply(this, arguments);
    };
    var os = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
        try { if (this.__gapi) window.__ghdr[k] = v; } catch (e) {}
        return os.apply(this, arguments);
    };
})();
"""

# 页面内执行：用浏览器自己的 fetch 发请求，带上抄来的 header
FETCH_JS = """
async ([path, hdrs]) => {
    const h = Object.assign({}, hdrs || {});
    delete h['content-length'];
    delete h['Content-Length'];
    if (!h['Accept']) h['Accept'] = 'application/json, text/plain, */*';
    const r = await fetch(path, { credentials: 'include', headers: h });
    const txt = await r.text();
    let js = null;
    try { js = JSON.parse(txt); } catch (e) { /* 非 JSON 就留原文 */ }
    return { status: r.status, len: txt.length, json: js,
             text: (js === null ? txt.slice(0, 3000) : null) };
}
"""


def capture_headers(page, timeout_s: float = 20.0) -> dict:
    """等应用自己发出 /gc-api 请求，把它用的 header 抄下来。

    判据是出现 Connect-Csrf-Token —— 那是 Cloudflare 放行 /gc-api 的关键。
    """
    import time as _t
    deadline = _t.time() + timeout_s
    last = {}
    while _t.time() < deadline:
        try:
            hdrs = page.evaluate("window.__ghdr || {}")
        except Exception:
            hdrs = {}
        if hdrs:
            last = hdrs
            if any(k.lower() == "connect-csrf-token" for k in hdrs):
                return hdrs
        _t.sleep(1.0)
    return last



# ── 小工具 ─────────────────────────────────────────────────────

def die(msg: str, code: int = 1):
    print(f"\n[[x]] {msg}")
    sys.exit(code)


def deep_find(obj, keys, depth=0):
    """在嵌套结构里找第一个命中的字段值。Garmin 的字段路径不稳定，别写死。

    数字也接受（userProfilePk 是 int，早期只认 str 导致漏取）。
    """
    if depth > 8:
        return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                return str(v)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in obj.values():
            got = deep_find(v, keys, depth + 1)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = deep_find(v, keys, depth + 1)
            if got:
                return got
    return None



def fill(template: str, ctx: dict) -> str:
    out = template
    for key in ("start_date", "end_date", "date", "guid", "activity_id"):
        if "{" + key + "}" in out:
            if not ctx.get(key):
                die(f"端点需要 {key}，但没给")
            out = out.replace("{" + key + "}", str(ctx[key]))
    return out


def log_line(rec: dict) -> None:
    RAW_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class Budget:
    def __init__(self, cap: int):
        self.cap = cap
        self.used = 0

    def take(self) -> bool:
        if self.used >= self.cap:
            print(f"[…] 已达单次上限 {self.cap} 个请求，停。")
            return False
        if self.used > 0:
            nap = random.uniform(DELAY_MIN_S, DELAY_MAX_S)
            print(f"    … 等 {nap:.1f}s")
            time.sleep(nap)
        self.used += 1
        return True


# ── 主流程 ─────────────────────────────────────────────────────

def build_jobs(args) -> list:
    today = datetime.now().strftime("%Y-%m-%d")
    ctx = {
        "date": args.date or today,
        "start_date": args.start or args.date or today,
        "end_date": args.end or args.date or today,
        "guid": "",
        "activity_id": getattr(args, "activity", "") or "",
    }
    if args.probe:
        keys = ["user_profile_base"]
    elif args.morning:
        keys = list(MORNING_KEYS)
    elif args.only:
        keys = [k.strip() for k in args.only.split(",") if k.strip()]
        for k in keys:
            if k not in ENDPOINTS:
                die(f"未知端点 {k!r}。可用：{', '.join(sorted(ENDPOINTS))}")
    else:
        keys = [k for k, _ in ENDPOINTS.items() if ENDPOINTS[k]["level"] <= args.level]
        keys.sort()

    jobs = []
    for k in keys:
        spec = ENDPOINTS[k]
        if spec["kind"] != "rest":
            print(f"[skip] {k}: v2 只支持 rest")
            continue
        jobs.append({"key": k, "path": spec["path"], "desc": spec["desc"]})
    return jobs, ctx


def cmd_dry(jobs, ctx) -> int:
    print("=" * 70)
    print(" --dry · 计划预览（一个请求都不会发）")
    print("=" * 70)
    print(f" 域：{BASE}")
    print(f" 整页加载：1 次（{APP_URL}，拦掉图片/字体/媒体）")
    print(f" API 请求间隔：{DELAY_MIN_S}~{DELAY_MAX_S}s 随机")
    print(f" 单次上限：{MAX_REQ_PER_RUN} 个 API 请求")
    print(" 登录态：默认 CDP 模式（复用你已登录 Edge，不需要登录态文件）"
          + ("；检测到 storage_state.json 存在" if STATE_FILE.exists() else ""))
    print()
    print(f" 本次计划 {len(jobs)} 个端点：")
    for i, j in enumerate(jobs, 1):
        print(f"   {i:2}. {j['key']:<22} {j['path']}")
        print(f"       └ {j['desc']}")
    print("=" * 70)
    return 0


def _port_open(port: int = 0, timeout: float = 1.0) -> bool:
    # 不用 `int | None`：本文件没有 `from __future__ import annotations`，
    # 那种写法在 Python 3.9 上 import 阶段就会 TypeError。
    port = port or CDP_PORT
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _msedge_exe() -> str:
    import os
    cands = [
        os.path.join(os.environ.get("ProgramFiles(x86)", ""),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for c in cands:
        if c and Path(c).exists():
            return c
    return ""


def ensure_edge_debug(wait_s: float = 25.0) -> str:
    """确保 9222 调试端口可用。你不用关心 Edge 是怎么起来的。

    返回：
        "reuse"   端口已通，直接用
        "launched" 之前没有 Edge，本脚本自己拉起来的
        "blocked"  Edge 在跑但没开调试端口（只能请你关一次）
    """
    import subprocess

    if _port_open():
        return "reuse"

    if _edge_running():
        # Edge 已经开着但没有调试端口：flag 只在全新启动时生效，救不回来
        return "blocked"

    exe = _msedge_exe()
    if not exe:
        return "blocked"

    print("[i] 没有 Edge 在跑 → 本脚本自己拉起一个带调试端口的")
    try:
        subprocess.Popen(
            [exe, f"--remote-debugging-port={CDP_PORT}",
             f"--user-data-dir={_edge_user_data()}",
             "--profile-directory=Default",
             "--no-first-run", "--disable-session-crashed-bubble"],
            close_fds=True,
        )
    except Exception as exc:
        print(f"[!] 启动 Edge 失败：{exc}")
        return "blocked"

    waited = 0.0
    while waited < wait_s:
        time.sleep(0.5)
        waited += 0.5
        if _port_open():
            print(f"[i] 端口就绪（等了 {waited:.1f}s）")
            return "launched"
    print(f"[!] 等了 {wait_s:.0f}s 端口仍未就绪")
    return "blocked"


def _edge_running() -> bool:
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/NH"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return False
    return "msedge.exe" in out.lower()


def _edge_user_data() -> Path:
    """Edge 的 User Data 目录。Windows / macOS / Linux 各一条（mac/Linux 未实测）。

    可用 GARMIN_USER_DATA 直接指定（换了浏览器或自定义 profile 时用）。
    """
    import os
    env = (os.environ.get("GARMIN_USER_DATA") or "").strip()
    if env:
        return Path(env)
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Microsoft Edge"
    return Path.home() / ".config" / "microsoft-edge"


def run(jobs, ctx, budget_cap: int, mode: str) -> int:
    """mode:
        "storage" → storage_state + 干净实例（[warn] CF 当无痕拦，403）
        "real"    → 真实 Edge profile 启动（Edge 必须先关） 实测可用
        "cdp"     → 连到已开的调试端口 Edge（不用关） 最省事
    """
    RAW_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    budget = Budget(budget_cap)
    real_profile = mode in ("real", "cdp")

    if mode == "real" and _edge_running():
        print("[!] Edge 还在运行，真实 profile 被占用。")
        print("    → 关掉所有 Edge 窗口后重跑；或改用 --cdp（见 start-edge-debug.cmd）")
        return 6

    with sync_playwright() as p:
        own_browser = None
        own_context = None
        temp_page = None

        if mode == "cdp":
            # 连到他自己的 Edge：共享真实 profile + 真实指纹。
            # 绝不在这个 context 上挂 route（会拦住他正常浏览的图片）
            state = ensure_edge_debug()
            if state == "blocked":
                print("[!] 拿不到可用的调试端口。三种原因，按顺序自查：")
                print("    ①【最常见】Edge 已经开着，但启动时没带 --remote-debugging-port。")
                print("       调试开关只在进程**全新启动**时生效 → 关掉所有 Edge 窗口（Ctrl+Shift+W）后重跑。")
                print("       [warn] 若 Edge 开着「启动增强/后台运行」，关窗口后进程仍在：")
                print("          到任务管理器结束所有 msedge.exe，或直接跑仓库根的 start-edge-debug.cmd。")
                print("    ② 没找到 msedge.exe（非 Windows，或 Edge 不在标准安装路径）")
                print("       → 用 --real-profile 模式，或自己先把 9222 端口起起来。")
                print("    ③ 端口被别的程序占用（doctor.py 只会报『已在监听』）")
                print("       → 换个端口：$env:GARMIN_CDP_PORT=9333，并同步用该端口启动 Edge。")
                return 6
            try:
                own_browser = p.chromium.connect_over_cdp(CDP_URL)
            except Exception as exc:
                print(f"[!] 连不上调试端口 {CDP_URL}")
                print(f"    {type(exc).__name__}: {exc}")
                print("    → 关掉所有 Edge 窗口后重跑本命令。")
                return 6
            if not own_browser.contexts:
                print("[!] 连上了但没有可用 context。")
                return 6
            context = own_browser.contexts[0]
            context.add_init_script(CAPTURE_JS)     # 必须在 new_page 之前注入
            temp_page = context.new_page()      # 只开一个临时标签，不碰他的标签
            page = temp_page
        elif mode == "real":
            own_context = p.chromium.launch_persistent_context(
                user_data_dir=str(_edge_user_data()),
                channel="msedge",
                headless=False,          # [!] 必须 headed：headless 也是 bot 信号
                locale="zh-CN",
                no_viewport=True,
                args=["--no-first-run", "--disable-session-crashed-bubble",
                      "--disable-restore-session-state"],
            )
            context = own_context
            context.add_init_script(CAPTURE_JS)
            page = context.pages[0] if context.pages else context.new_page()
        else:
            own_browser = p.chromium.launch(
                channel="msedge",
                headless=False,
                ignore_default_args=["--enable-automation"],
                args=["--disable-blink-features=AutomationControlled",
                      "--no-first-run"],
            )
            own_context = own_browser.new_context(
                storage_state=str(STATE_FILE), locale="zh-CN",
                no_viewport=True)
            context = own_context
            context.add_init_script(CAPTURE_JS)
            page = context.new_page()

        # 拦掉大件，只留文档 + 脚本。[warn] cdp 模式下不拦（那是他的浏览器）
        if mode != "cdp":
            blocked = {"image", "media", "font"}
            context.route("**/*", lambda route: (
                route.abort() if route.request.resource_type in blocked else route.continue_()
            ))

        label = {"storage": "storage_state（无痕上下文，实测 CF 会 403）",
                 "real": "真实 Edge profile（headed）",
                 "cdp": f"CDP 接管已开 Edge（{CDP_URL}）"}[mode]
        print(f"[i] 打开 {APP_URL}（唯一一次整页加载）")
        print(f"    模式：{label}")
        try:
            page.goto(APP_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        except Exception as exc:
            print(f"[!] 页面加载异常：{type(exc).__name__}: {exc}")
            _cleanup(mode, own_context, own_browser, temp_page)
            return 2

        if "signin" in page.url.lower() or "sso" in page.url.lower():
            print(f"[[x]] 被弹回登录页（{page.url}）→ 登录态失效")
            if real_profile:
                print("    在弹出的窗口里手动登录一次，然后重跑。")
                time.sleep(20)
            _cleanup(mode, own_context, own_browser, temp_page)
            return 4
        print(f"[i] 已落地：{page.url}")

        # ── 抄应用自己的请求头（关键：Connect-Csrf-Token）──────
        print("[i] 等应用发出自己的 /gc-api 请求，抄 header…")
        hdrs = capture_headers(page)
        if not hdrs:
            print("[!] 一个 header 都没抄到 —— 应用可能没走 /gc-api，"
                  "或 hook 没生效。只用 Accept 硬试一次。")
        else:
            keys = sorted(hdrs)
            print(f"[✓] 抄到 {len(keys)} 个 header：{', '.join(keys)}")
            if not any(k.lower() == "connect-csrf-token" for k in keys):
                print("[!] 没抄到 Connect-Csrf-Token —— 大概率还会 403。")

        # ── 引导：拿 guid ──────────────────────────────────
        boot = {"kind": "rest", "path": ENDPOINTS["user_profile_base"]["path"]}
        if not budget.take():
            _cleanup(mode, own_context, own_browser, temp_page)
            return 0
        print(f"[{budget.used}/{budget.cap}] GET user_profile_base（引导）")
        try:
            res = page.evaluate(FETCH_JS, [boot["path"], hdrs])
        except Exception as exc:
            print(f"[[x]] page.evaluate 失败：{type(exc).__name__}: {exc}")
            _cleanup(mode, own_context, own_browser, temp_page)
            return 2
        print(f"     HTTP {res['status']}  {res['len']} 字节")
        if res["status"] != 200:
            print(f"\n[!] 引导端点非 200（{res['status']}）→ 停手，不重试。")
            _cleanup(mode, own_context, own_browser, temp_page)
            return 3

        # Garmin 的 URL 里那个「GUID」其实就是 displayName 字段的值
        # （实测: displayName 是 UUID 形态，userName 反而是手机号）。
        # 所以优先取 displayName，取不到再退回其它键。
        guid = (deep_find(res["json"], ("displayName", "userGuid", "guid", "userProfileId"))
                or deep_find(res["json"], ("userProfilePk",)))
        if guid:
            ctx["guid"] = guid
            print(f"     ↳ guid = {guid[:8]}…（已填进路径占位符）")
        else:
            print("     [warn] 没取到 guid —— 需要 guid 的端点会被跳过")


        (RAW_DIR / f"user_profile_base_{stamp}.json").write_text(
            json.dumps(res["json"], ensure_ascii=False, indent=2), encoding="utf-8")
        log_line({"ts": stamp, "key": "user_profile_base",
                  "status": res["status"], "len": res["len"]})

        # ── 正式端点 ──────────────────────────────────────
        for job in jobs:
            if job["key"] == "user_profile_base":
                continue
            try:
                path = fill(job["path"], ctx)
            except SystemExit:
                print(f"[skip] {job['key']}: 占位符没填上")
                continue
            if not budget.take():
                break

            print(f"[{budget.used}/{budget.cap}] GET {job['key']}")
            try:
                res = page.evaluate(FETCH_JS, [path, hdrs])
            except Exception as exc:
                print(f"[[x]] 请求异常：{type(exc).__name__}: {exc}")
                log_line({"ts": stamp, "key": job["key"], "path": path,
                          "status": None, "error": str(exc)})
                _cleanup(mode, own_context, own_browser, temp_page)
                return 2

            print(f"     HTTP {res['status']}  {res['len']} 字节")
            out = RAW_DIR / f"{job['key']}_{stamp}.json"
            if res["json"] is not None:
                out.write_text(json.dumps(res["json"], ensure_ascii=False, indent=2),
                               encoding="utf-8")
            else:
                out.write_text(res["text"] or "", encoding="utf-8")
            log_line({"ts": stamp, "key": job["key"], "path": path,
                      "status": res["status"], "len": res["len"]})

            if res["status"] != 200:
                print(f"\n[!] 非 200（{res['status']}）→ 立即停手，不重试。")
                if res["status"] == 404:
                    print("   404 = 路径不对。这个端点需要按真实流量再校正（跑 sniff.py）。")
                elif res["status"] in (401, 403):
                    print("   401/403 = 登录态或指纹问题：确认 Edge 里已登录 connect.garmin.cn，"
                          "然后重跑（别连试）。")
                _cleanup(mode, own_context, own_browser, temp_page)
                return 3

        _cleanup(mode, own_context, own_browser, temp_page)

    print(f"\n[✓] 完成，共发 {budget.used} 个 API 请求（+1 次整页加载）。")
    print(f"    落盘：{RAW_DIR.resolve()}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Garmin CN 抓取 v2（浏览器内 fetch）")
    ap.add_argument("--dry", action="store_true", help="只打印计划，不发请求")
    ap.add_argument("--probe", action="store_true", help="只验证通路（1 个请求）")
    ap.add_argument("--only", default="", help="只抓指定端点，逗号分隔")
    ap.add_argument("--level", type=int, default=1, help="按等级抓，默认 1")
    ap.add_argument("--date", default="", help="单日 YYYY-MM-DD")
    ap.add_argument("--start", default="", help="区间起")
    ap.add_argument("--end", default="", help="区间止")
    ap.add_argument("--real-profile", action="store_true",
                    help="用真实 Edge profile 启动（Edge 必须已关闭）")
    ap.add_argument("--cdp", action="store_true",
                    help="连到调试端口 Edge（默认就是这条，保留开关只为兼容旧命令）")
    ap.add_argument("--storage", action="store_true",
                    help="旧模式：用 storage_state.json + 干净实例（[warn] 实测会被 CF 拦成 403）")
    ap.add_argument("--morning", action="store_true",
                    help=" 早课表专用：只抓 sleep / heart_rate / hrv_daily / daily_summary")
    ap.add_argument("--activity", default="",
                    help="活动 ID，配合 activity_detail / activity_splits / activity_hr_zones 用")
    args = ap.parse_args()

    jobs, ctx = build_jobs(args)

    if args.dry:
        return cmd_dry(jobs, ctx)

    # 默认走 CDP：storage_state 模式会被 Cloudflare 一律拦成 403（见 docs/PITFALLS.md）
    if args.storage:
        mode = "storage"
    elif args.real_profile:
        mode = "real"
    else:
        mode = "cdp"
    if mode != "cdp" and not STATE_FILE.exists():
        die("这个模式需要 storage_state.json。先跑：python login.py --from-edge\n"
            "（推荐直接用默认的 CDP 模式，不需要任何登录态文件）")

    cap = 2 if args.probe else MAX_REQ_PER_RUN
    print("=" * 70)
    print(f" 抓取 {len(jobs)} 个端点 @ {BASE}")
    print(f" 整页加载 1 次 · API 上限 {cap} 个 · 间隔 {DELAY_MIN_S}~{DELAY_MAX_S}s")
    print("=" * 70)
    return run(jobs, ctx, cap, mode)


if __name__ == "__main__":
    sys.exit(main())
