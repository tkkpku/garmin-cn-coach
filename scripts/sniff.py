"""嗅探 Garmin Connect 前端真实发出的 API 请求。

【为什么要有这个脚本】
    靠翻别人项目的端点表去猜路径，撞了两次墙（403 / 401）。
    正确做法是让浏览器自己告诉我们：它打哪些 URL、返回什么状态。

【做法】
    用真实 Edge profile 启动一次（Edge 必须先关掉），打开 /app/home，
    只做一次正常页面加载，收集所有 XHR / fetch 请求的
    (method, url, status)，落盘后分析。

[!] 不打印任何 header 值、不打印响应体 —— 只记 URL 和状态码。
[!] 一次页面加载，不做反复刷新。
"""

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import json
import subprocess
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
APP_URL = "https://connect.garmin.cn/app/home"
WAIT_AFTER_LOAD_S = 12      # 页面加载后再挂 12 秒，等异步请求发完


def _edge_user_data() -> Path:
    """Edge 的 User Data 目录（Windows / macOS / Linux）。与 fetch.py 同款逻辑。"""
    import os
    env = (os.environ.get("GARMIN_USER_DATA") or "").strip()
    if env:
        return Path(env)
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Microsoft Edge"
    return Path.home() / ".config" / "microsoft-edge"


def edge_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/NH"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return False
    return "msedge.exe" in out.lower()


def main() -> int:
    if edge_running():
        print("[!] Edge 还在跑，profile 被占用。先关掉所有 Edge 窗口再来。")
        return 6

    user_data = _edge_user_data()

    recs = []
    RAW.mkdir(exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data),
            channel="msedge",
            headless=False,
            locale="zh-CN",
            no_viewport=True,
            args=["--no-first-run", "--disable-session-crashed-bubble",
                  "--disable-restore-session-state"],
        )
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(resp):
            try:
                rt = resp.request.resource_type
                if rt not in ("xhr", "fetch"):
                    return
                recs.append({
                    "method": resp.request.method,
                    "url": resp.url,
                    "status": resp.status,
                    "type": rt,
                })
            except Exception:
                pass

        page.on("response", on_response)

        print(f"[i] 打开 {APP_URL}（一次页面加载）")
        try:
            page.goto(APP_URL, wait_until="domcontentloaded", timeout=90_000)
        except Exception as exc:
            print(f"[!] 加载异常：{type(exc).__name__}: {exc}")
        print(f"[i] 已加载，再挂 {WAIT_AFTER_LOAD_S} 秒收异步请求")
        time.sleep(WAIT_AFTER_LOAD_S)
        final_url = page.url
        context.close()

    print(f"[i] 落地页：{final_url}")
    print(f"[i] 捕获 XHR/fetch：{len(recs)} 个\n")

    # ── 按 host 归类 ────────────────────────────────────────
    hosts = Counter(urlsplit(r["url"]).netloc for r in recs)
    print("按 host 统计：")
    for h, n in hosts.most_common():
        print(f"   {h:<34} {n}")
    print()

    # ── 只看 garmin 的 ──────────────────────────────────────
    g = [r for r in recs if "garmin" in urlsplit(r["url"]).netloc.lower()]
    print(f"Garmin 域下 XHR：{len(g)} 个\n")

    # 按「路径第一段」聚合，找 API 前缀
    prefixes = Counter()
    for r in g:
        parts = [x for x in urlsplit(r["url"]).path.split("/") if x]
        prefix = "/" + "/".join(parts[:2]) if parts else "/"
        prefixes[(urlsplit(r["url"]).netloc, prefix)] += 1
    print("API 前缀聚合（host + 路径前两段）：")
    for (h, pre), n in prefixes.most_common(20):
        print(f"   {h:<32} {pre:<40} {n}")

    print()
    print("明细（最多 40 条，按状态码排序）：")
    print(f"{'status':>7} {'method':<7} url")
    print("-" * 110)
    for r in sorted(g, key=lambda x: (x["status"] != 200, x["status"]))[:40]:
        print(f"{r['status']:>7} {r['method']:<7} {r['url'][:96]}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = RAW / f"sniff_{stamp}.json"
    out.write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[i] 全量落盘：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
