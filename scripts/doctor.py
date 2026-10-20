"""doctor.py —— 环境自检。装完先跑这个，别先跑去抓数据。

用法:
    python scripts/doctor.py

输出一张 PASS / WARN / FAIL 表，并有针对性的一句话建议。
退出码：0 = 可以跑；1 = 有 FAIL 项必须修。
"""
from __future__ import annotations

import os
import socket
import sys

for _s in (sys.stdout, sys.stderr):          # Windows 控制台默认 GBK，避免符号炸
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
rows: list[tuple[str, str, str]] = []


def add(item: str, status: str, note: str) -> None:
    rows.append((item, status, note))


def check_python() -> None:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 9):
        add("Python", PASS, ver)
    else:
        add("Python", FAIL, f"{ver} 太旧，需要 3.9+")


def check_playwright() -> None:
    try:
        import playwright  # noqa: F401
        from importlib.metadata import version
        add("playwright", PASS, version("playwright"))
    except Exception:
        add("playwright", FAIL, "未安装 → pip install -r requirements.txt")


def _browser_candidates() -> list[Path]:
    cands: list[Path] = []
    if sys.platform.startswith("win"):
        for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                cands.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    elif sys.platform == "darwin":
        cands.append(Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"))
    else:
        for p in ("/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable",
                  "/usr/bin/google-chrome"):
            cands.append(Path(p))
    return cands


def check_browser() -> None:
    found = [p for p in _browser_candidates() if p.exists()]
    if found:
        add("Edge", PASS, str(found[0]))
    elif sys.platform.startswith("win"):
        add("Edge", FAIL,
            "没找到 msedge.exe。本工具用系统已装的 Edge（channel='msedge'），"
            "请先装 Edge")
    else:
        add("Edge", WARN,
            "非 Windows 上本工具尚未适配（fetch.py 的『自动拉起浏览器』只认 Windows 路径）。"
            "可先手动用 --remote-debugging-port 启动浏览器，再用 --cdp 连它")


def check_port() -> None:
    port = int(os.environ.get("GARMIN_CDP_PORT", "9222") or 9222)
    s = socket.socket()
    s.settimeout(0.6)
    try:
        open_ = s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()
    if open_:
        add(f"CDP 端口 {port}", PASS, "已在监听（可能正是你的 Edge 调试端口）")
    else:
        add(f"CDP 端口 {port}", WARN,
            "未监听。首次运行时 fetch.py 会自己拉起浏览器；若报"
            "「Edge 正在运行但没开调试端口」，关掉所有 Edge 窗口再跑即可")


def check_layout() -> None:
    need = [HERE / "fetch.py", HERE / "endpoints.py"]
    miss = [p.name for p in need if not p.exists()]
    if miss:
        add("文件完整性", FAIL, f"缺 {', '.join(miss)}")
    else:
        add("文件完整性", PASS, "fetch.py / endpoints.py 就位")


def check_writable() -> None:
    d = HERE / "raw"
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".doctor_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("数据目录可写", PASS, str(d))
    except Exception as exc:
        add("数据目录可写", FAIL, f"{d} 写不进去：{type(exc).__name__}")


def check_net() -> None:
    s = socket.socket()
    s.settimeout(3.0)
    try:
        code = s.connect_ex(("connect.garmin.cn", 443))
    finally:
        s.close()
    if code == 0:
        add("connect.garmin.cn:443", PASS, "可达")
    else:
        add("connect.garmin.cn:443", WARN,
            "连不上（代理/DNS/网络）。国区账号必须能直连这个域")


def check_athlete() -> None:
    p = ROOT / "rules" / "athlete.yaml"
    if p.exists():
        add("训练档案 athlete.yaml", PASS, str(p))
    else:
        add("训练档案 athlete.yaml", WARN,
            "还没有。复制 rules/athlete.example.yaml 填好再让 AI 出课表；"
            "没它只能出恢复跑")


def main() -> int:
    print("=" * 78)
    print(" garmin-cn-coach · 环境自检")
    print("=" * 78)
    check_python()
    check_playwright()
    check_browser()
    check_port()
    check_layout()
    check_writable()
    check_net()
    check_athlete()

    w = max(len(r[0]) for r in rows)
    for item, status, note in rows:
        tag = {"PASS": "[ ok ]", "WARN": "[warn]", "FAIL": "[FAIL]"}[status]
        print(f"{tag} {item.ljust(w)}  {note}")

    fails = sum(1 for r in rows if r[1] == FAIL)
    warns = sum(1 for r in rows if r[1] == WARN)
    print("-" * 78)
    print(f" {len(rows) - fails - warns} PASS · {warns} WARN · {fails} FAIL")
    if fails:
        print("\n先修 FAIL 项。")
        return 1
    print("\n可以开始了： python scripts/fetch.py --cdp --probe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
