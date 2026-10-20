r"""生成 Garmin 数据周报 → 写入 md（默认 scripts/reports/garmin_weekly.md）

【设计取舍 —— 为什么这里允许一次发 13 个 API 请求】
fetch.py 把单次上限压到 6，是为了「日常反复跑」时别累积。
周报是**每周一次**的重活，而且应用自己加载一次首页就要发 50-100 个
/gc-api 请求。我们 13 个带正确 CSRF header、间隔 3~6s 的请求不算激进。
→ 一次页面加载 + 13 个请求（合计 14 次），比两次页面加载 + 各 6 个更省。

【幂等】按 ISO 周号做标记，同一周重复跑只替换那一节，不会堆重复。

用法：
    python weekly.py                # 抓取 + 生成本周报
    python weekly.py --no-fetch     # 不吃网络，只用 raw/ 里已有数据重算
"""

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import argparse
import glob
import json
import os
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

from endpoints import ENDPOINTS

# ── 路径与参数 ─────────────────────────────────────────────────
HERE = Path(__file__).parent
RAW = HERE / "raw"
REPORT = Path(os.environ.get("GARMIN_REPORT")
              or (HERE / "reports" / "garmin_weekly.md"))
CDP_URL = "http://127.0.0.1:" + os.environ.get("GARMIN_CDP_PORT", "9222")
BASE = "https://connect.garmin.cn"
APP_URL = BASE + "/app/home"

DELAY_MIN_S = 3.0
DELAY_MAX_S = 6.0
WEEKLY_CAP = 14                 # 见文件头「设计取舍」

# 周报要抓的（顺序有讲究：bootstrap 在代码里单独先跑）
PLAN = ["hrv_daily", "vo2max_trend", "race_predictions", "activities",
        "sleep", "heart_rate", "daily_summary", "hr_zones", "lactate_threshold",
        "intensity_minutes", "stress", "respiration", "spo2"]

FETCH_JS = """
async ([path, hdrs]) => {
    const h = Object.assign({}, hdrs || {});
    delete h['content-length']; delete h['Content-Length'];
    const r = await fetch(path, { credentials: 'include', headers: h });
    const txt = await r.text();
    let js = null; try { js = JSON.parse(txt); } catch (e) {}
    return { status: r.status, len: txt.length, json: js };
}
"""

CAPTURE_JS = r"""
window.__ghdr = {};
(function () {
    function merge(h) {
        if (!h) return;
        try {
            if (typeof Headers !== 'undefined' && h instanceof Headers)
                h.forEach(function (v, k) { window.__ghdr[k] = v; });
            else if (Array.isArray(h)) h.forEach(function (kv) { window.__ghdr[kv[0]] = kv[1]; });
            else if (typeof h === 'object') Object.keys(h).forEach(function (k) { window.__ghdr[k] = h[k]; });
        } catch (e) {}
    }
    var of = window.fetch;
    window.fetch = function (input, init) {
        try {
            var u = (typeof input === 'string') ? input : (input && input.url) || '';
            if (u.indexOf('/gc-api/') >= 0 && init && init.headers) merge(init.headers);
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


# ── 小工具 ─────────────────────────────────────────────────────
def hms(sec):
    sec = float(sec or 0)
    return f"{int(sec // 60)}:{sec % 60:05.2f}"


def km(m):
    return f"{(m or 0) / 1000:.2f}"


def deep(o, keys, depth=0):
    """在嵌套结构里找第一个命中的字段值。

    [!] 2026-10-20 踩过的坑：调用方写过 deep(x, ('restingHeartRate'))，
    而 Python 里 ('str') 就是 str，不是元组 → for k in keys 变成逐字符遍历
    → 永远匹配不上 → 整列 None。这里做兼容，别指望调用方不漏逗号。
    """
    if isinstance(keys, str):
        keys = (keys,)
    if depth > 10:
        return None
    if isinstance(o, dict):
        for k in keys:
            if k in o and o[k] is not None and not isinstance(o[k], bool):
                return o[k]
        for v in o.values():
            r = deep(v, keys, depth + 1)
            if r is not None:
                return r
    elif isinstance(o, list) and o:
        for v in reversed(o):          # 列表取最后一条有效记录
            r = deep(v, keys, depth + 1)
            if r is not None:
                return r
    return None


def latest_raw(prefix):
    fs = sorted(glob.glob(str(RAW / f"{prefix}_*.json")), key=os.path.getmtime)
    if not fs:
        return None
    try:
        return json.load(open(fs[-1], encoding="utf-8"))
    except Exception:
        return None


def fetch_all(args):
    """一次页面加载 + 抓 PLAN。返回 {key: json}。"""
    got = {}
    today = date.today()
    ctx_vars = {
        "date": today.isoformat(),
        "start_date": (today - timedelta(days=7)).isoformat(),
        "end_date": today.isoformat(),
        "guid": "",
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as exc:  # noqa: BLE001
            print(f"[[x]] 连不上调试端口 {CDP_URL}（{type(exc).__name__}）")
            print("    先跑一次： python scripts/fetch.py --cdp --probe")
            print("    它会自动拉起一个带调试端口的 Edge；成功后再重跑本脚本。")
            return 98
        ctx = browser.contexts[0]
        ctx.add_init_script(CAPTURE_JS)
        page = ctx.new_page()
        print(f"[i] 落地 {APP_URL}")
        page.goto(APP_URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(8000)

        hdr = page.evaluate("window.__ghdr || {}")
        if not hdr:
            print("[!] 没抄到 header，放弃抓取")
            page.close(); browser.close()
            return got
        print(f"[✓] header {len(hdr)} 个")

        # 引导：拿 guid
        res = page.evaluate(FETCH_JS, [ENDPOINTS["user_profile_base"]["path"], hdr])
        if res["status"] == 200:
            g = deep(res["json"], ("displayName", "userGuid"))
            ctx_vars["guid"] = g or ""
            print(f"[i] guid = {str(g)[:8]}…")
        time.sleep(random.uniform(DELAY_MIN_S, DELAY_MAX_S))

        used = 0
        for key in PLAN:
            if used >= WEEKLY_CAP:
                print(f"[…] 达上限 {WEEKLY_CAP}，剩余跳过")
                break
            path = ENDPOINTS[key]["path"]
            for ph in ("{date}", "{start_date}", "{end_date}", "{guid}"):
                if ph in path:
                    name = ph.strip("{}")
                    if not ctx_vars.get(name):
                        path = None
                        break
                    path = path.replace(ph, ctx_vars[name])
            if not path:
                print(f"[skip] {key}: 缺参数")
                continue
            time.sleep(random.uniform(DELAY_MIN_S, DELAY_MAX_S))
            used += 1
            r = page.evaluate(FETCH_JS, [path, hdr])
            print(f"   [{used}] {key:<18} HTTP {r['status']}  {r['len']} 字节")
            if r["status"] == 200 and r["json"] is not None:
                got[key] = r["json"]
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                (RAW / f"{key}_{stamp}.json").write_text(
                    json.dumps(r["json"], ensure_ascii=False, indent=2),
                    encoding="utf-8")

        page.close()
        browser.close()
    return got


# ── 报告生成 ───────────────────────────────────────────────────
def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def build_section() -> str:
    """从 raw/ 里读最新数据，生成一节 markdown。"""
    today = date.today()
    week = iso_week(today)
    mon = today - timedelta(days=today.weekday())
    sun = mon + timedelta(days=6)

    sleep = latest_raw("sleep")
    hr = latest_raw("heart_rate")
    hrv = latest_raw("hrv_daily")
    ds = latest_raw("daily_summary")
    zones = latest_raw("hr_zones")
    lt = latest_raw("lactate_threshold")
    vo2 = latest_raw("vo2max_trend")
    race = latest_raw("race_predictions")
    acts = latest_raw("activities")
    im = latest_raw("intensity_minutes")
    resp = latest_raw("respiration")
    spo2 = latest_raw("spo2")
    stress = latest_raw("stress")

    L = []
    L.append(f"<!-- WEEK-{week} -->")
    L.append(f"## {week} · {mon.isoformat()} → {sun.isoformat()}")
    L.append("")
    L.append(f"> 生成时间 {datetime.now():%Y-%m-%d %H:%M} ｜ 数据源 Garmin Connect 中国区 `/gc-api`")
    L.append("")

    # 晨检
    L.append("### 晨检（最新一日）")
    L.append("")
    L.append("| 项 | 值 | 备注 |")
    L.append("| --- | --- | --- |")
    if sleep:
        st = deep(sleep, ("sleepTimeSeconds"))
        deep_s = deep(sleep, ("deepSleepSeconds"))
        rem = deep(sleep, ("remSleepSeconds"))
        light = deep(sleep, ("lightSleepSeconds"))
        row = f"{st/3600:.2f} h" if st else "—"
        det = (f"深 {deep_s/3600:.2f} / 浅 {light/3600:.2f} / REM {rem/3600:.2f} h"
               if st else "—")
        L.append(f"| 睡眠时长 | **{row}** | {det} |")
    if hr:
        L.append(f"| 静息心率 | **{deep(hr,('restingHeartRate'))} bpm** "
                 f"| 7 日均 {deep(hr,('lastSevenDaysAvgRestingHeartRate'))} |")
    if hrv:
        L.append(f"| HRV | 昨夜 **{deep(hrv,('lastNightAvg'))}** / 周均 "
                 f"{deep(hrv,('weeklyAvg'))} | 状态 **{deep(hrv,('status'))}** |")
    if ds:
        L.append(f"| 身体电量 | {deep(ds,('bodyBatteryMostRecentValue'))} "
                 f"| 最高 {deep(ds,('bodyBatteryHighestValue'))} |")
        L.append(f"| 压力均值 | {deep(ds,('averageStressLevel'))} | — |")
    if stress:
        L.append(f"| 压力峰值 | {deep(stress,('maxStressLevel'))} | — |")
    if resp:
        L.append(f"| 呼吸（醒时均） | {deep(resp,('avgWakingRespirationValue'))} 次/分 | — |")
    if spo2:
        L.append(f"| 血氧均值 | {deep(spo2,('averageSpO2'))}% | 最低 {deep(spo2,('lowestSpO2'))}% |")
    L.append("")

    # 训练指标
    L.append("### 训练指标")
    L.append("")
    L.append("| 项 | 值 | 说明 |")
    L.append("| --- | --- | --- |")
    if zones:
        z = zones[0] if isinstance(zones, list) and zones else zones
        floors = [z.get(f"zone{i}Floor") for i in range(1, 6)]
        L.append(f"| 心率分区地板 | {floors[0]}/{floors[1]}/{floors[2]}/"
                 f"{floors[3]}/{floors[4]} | 手表原文 |")
        L.append(f"| **HRmax（手表认定）** | **{z.get('maxHeartRateUsed')}** "
                 f"| `trainingMethod = {z.get('trainingMethod')}` |")
        L.append(f"| 静息心率（分区基准） | {z.get('restingHeartRateUsed')} | — |")
    if lt and isinstance(lt, list):
        spd = next((x.get("speed") for x in lt if x.get("speed")), None)
        hrev = next((x.get("hearRate") for x in lt if x.get("hearRate")), None)
        d = next((x.get("calendarDate") for x in lt if x.get("calendarDate")), "")
        if spd:
            L.append(f"| 乳酸阈值 | **{hrev} bpm / {hms(1000/(spd*10))}/km** "
                     f"| 记录日 {d[:10]} |")
    if vo2 and isinstance(vo2, list):
        vs = [x.get("generic", {}) for x in vo2 if x.get("generic")]
        if vs:
            first, last = vs[0], vs[-1]
            L.append(f"| VO2max | **{last.get('vo2MaxValue')}** "
                     f"（{last.get('vo2MaxPreciseValue')}）"
                     f"| 本周 {first.get('vo2MaxPreciseValue')} → "
                     f"{last.get('vo2MaxPreciseValue')} |")
    if im:
        L.append(f"| 强度分钟（周） | {deep(im,('weeklyTotal'))} / 目标 "
                 f"{deep(im,('weekGoal'))} | 中 {deep(im,('weeklyModerate'))} + "
                 f"高 {deep(im,('weeklyVigorous'))} |")
    if race and isinstance(race, list):
        r = race[-1]
        L.append(f"| 比赛预测 5K | **{hms(r.get('time5K'))}** | Garmin 模型 |")
        L.append(f"| 比赛预测 10K | **{hms(r.get('time10K'))}** | Garmin 模型 |")
    L.append(f"| 健身年龄 | — | `USER_UNDER_AGE`（需 ≥20 岁，不可用） |")
    L.append("")

    # 本周活动
    L.append("### 本周活动")
    L.append("")
    if acts and isinstance(acts, list):
        rows = []
        total = 0.0
        for a in acts:
            st = (a.get("startTimeLocal") or "")[:10]
            if not st:
                continue
            try:
                if not (mon <= date.fromisoformat(st) <= sun):
                    continue
            except Exception:
                continue
            dist = a.get("distance", 0) or 0
            total += dist / 1000
            rows.append((st, a.get("activityType", {}).get("typeKey", ""),
                         dist / 1000, a.get("duration", 0)))
        if rows:
            L.append("| 日期 | 类型 | 距离 | 时长 |")
            L.append("| --- | --- | --- | --- |")
            for st, tp, kmv, dur in sorted(rows):
                L.append(f"| {st} | {tp} | {kmv:.2f} km | {hms(dur)} |")
            L.append(f"| **合计** | {len(rows)} 次 | **{total:.2f} km** | — |")
        else:
            L.append("_本周暂无活动记录_")
    else:
        L.append("_活动列表未抓到_")
    L.append("")

    # 与训练日志的对拍提醒
    L.append("### [warn] 数据与主观反馈的边界")
    L.append("")
    L.append("本节全部为**客观数据**。以下必须由你口报，本表永远不会有：")
    L.append("RPE ｜ 有无刺痛 ｜ 腿感（沉/紧/正常）｜ 哪段最难受")
    L.append("")
    return "\n".join(L)


def upsert(week: str, section: str) -> None:
    marker = f"<!-- WEEK-{week} -->"
    header = (
        "# 92 · Garmin 数据周报\n\n"
        "> **自动生成，不要手改正文**（重跑会覆盖当周那一节）。\n"
        "> 生成器：`scripts/weekly.py`（garmin-cn-coach）\n"
        "> [!] 本文件含真实健康数据，只在本地，不外传。\n\n"
        "---\n\n"
    )
    if REPORT.exists():
        text = REPORT.read_text(encoding="utf-8")
    else:
        text = header

    if marker in text:
        start = text.index(marker)
        nxt = text.find("<!-- WEEK-", start + len(marker))
        end = nxt if nxt != -1 else len(text)
        text = text[:start] + section + ("\n" if nxt != -1 else "") + text[end:]
        print(f"[i] 已更新 {week} 那一节")
    else:
        # 插到第一个周节之前（周报按新→旧排列）
        first = text.find("<!-- WEEK-")
        if first == -1:
            text = text.rstrip() + "\n\n" + section
        else:
            text = text[:first] + section + "\n" + text[first:]
        print(f"[i] 已新增 {week} 那一节")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text, encoding="utf-8")
    print(f"[✓] 已写入 {REPORT}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Garmin 数据周报生成器")
    ap.add_argument("--no-fetch", action="store_true", help="不吃网络，只用已有 raw/ 重算")
    args = ap.parse_args()

    if not args.no_fetch:
        print("=" * 66)
        print(" 抓取周报数据（一次页面加载 + 最多 %d 个请求）" % WEEKLY_CAP)
        print("=" * 66)
        got = fetch_all(args)
        print(f"\n[i] 本轮抓到 {len(got)} 个端点")

    section = build_section()
    upsert(iso_week(date.today()), section)
    print("\n--- 预览 ---")
    print(section[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
