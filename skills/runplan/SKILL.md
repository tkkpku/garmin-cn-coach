---
name: runplan
description: 用佳明国区数据与本地训练档案生成当日跑步课表（热身、主课、力量、拉伸），并把跑完的数据记入训练日志。当用户说"给我今天的跑步计划""今日跑步计划""今天要跑""要课表""周报""本周数据"等索要训练安排的短句时使用。
---

# 今日跑步计划

触发即生成当日完整课表，落成 md，再按配置交付。

本文件不含个人数据。配速、心率、跑量、约束全部来自 `rules/athlete.yaml` 与 `rules/training-rules.md`。

## 前置检查

| 需要 | 位置 | 缺失时 |
| --- | --- | --- |
| 训练档案 | rules/athlete.yaml | 拒绝安排强度课，先让用户补 hr_max、pace_prescription、constraints |
| 课型库 | rules/training-rules.md | 说明规则库缺失，只给评估不给处方 |
| 抓取脚本 | scripts/fetch.py | 让用户口报睡眠与静息心率 |
| 训练日志 | athlete.yaml 的 paths.log | 新建空日志，回话注明"首日无历史" |

档案里为 null 的关键项不许用默认值顶替。宁可只给恢复跑，也不要拿假数字开处方。

## 触发

用户发来下列任一或语义相近的短句：

`给我今天的跑步计划`、`今日跑步计划`、`今天要跑`、`要课表`、`跑步计划`、`今天的训练`、`明天跑什么`、`周报`、`本周数据`

### 被动响应

| 规则 | 说明 |
| --- | --- |
| 不排定时任务 | 训练要当天看状态。提前排等于拿昨天的假设替今天的决定。用户明确要自动再说 |
| 不提前落"正式课表" | 可以写草稿，但文件里必须标"草稿，待用户开口才发" |
| 意图陈述不算开口 | 用户说"我打算明天下午补一次强度"是告知打算，只给评估与建议，不落文件不推送 |
| 开口的判据 | 明确索要：要课表、今日计划、明天跑什么、今天要跑 |
| 报跑完的数据不等于要课表 | 走记录流程 |

## 第一步 核对日期

跑一次系统时间命令，不许沿用上一轮缓存的日期。曾经把"今天刚跑完"当成"两天前"，排出错的时间表。

## 第二步 同步数据

```bash
python scripts/fetch.py --cdp --morning
```

脚本自己处理浏览器：端口通了就复用，没开就自己拉起一个带调试端口的。唯一失败场景是浏览器开着但没带调试端口，此时请用户关一次窗口。

读 `scripts/raw/` 下最新文件：

| 文件 | 取值 | 用途 |
| --- | --- | --- |
| hrv_daily_*.json | lastNightAvg、weeklyAvg、status | status 为 UNBALANCED 或 LOW 时降档 |
| heart_rate_*.json | restingHeartRate、lastSevenDaysAvgRestingHeartRate | 高于基线达 redlines.rhr_delta_bpm 且连续两天，触发红线 |
| sleep_*.json | sleepTimeSeconds、深中浅与 REM | 低于 redlines.sleep_hours_min，当日最高到中等强度 |
| daily_summary_*.json | 步数、身体电量、压力 | 状态参考 |

变化慢的数据不必每天抓，每周补一次：`--only hr_zones,lactate_threshold,devices`

失败处理：403 或连不上端口，请用户关一次窗口后重跑，不要连续重试；404 说明端点变了，跑 `scripts/sniff.py` 重新嗅探；抓不到就退回请用户口报，并在课表注明数据未同步。

抓不到的必须问用户：

| 项目 | 为什么数据替代不了 |
| --- | --- |
| RPE | 心率不高但很累说明急性唤醒不足，数据看不出来 |
| 有无刺痛 | 红线都是主观信号 |
| 腿感 | 腘绳肌发紧是排课的第一道闸 |

只问这一句，其余自动。不要问距离、心率、步频，脚本里有。

## 第三步 出课表

### 选课型

照 `rules/training-rules.md` 的决策树：

```
上次跑是几天前
  0 天：昨天高强度则今天只能恢复或有氧底；昨天轻松则可中长或阈值
  1 到 2 天：常规轮转，优先补有氧底
  3 天以上：禁止直接上强度，第一堂给有氧底
最近 7 天高强度次数达到上限：今天禁高强
最近 7 天跑量达到周目标：进入减量
当日状态异常：降一档
硬约束：单次不超过 single_run_max_km；强度不连续两天；每周至少 rest_days_per_week_min 个休息日
```

### 查固定约束

`constraints.no_run_weekdays` 以及用户的课程与工作安排。问一句今天的实际情况，不要假设。

### 组装三件套

| 组件 | 来源 | 说明 |
| --- | --- | --- |
| 热身 | 规则库 | 低强度日简报，强度日全套含加速跑 |
| 主课 | 课型库 | 配速、距离、组数、组间歇、禁区 |
| 力量 | 规则库 | 仅低强度日，按跑步肌群、背部、腹部、手臂排序 |
| 拉伸 | 规则库 | 每天必做 |

强度日不做力量。跑完高强度后肌肉已有微损，再练力量会把刺激变成过度。

### 写文件

输出到 `paths.plans_dir`，文件名 `YYYY-MM-DD_跑步计划.md`，固定五节：

```markdown
# YYYY-MM-DD（周X）跑步计划

> 课型 · 主段处方 · 全程约 X km

## 1 热身
## 2 主课
## 3 力量
## 4 拉伸（跑后）
## 5 跑完要报给我
RPE __/10，有没有刺痛或异常痛
```

md 里只放训练内容，不写"为什么这么安排"、决策树依据、可信度表。那些只在对话里讲，而且不要长篇。用户是在手机或手表上照着练。

强度日额外保留两条：停止规则三条，里程兜底（超上限就砍收尾）。

### 交付

按 `notify.channel` 执行：none 只在对话里给，file 写文件，stdout 打印，qq 走外部通道。收件人、账号、token 一律从配置读，不写进本文件。

### 回话

简明复述课型、距离、配速、力量做不做、交付到哪。不要把整份 md 贴进对话。

## 第四步 记录（用户报来跑完的数据时）

先收主观两项：`RPE __/10` 与有无刺痛。只问这两项。

抓活动数据：

```bash
python scripts/fetch.py --cdp --only activities
# 按 startTimeLocal 取最近的 activityId，不要按 ID 大小排序
python scripts/fetch.py --cdp --only activity_detail,activity_splits,activity_hr_zones --activity <ID>
```

| 端点 | 数据位置 |
| --- | --- |
| activity_detail | summaryDTO |
| activity_splits | 必须带 `?splitType=KM`，数据在 lapDTOs |
| activity_hr_zones | zoneNumber、secsInZone、zoneLowBoundary |

自动核算：距离、时长、均配、均与最大心率、步频、步幅、触地、垂直振幅、有氧与无氧训练效果、负荷、卡路里、trainingEffectLabel、分圈逐圈数据。

写日志只追加不覆盖。表格放摘要，脚本读到的精确值写进备注栏并标"脚本直读"。

交叉核验，这一步价值最高：

| 核验 | 判据 |
| --- | --- |
| 均配与处方区间 | 超了就记，擦线 1 秒也记 |
| trainingEffectLabel | 恢复、基础训练、节奏，是校准配速表的唯一真依据 |
| RPE 与心率 | 心率不高但 RPE 很高，说明急性唤醒不足 |
| 分圈极差 | 大于 30 秒每公里说明配速控制差，小于 10 秒说明稳 |
| 步幅与配速 | 同配速比步幅，不要跨配速比 |

然后更新汇总统计与本周进度，回话给三档判读。

## 端点速查

脚本已封装，不要自己拼 URL。

| key | 用途 | 关键字段 |
| --- | --- | --- |
| user_profile_base | 引导，自动先跑 | displayName（即 URL 里的 GUID） |
| sleep | 睡眠 | sleepTimeSeconds、分期、restingHeartRate |
| heart_rate | 心率 | restingHeartRate、lastSevenDaysAvgRestingHeartRate、maxHeartRate |
| hrv_daily | HRV | lastNightAvg、weeklyAvg、status |
| daily_summary | 日汇总 | 步数、卡路里、压力、身体电量 |
| hr_zones | 分区 | 分区下限、maxHeartRateUsed、trainingMethod |
| lactate_threshold | 乳酸阈值 | hearRate（原文拼写）、speed |
| vo2max_trend | VO2max | generic.vo2MaxPreciseValue |
| race_predictions | 比赛预测 | time5K、time10K、timeHalfMarathon、timeMarathon |
| activities 与 activity_* | 活动 | 见记录流程 |

拿不到的：每日建议训练、RPE 与痛感、健身年龄（需账号年龄 20 岁以上）。

## 周报

```bash
python scripts/weekly.py
```

读完给判读，不要只贴表：

| 关注 | 判读 |
| --- | --- |
| HRV 状态 | 连续 UNBALANCED 提示降档 |
| 静息心率趋势 | 高于基线达阈值且连续两天，触发红线 |
| 比赛预测与训练锚点 | 差异大说明哪个锚点该更新 |
| 本周活动与周跑量目标 | 缺口多少，靠哪几天补 |

关键三项（sleep、heart_rate、hrv_daily）缺两项就不要出周报，直接说数据不足。

## 硬规矩

| 规矩 | 原因 |
| --- | --- |
| md 只写训练内容 | 用户在手机上是照着练 |
| 每轮先核对日期 | 曾经把今天当成两天前 |
| 日志只写已发生的事，未来日标"未来日"并留空 | 不预填未跑 |
| 低强度课要写"慢于 X 就是合格" | 新手惯性加速，需要的是许可 |
| 处方里写配速不写心率（按 preferences 配置） | 训练时看配速更可执行 |
| 强度日不排力量 | 见组装三件套 |
| 数据不许编造 | 抓不到就如实说未同步 |
| 跑完别问距离、心率、步频 | 脚本里有，只问 RPE 与有没有痛 |
| 不排定时任务 | 见被动响应 |

## 不做的事

不替用户判断"这组还能不能顶"，主观感受由用户给。

不在日志里编造未报的数据，缺就留空或标待确认。

不经用户同意改动规则库里的区间标准。
