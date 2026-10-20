# garmin-cn-coach

从佳明中国区（connect.garmin.cn）读出本人账号的训练与健康数据，存成 JSON，交给 AI 按自己的规则生成每日跑步课表。

不绕过授权：接管的是你自己已登录的浏览器，读的是你自己本来就看得见的数据。

Windows + Edge 实测通过。macOS 与 Linux 分支未实测。

## 问题

Garmin Coach 的处方不可改，强度对新手偏严，跑崩了也不会自动降档。Connect 网页上数据看得见但拿不出来。官方 Health API 要企业申请，个人拿不到。

本项目只负责两件事：把数据读成本地 JSON；把"今天怎么跑"的规则交给使用者自己定。

## 机制

```
已登录 connect.garmin.cn 的 Edge（带 --remote-debugging-port=9222）
  Playwright connect_over_cdp 接管该浏览器
  注入 init script，页面加载前 hook fetch 与 XHR
  页面自己发出的 /gc-api 请求会带上完整请求头（关键在 Connect-Csrf-Token），抄下来
  用页面内的 fetch 重发请求，浏览器指纹保持真实
  结果写入 scripts/raw/<端点>_<时间戳>.json，不上传任何地方
```

四个失败过的做法，对应四个原因：

| 做法 | 结果 |
| --- | --- |
| 用开源工具直连全球区 | 国区账号数据不在这套系统里 |
| 导出 storage_state 开无痕上下文 | Cloudflare 一律 403，改 UA 与去 automation 标记均无效 |
| 复制 Edge profile 到别的目录 | Edge 的 ABE 把 cookie 密钥绑在进程族上，换目录解不出（269 条只剩 15 条） |
| 手写 fetch 只带 Accept | 403，应用自己发请求带 8 个请求头 |

细节见 [docs/PITFALLS.md](docs/PITFALLS.md)。

## 安装

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts/doctor.py
```

不需要 `playwright install` 下载浏览器，用的是系统已装的 Edge。

## 使用

```powershell
# 首次：用调试端口启动 Edge，在里面登录一次
start-edge-debug.cmd

# 验证通路，1 个请求
python scripts/fetch.py --cdp --probe

# 每日素材：睡眠、心率、HRV、日汇总，1 次整页加载加 5 个 API 请求
python scripts/fetch.py --cdp --morning
```

其余命令：

| 用途 | 命令 | 请求数 |
| --- | --- | --- |
| 只看计划不联网 | `python scripts/fetch.py --dry` | 0 |
| 每周补慢变量 | `python scripts/fetch.py --cdp --only hr_zones,lactate_threshold,devices` | 4 |
| 指定某天 | `python scripts/fetch.py --cdp --only sleep --date 2026-10-19` | 2 |
| 活动详情 | 先 `--only activities` 取 activityId，再 `--only activity_detail,activity_splits,activity_hr_zones --activity <ID>` | 4 |
| 数据周报 | `python scripts/weekly.py` | 14 |

请求间隔 3~6 秒随机，单次最多 6 个 API 请求，非 200 立即停止不重试。不要改成并发。

`--level 2` 想一次抓完 26 个端点会被上限截断，会打印"已达上限"。要全量请分批用 `--only` 点名。

## 端点

`scripts/endpoints.py` 内含 26 个端点，全部 REST。字段容易踩错的地方：

| 端点 | 注意 |
| --- | --- |
| user_profile_base | 引导端点，先跑。`displayName` 是 UUID 形态，就是 URL 里的 GUID |
| activity_detail | 数据在 `summaryDTO`，不在顶层 |
| activity_splits | 必须带 `?splitType=KM`，数据在 `lapDTOs`；不带参数得到的是自动分段，不能当计圈 |
| lactate_threshold | 字段名原文就是 `hearRate`（少一个 t）；`speed` 乘 10 才是 m/s |
| hr_zones | 含 `maxHeartRateUsed` 与 `trainingMethod`，比手填的可靠 |

拿不到的项：每日建议训练（前端未暴露）、RPE 与痛感（任何接口都没有）、健身年龄（需账号年龄 20 岁以上）。

## 目录

```
start-edge-debug.cmd          用调试端口启动 Edge
scripts/fetch.py              抓取主入口
scripts/endpoints.py          端点表
scripts/weekly.py             数据周报
scripts/sniff.py              端点失效时重新嗅探
scripts/login.py              遗留代码，ABE 之后已失效
scripts/doctor.py             环境自检
skills/runplan/SKILL.md       给 AI 用的技能文件，不含个人数据
rules/athlete.example.yaml    个人档案模板，复制成 athlete.yaml
rules/training-rules.example.md  课型库与决策树模板，复制成 training-rules.md
docs/PITFALLS.md              踩坑记录
INSTALL-PROMPT.md             给 AI 用的部署步骤
```

两个模板都要复制，缺前者 AI 不会开强度课，缺后者只给评估不给处方。

## 环境变量

项目不读 `.env` 文件，直接在 shell 里设置：

```powershell
$env:GARMIN_CDP_PORT = "9222"
$env:GARMIN_REPORT   = "D:\notes\garmin_weekly.md"
$env:GARMIN_USER_DATA = "C:\Users\you\AppData\Local\Microsoft\Edge\User Data"
```

| 变量 | 默认 | 使用者 |
| --- | --- | --- |
| GARMIN_CDP_PORT | 9222 | fetch.py、weekly.py、doctor.py |
| GARMIN_REPORT | scripts/reports/garmin_weekly.md | weekly.py |
| GARMIN_USER_DATA | 系统默认 Edge 目录 | fetch.py、sniff.py |

## 安全

`storage_state.json`、`_edge_profile_copy/`、`_edge_backup/` 等价于一个已登录浏览器，不要提交、上传、分享、贴 issue。默认的 CDP 模式不产生这些文件。

调试端口对本机任意进程无鉴权，任何本地程序都能借它读取所有站点的 cookie、代你操作网页。抓完关掉那个 Edge 窗口，不要做成开机常驻，不要在共享机器上运行。

## 合规

本项目与 Garmin 无关联，未获授权。只在本人账号与本人浏览器上运行，不支持多账号或批量抓取。`/gc-api` 是前端内部接口，Garmin 可随时变更，按现状提供。自动化访问可能违反用户协议并导致账号受限，风险自负。输出仅为训练参考，不构成医疗建议。

不要上传登录态，不要多账号批量抓取，不要后台常驻高并发抓取，不要把 raw/ 或周报推到公开仓库，不要包装成对外收费服务。

## 平台

| 平台 | 状态 |
| --- | --- |
| Windows 11 + Edge | 实测可用 |
| macOS + Edge | 未实测。自动拉起浏览器只认 Windows 路径，需手动带端口启动 |
| Linux | 未实测，同上 |
| 国际区账号 | 不支持，端点前缀与 GUID 语义不同 |

MIT，见 [LICENSE](LICENSE)。
