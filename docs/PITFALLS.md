# 踩坑记录

四个走过的错路，以及留下的四个后遗症。

## 1 直连全球区拿不到数据

现象：用现成的开源工具，认证通过、请求全 200，但拿不到数据，字段全空。

原因：国区账号在 connect.garmin.cn，与全球区 connect.garmin.com 是两套系统，账号体系与端点前缀都不同。

处理：只认 connect.garmin.cn 与 /gc-api 前缀。国际区账号不支持。

国区与国际区的端点差异程度未实测，不要照搬全球区文档。

## 2 storage_state 加无痕上下文一律 403

现象：导出的 storage_state.json 看起来正常，几百 KB、几十条 cookie，但请求全部 403。

试过无效的做法：`--disable-blink-features=AutomationControlled`、去掉 `--enable-automation`、改 User-Agent、headed 模式、加随机延时。

原因：无痕上下文没有历史、没有本地存储痕迹，指纹与真实浏览器不一致，会被识别。

处理：放弃这条路，改为接管已登录的真实浏览器。storage_state 模式只保留用于排障。

## 3 复制 Edge profile 后 cookie 解不出来

现象：把 Edge 的 User Data 复制到项目下，cookie 数据库还在，但能解出的只有 15 条，原来有 269 条。

原因：Edge 与 Chrome 的 ABE 把 cookie 加密密钥绑定到进程族与安装路径，换 user_data_dir 就解不出，且静默失败不报错。

处理：不复制 profile。要么接管真实进程，要么用 `--real-profile` 直启（需先关闭所有 Edge）。

因此 `login.py --from-edge` 属于遗留代码，已经失效。

## 4 手写 fetch 只带 Accept 会 403

现象：用 requests 或页面内手写 fetch，只带 Accept 头，403；应用自己发的请求 200。

原因：应用发请求带 8 个请求头，其中 Connect-Csrf-Token 是关键，另外还有自定义头与同源校验。

处理：页面加载前注入脚本 hook fetch 与 XHR，抄下应用自己发的请求头，再用页面内 fetch 重发。请求头只存在于内存与 `window.__ghdr`，不落盘、不回显。

## 后遗症 1 默认模式落在已知 403 的路

原代码不带参数时走 storage 模式，而那条路已知 403，文档示例却全带 `--cdp`，新人第一条命令必踩。已改为默认走 CDP，要旧模式必须显式 `--storage`。

## 后遗症 2 level 2 静默截断

单次运行上限 6 个请求，而 level 0 与 1 合计就有 8 个端点，排序靠后的会被丢弃，只打印"已达上限"。要全量请分批用 `--only` 点名。

## 后遗症 3 weekly.py 裸连 CDP

浏览器没带调试端口时直接抛 Playwright traceback。已改为捕获异常并提示先跑 `fetch.py --cdp --probe`。

## 后遗症 4 调试端口常开的风险

`--remote-debugging-port` 对本机任意进程无鉴权，本地任何程序都能借它读取所有站点 cookie、代你操作网页。抓完关掉窗口，不要做成开机常驻，不要在共享机器上使用。

## 端点失效怎么办

`/gc-api` 是前端内部接口，改版就会变。自查流程：

```powershell
python scripts/sniff.py
# 看 scripts/raw/sniff_*.json 里 /gc-api 的 200 请求
# 按真实路径改 scripts/endpoints.py
python scripts/fetch.py --cdp --probe
```

`endpoints.py` 里记录了三处历史校正，不要改回去：

| 曾经写错 | 实测正确 |
| --- | --- |
| /gc-api/graphql | /gc-api/graphql-gateway |
| personal-information | personal-information/{guid} |
| usersummary/daily/{display_name} | usersummary/daily/{guid} |

## 字段坑

| 端点 | 注意 |
| --- | --- |
| activity_detail | 数据在 summaryDTO |
| activity_splits | 必须 `?splitType=KM`，数据在 lapDTOs；不带参数得到的是自动分段 |
| activity_hr_zones | 结构是 [{zoneNumber, secsInTimeZone, zoneLowBoundary}] |
| lactate_threshold | 字段名原文 `hearRate`（少一个 t）；speed 乘 10 才是 m/s |
| user_profile_base | displayName 是 UUID，等于 URL 里的 GUID；userName 反而是手机号 |
| gear | 可能返回空数组（未录装备），不是错误 |
| 健身年龄 | 需账号年龄 20 岁以上，否则 invalidReason 为 USER_UNDER_AGE |
| 每日建议训练 | 前端未暴露，不要找 |
