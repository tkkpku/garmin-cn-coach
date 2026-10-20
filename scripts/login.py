"""Garmin Connect 中国区 · 两种拿登录态的方式

方式一（推荐）：--from-edge —— 继承你日常 Edge 的登录态
    不是"重新登录"，是"把已经登录过的浏览器搬过来用"。
    做法：把你 Edge profile 里登录相关的几个文件**整份复制**到项目下的
    临时 profile，再用 Playwright 启动一份副本。
    你现在的 Edge 不用关，标签页不受影响。

方式二：不带参数 —— 弹一个干净 Edge 窗口自己登录
    [warn] 实测这条路走不通：全新上下文没有历史、没有 cookie，Garmin 新版 SSO
    会把提交按钮禁掉（2026-10-20 踩过）。留着只作备用。

【凭据边界】
- 方式一只复制文件，**从不读取、不回显任何 cookie 内容**
- 复制出的临时 profile 和 storage_state.json 都已 gitignore
- 想作废：删 storage_state.json 和 _edge_profile_copy/ 即可
"""

import sys

# [!] Windows 控制台默认 GBK，打印 [ok] [x] 这类符号会抛 UnicodeEncodeError。
# 统一改 UTF-8，编不出的字符顶替而不是崩。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import argparse
import json
import os
import shutil
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

# ── 参数 ────────────────────────────────────────────────────────
HERE = Path(__file__).parent
STATE_FILE = HERE / "storage_state.json"
TMP_PROFILE = HERE / "_edge_profile_copy"
MARKER = TMP_PROFILE / ".dsh_garmin_tmp"      # 自建目录标记，防止误删别人的目录

# 日常用的域（connect.garmin.cn/app/home）
APP_URL = "https://connect.garmin.cn/app/home"

LOGIN_TIMEOUT_S = 600
POLL_INTERVAL_S = 2.0
SETTLE_S = 3.0

SESSION_COOKIE_HINTS = ("sessionid", "sso", "token", "auth")


# ── 登录态判据（只认 cookie，不认网址）─────────────────────────

def find_session_cookie(cookies):
    """找 Garmin 域下的登录会话 cookie。只返回名字，绝不碰值。"""
    for c in cookies:
        name = (c.get("name") or "").lower()
        domain = (c.get("domain") or "").lower()
        if "garmin" not in domain:
            continue
        if any(h in name for h in SESSION_COOKIE_HINTS):
            return c.get("name"), c.get("domain")
    return None, None


def on_login_page(url: str) -> bool:
    u = url.lower()
    return "signin" in u or "sign-in" in u or "sso" in u or "login" in u


def save_and_verify(context) -> int:
    """落盘 storage_state 并自检。空文件 = 假成功，直接判失败。"""
    context.storage_state(path=str(STATE_FILE))
    size = STATE_FILE.stat().st_size
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    n_cookies = len(data.get("cookies") or [])
    n_origins = len(data.get("origins") or [])

    print()
    print("=" * 64)
    if n_cookies == 0:
        print("[[x]] 落盘了但 cookies 数量为 0 —— 假成功，不算数。")
        print(f"    文件 {size} 字节，只是空壳。")
        print("=" * 64)
        return 5
    print("[✓] 登录态已保存并通过自检")
    print(f"    文件：{STATE_FILE}")
    print(f"    大小：{size} 字节")
    print(f"    cookies：{n_cookies} 条 ｜ origins：{n_origins} 条")
    print("=" * 64)
    return 0


# ── 方式一：从 Edge 的 profile 继承登录态 ─────────────────────

def _edge_user_data() -> Path:
    local = os.environ.get("LOCALAPPDATA") or ""
    return Path(local) / "Microsoft" / "Edge" / "User Data"


def _safe_rmtree(path: Path) -> bool:
    """只删带我们标记的目录。防止手滑删到别处。"""
    if not path.exists():
        return True
    if not (path / ".dsh_garmin_tmp").exists():
        print(f"[!] {path} 没有本项目的标记文件，拒绝删除。")
        return False
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def _copy_file(src: Path, dst: Path) -> None:
    """复制文件 —— 刻意不用 shutil.copy2。

    踩坑记录（2026-10-20）：Edge 运行时以 FILE_SHARE_READ|FILE_SHARE_WRITE
    打开 cookie 数据库，shutil.copy2 底层的 CopyFile2 会报
    PermissionError [WinError 32]。而 Python 的 open() 用的是同款共享模式，
    分块读写就能读出来 —— 于是不用关 Edge。
    若哪天这条也失效了，就只能请你先关掉所有 Edge 窗口。
    """
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            chunk = fsrc.read(1 << 20)      # 1 MB 一块
            if not chunk:
                break
            fdst.write(chunk)
    try:
        shutil.copystat(src, dst)           # 元数据失败不影响可用性
    except Exception:
        pass


def _copy_edge_profile() -> int:
    """把 Edge profile 里登录需要的文件整份复制过来。不读内容。"""
    src_root = _edge_user_data()
    if not (src_root / "Local State").exists():
        print(f"[!] 没找到 Edge profile：{src_root}")
        return 2

    rel_files = [
        "Local State",
        "Default/Network/Cookies",
        "Default/Network/Cookies-journal",
        "Default/Network/Cookies-wal",
        "Default/Network/Cookies-shm",
        "Default/Preferences",
        "Default/Secure Preferences",
    ]

    if not _safe_rmtree(TMP_PROFILE):
        return 2
    TMP_PROFILE.mkdir(parents=True, exist_ok=True)
    MARKER.write_text("temp profile for garmin fetch, safe to delete\n",
                      encoding="utf-8")

    copied, missing, failed = [], [], []
    for rel in rel_files:
        src = src_root / Path(rel)
        dst = TMP_PROFILE / Path(rel)
        if not src.exists():
            missing.append(rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            _copy_file(src, dst)
            copied.append((rel, dst.stat().st_size))
        except PermissionError as exc:
            failed.append((rel, str(exc)))

    print("[i] 已复制（整份拷贝，未读取内容）：")
    for rel, size in copied:
        print(f"      {rel:<34} {size:>9} bytes")
    if missing:
        print(f"[i] 不存在、已跳过：{', '.join(missing)}")
    if failed:
        print()
        print("[!] 以下文件被 Edge 独占，复制失败：")
        for rel, err in failed:
            print(f"      {rel}  →  {err}")
        if any("Cookies" in rel for rel, _ in failed):
            print()
            print(" [!] cookie 数据库没过来说明锁得更死。请**关掉所有 Edge 窗口**")
            print("    （标签页会自动恢复），然后重跑本条命令。")
            return 6
    return 0


def from_edge() -> int:
    print("=" * 64)
    print(" 方式一 · 继承你日常 Edge 的登录态")
    print("=" * 64)
    print(" 不重新登录，只把你 Edge profile 里的登录相关文件复制一份过来。")
    print(" [!] 你现在的 Edge 不用关，标签页不受影响。")
    print()

    rc = _copy_edge_profile()
    if rc != 0:
        return rc

    print()
    print(f"[i] 用副本 profile 启动 Edge，打开 {APP_URL}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(TMP_PROFILE),
            channel="msedge",
            headless=False,
            locale="zh-CN",
            no_viewport=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            print(f"[!] 打开 {APP_URL} 失败：{type(exc).__name__}: {exc}")
            context.close()
            return 3

        time.sleep(SETTLE_S)
        cookies = context.cookies()
        garmin_n = sum(1 for c in cookies
                       if "garmin" in (c.get("domain") or "").lower())
        name, domain = find_session_cookie(cookies)

        print(f"[i] 当前页面：{page.url}")
        print(f"[i] garmin cookie：{garmin_n} 条")
        print(f"[i] 会话 cookie：{name or '（没找到）'}"
              + (f" @ {domain}" if domain else ""))

        if not name or on_login_page(page.url):
            print()
            print("[!] 副本里没有可用的登录态 —— 有两种可能：")
            print("    ① 你的 Edge 登录态已经过期（浏览器里还能看，cookie 已失效）")
            print("    ② 关键 cookie 存在别的文件里（本脚本没复制到）")
            print("    → 先在你自己的 Edge 里刷新一下 Garmin 页面确认还登着，再重跑。")
            context.close()
            return 4

        rc = save_and_verify(context)
        context.close()

    if rc == 0:
        print()
        print(" 下一步：")
        print("     python fetch.py --probe     # 2 个请求，看哪个域真回 200")
    return rc


# ── 方式二：干净窗口手动登录（备用）──────────────────────────

def manual_login() -> int:
    home = "https://connectus.garmin.cn/zh_CN/"
    print("=" * 64)
    print(" 方式二 · 干净窗口手动登录（备用）")
    print("=" * 64)
    print(" [warn] 这条路 2026-10-20 实测失败了：全新上下文无历史无 cookie，")
    print("    Garmin 新版 SSO 会把「登录」按钮禁掉。推荐改用 --from-edge。")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=False)
        context = browser.new_context(locale="zh-CN")
        page = context.new_page()
        try:
            page.goto(home, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            print(f"[!] 打开首页失败：{type(exc).__name__}: {exc}")
            browser.close()
            return 2

        print(f"[i] 首页已打开：{page.url}")
        print(f"[i] 等你登录中……（最长 {LOGIN_TIMEOUT_S // 60} 分钟）")

        deadline = time.time() + LOGIN_TIMEOUT_S
        found = None
        last_note = ""
        while time.time() < deadline:
            if page.is_closed():
                print("[!] 窗口被关闭了，登录没完成。")
                browser.close()
                return 3
            cookies = context.cookies()
            garmin_n = sum(1 for c in cookies
                           if "garmin" in (c.get("domain") or "").lower())
            name, domain = find_session_cookie(cookies)
            if name and not on_login_page(page.url):
                found = (name, domain)
                break
            note = f"[…] 登录页={on_login_page(page.url)}  garmin cookie={garmin_n}"
            if note != last_note:
                print(note)
                last_note = note
            time.sleep(POLL_INTERVAL_S)

        if not found:
            print("[!] 超时，没检测到会话 cookie。登录态未保存。")
            browser.close()
            return 4

        time.sleep(SETTLE_S)
        print(f"[✓] 拿到会话 cookie：{found[0]} @ {found[1]}")
        rc = save_and_verify(context)
        browser.close()
    return rc


def from_real_profile() -> int:
    """用**真实** Edge profile 目录启动一次，导出登录态。

    为什么必须用真实目录（2026-10-20 实测）：
        复制出来的 profile 里 cookie 解不开 —— Edge 127+ 的
        App-Bound Encryption 把密钥绑在浏览器程序/进程族上，
        文件搬到别的 user_data_dir 就静默解不出来（只读到 3 条
        Cloudflare 的明文 cookie，登录态全丢）。

    安全措施：
        1. 动手前确认 Edge 已完全退出（否则目录被锁）
        2. 只备份两个小文件（Local State / Preferences），不碰大的
        3. 启动 → 立刻导出 → 立刻关闭，不做任何写操作
    """
    src_root = _edge_user_data()
    if not (src_root / "Local State").exists():
        print(f"[!] 没找到 Edge profile：{src_root}")
        return 2

    # ── 检查 Edge 是否已完全退出 ──────────────────────────────
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/NH"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:
        out = ""
    if "msedge.exe" in out.lower():
        print("[!] Edge 还在运行，profile 目录被占用。")
        print("    请先关掉**所有** Edge 窗口（Ctrl+Shift+W），再重跑。")
        return 6

    # ── 备份两个小文件 ────────────────────────────────────────
    backup = HERE / "_edge_backup"
    backup.mkdir(exist_ok=True)
    for rel in ("Local State", "Default/Preferences"):
        s = src_root / Path(rel)
        if s.exists():
            d = backup / Path(rel)
            d.parent.mkdir(parents=True, exist_ok=True)
            try:
                _copy_file(s, d)
                print(f"[i] 已备份 {rel} → _edge_backup/")
            except Exception as exc:
                print(f"[!] 备份 {rel} 失败：{exc}")

    print()
    print(f"[i] 用真实 profile 启动 Edge，打开 {APP_URL}")
    print("    [!] 这个窗口会继承你所有登录态 —— 别在里面点任何东西")
    print("    [!] 脚本导出完会自己关窗口")

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(src_root),
                channel="msedge",
                headless=False,
                locale="zh-CN",
                no_viewport=True,
                args=[
                    "--no-first-run",
                    "--disable-session-crashed-bubble",
                    "--disable-restore-session-state",
                ],
            )
        except Exception as exc:
            print(f"[!] 启动失败：{type(exc).__name__}: {exc}")
            print("    常见原因：Edge 没关干净 / profile 仍被锁。")
            return 3

        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            print(f"[!] 打开 {APP_URL} 失败：{type(exc).__name__}: {exc}")

        time.sleep(SETTLE_S)
        cookies = context.cookies()
        garmin_n = sum(1 for c in cookies
                       if "garmin" in (c.get("domain") or "").lower())
        name, domain = find_session_cookie(cookies)

        print(f"[i] 当前页面：{page.url}")
        print(f"[i] cookie 总数 {len(cookies)} 条，其中 garmin {garmin_n} 条")
        print(f"[i] 会话 cookie：{name or '（没找到）'}"
              + (f" @ {domain}" if domain else ""))

        if not name or on_login_page(page.url):
            print()
            print("[!] 真实 profile 里也没读到 Garmin 会话 —— 这说明：")
            print("    ① 登录态确实过期了（关 Edge 那一刻已失效），或")
            print("    ② Garmin 把它存在了我们看不到的地方。")
            print("    → 让脚本停在这，**你在弹出的窗口里手动登录一次**，")
            print("      然后重跑本命令导出。")
            context.close()
            return 4

        rc = save_and_verify(context)
        context.close()

    if rc == 0:
        print()
        print(" 备份留在 _edge_backup/（两个小文件），确认没问题后可删。")
        print(" 下一步：")
        print("     python fetch.py --probe     # 2 个请求，看哪个域真回 200")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="Garmin CN 登录态获取")
    ap.add_argument("--from-edge", action="store_true",
                    help="复制 Edge profile 副本再启动（[warn] 新版 Edge 因 ABE 会解不出 cookie）")
    ap.add_argument("--real-profile", action="store_true",
                    help="用真实 Edge profile 目录启动（Edge 必须已关闭） 推荐")
    ap.add_argument("--force", action="store_true",
                    help="已有 storage_state.json 时也直接覆盖")
    args = ap.parse_args()

    if STATE_FILE.exists() and not args.force:
        print(f"[i] 已存在 {STATE_FILE.name}（{STATE_FILE.stat().st_size} 字节）")
        ans = input("    要覆盖重新获取吗？[y/N] ").strip().lower()
        if ans != "y":
            print("[i] 保留原登录态，退出。")
            return 0

    if args.real_profile:
        return from_real_profile()
    return from_edge() if args.from_edge else manual_login()


if __name__ == "__main__":
    sys.exit(main())
