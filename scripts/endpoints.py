"""Garmin Connect 中国区端点表。

【路径来源 —— 不是猜的】
2026-10-20 用 sniff.py 抓取 connect.garmin.cn/app/home 的真实流量，
380 个 XHR 里 94 个 /gc-api 请求返回 200，以下路径按实测校正。

【实测校正记录（别改回去）】
- [x] `/gc-api/graphql`           → [ok] `/gc-api/graphql-gateway`
- [x] `personal-information`      → [ok] `personal-information/{guid}`（要带用户 GUID）
- [x] `usersummary/daily/{display_name}` → [ok] `usersummary/daily/{guid}`
- [ok] 无需参数即可拿：`user-settings/` · `userProfileBase` · `heartRateZones/`
                    · `deviceregistration/devices` · `system-service/timezoneUnits`

【占位符】
    {date} / {start_date} / {end_date}   YYYY-MM-DD
    {guid}    用户 GUID，由 user_profile_base 或 user-settings 取得
"""

REST = "rest"

ENDPOINTS = {
    # ── level 0：引导（必须先跑，拿 GUID）──────────────────────
    "user_profile_base": {
        "kind": REST,
        "path": "/gc-api/userprofile-service/userprofile/userProfileBase",
        "needs": [],
        "level": 0,
        "desc": " 引导端点：不带参数就能拿，内含 user GUID / displayName",
    },
    "user_settings": {
        "kind": REST,
        "path": "/gc-api/userprofile-service/userprofile/user-settings/",
        "needs": [],
        "level": 0,
        "desc": "账号设置（实测 200，备用引导）",
    },
    "timezone_units": {
        "kind": REST,
        "path": "/gc-api/system-service/timezoneUnits",
        "needs": [],
        "level": 0,
        "desc": "时区与单位制（实测 200）",
    },

    # ── level 1：核心（训练教练档真正用得上的）──────────────────
    "hr_zones": {
        "kind": REST,
        "path": "/gc-api/biometric-service/heartRateZones/",
        "needs": [],
        "level": 1,
        "desc": " 心率分区（实测 200）。§2 权限训示说的「手表原文」就在这",
    },
    "devices": {
        "kind": REST,
        "path": "/gc-api/device-service/deviceregistration/devices",
        "needs": [],
        "level": 1,
        "desc": "已连接设备（实测 200，返回已配对手表列表）",
    },
    "lactate_threshold": {
        "kind": REST,
        "path": "/gc-api/biometric-service/biometric/latestLactateThreshold",
        "needs": [],
        "level": 1,
        "desc": "[ok] 200 · 乳酸阈值（阈值心率 bpm + 阈值配速 + 记录日）",
    },
    "personal_info": {
        "kind": REST,
        "path": "/gc-api/userprofile-service/userprofile/personal-information/{guid}",
        "needs": ["guid"],
        "level": 1,
        "desc": "个人信息（实测需带 GUID）",
    },
    "daily_summary": {
        "kind": REST,
        "path": "/gc-api/usersummary-service/usersummary/daily/{guid}?calendarDate={date}",
        "needs": ["guid", "date"],
        "level": 1,
        "desc": "单日汇总（实测路径需 GUID）",
    },

    # ── level 2：扩展（首页没加载，路径按全球区惯例，可能要对）────
    "heart_rate": {
        "kind": REST,
        "path": "/gc-api/wellness-service/wellness/dailyHeartRate/{guid}?date={date}",
        "needs": ["guid", "date"],
        "level": 2,
        "desc": "[ok] 200 · 单日全天心率（静息 / 7 日均 / 最大）",
    },
    "sleep": {
        "kind": REST,
        "path": "/gc-api/wellness-service/wellness/dailySleepData/{guid}?date={date}",
        "needs": ["guid", "date"],
        "level": 2,
        "desc": "[ok] 200 · 单日睡眠（分期 + 睡眠心率 / 约 170 KB）",
    },
    "hrv_daily": {
        "kind": REST,
        "path": "/gc-api/hrv-service/hrv/daily/{start_date}/{end_date}",
        "needs": ["start_date", "end_date"],
        "level": 2,
        "desc": "[ok] 200 · HRV（lastNightAvg / weeklyAvg / status 如 BALANCED）",
    },
    "activities": {
        "kind": REST,
        "path": "/gc-api/activitylist-service/activities/search/activities?limit=20&start=0",
        "needs": [],
        "level": 2,
        "desc": "[ok] 200 · 活动列表（activityId / 距离 / 时长 / startTimeLocal）",
    },
    "activity_detail": {
        "kind": REST,
        "path": "/gc-api/activity-service/activity/{activity_id}",
        "needs": ["activity_id"],
        "level": 2,
        "desc": "[ok] 200 · 单次详情。[!] 真数据在顶层 summaryDTO（50 字段），不是顶层",
    },
    "activity_splits": {
        "kind": REST,
        "path": "/gc-api/activity-service/activity/{activity_id}/splits?splitType=KM",
        "needs": ["activity_id"],
        "level": 2,
        "desc": "[ok] 200 ·  必须带 splitType=KM 才返回 lapDTOs（干净 1 km 计圈）；"
                "不带参数给的是 splits[]（自动侦测分段，不能当计圈用）",
    },
    "activity_hr_zones": {
        "kind": REST,
        "path": "/gc-api/activity-service/activity/{activity_id}/hrTimeInZones",
        "needs": ["activity_id"],
        "level": 2,
        "desc": "[ok] 200 · 心率区间分布（zoneNumber / secsInZone / zoneLowBoundary）",
    },
    "training_plans": {
        "kind": REST,
        "path": "/gc-api/trainingplan-service/trainingplan/plans",
        "needs": [],
        "level": 2,
        "desc": "训练计划（实测 200）",
    },

    # ── 健康统计（页面路径可用 sniff.py 的产出核对）──────────
    "stress": {
        "kind": REST,
        "path": "/gc-api/wellness-service/wellness/dailyStress/{date}",
        "needs": ["date"],
        "level": 2,
        "desc": "单日压力（/app/stress/{date}/0）",
    },
    "respiration": {
        "kind": REST,
        "path": "/gc-api/wellness-service/wellness/daily/respiration/{date}",
        "needs": ["date"],
        "level": 2,
        "desc": "单日呼吸（/app/respiration/{date}）",
    },
    "intensity_minutes": {
        "kind": REST,
        "path": "/gc-api/wellness-service/wellness/daily/im/{date}",
        "needs": ["date"],
        "level": 2,
        "desc": "单日强度分钟（/app/intensity-minutes）",
    },
    "floors": {
        "kind": REST,
        "path": "/gc-api/wellness-service/wellness/floorsChartData/daily/{date}",
        "needs": ["date"],
        "level": 2,
        "desc": "单日爬楼层数（/app/floors）",
    },
    "body_battery_events": {
        "kind": REST,
        "path": "/gc-api/wellness-service/wellness/bodyBattery/events/{date}",
        "needs": ["date"],
        "level": 2,
        "desc": "身体电量事件（/app/body-battery）",
    },
    "spo2": {
        "kind": REST,
        "path": "/gc-api/wellness-service/wellness/dailySpo2/{date}",
        "needs": ["date"],
        "level": 2,
        "desc": "单日血氧（/app/pulse-ox/{date}）",
    },
    "fitness_age": {
        "kind": REST,
        "path": "/gc-api/fitnessage-service/fitnessage/{date}",
        "needs": ["date"],
        "level": 2,
        "desc": "健身年龄（/app/fitness-age）",
    },
    "vo2max_trend": {
        "kind": REST,
        "path": "/gc-api/metrics-service/metrics/maxmet/daily/{start_date}/{end_date}",
        "needs": ["start_date", "end_date"],
        "level": 2,
        "desc": "VO2max 序列",
    },
    "race_predictions": {
        "kind": REST,
        "path": "/gc-api/metrics-service/metrics/racepredictions/daily/{guid}"
                "?fromCalendarDate={start_date}&toCalendarDate={end_date}",
        "needs": ["guid", "start_date", "end_date"],
        "level": 2,
        "desc": "5K/10K/半马/全马预测（/app/report）",
    },
    "gear": {
        "kind": REST,
        "path": "/gc-api/gear-service/gear/v2/list",
        "needs": [],
        "level": 2,
        "desc": "装备清单（/app/gear，跑鞋里程）",
    },
}


def by_level(max_level: int):
    return [(k, v) for k, v in sorted(ENDPOINTS.items())
            if v["level"] <= max_level]
