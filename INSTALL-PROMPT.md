# 部署指令（给 AI 助手）

把下面的内容整段交给 Claude Code、Codex、Cursor 之类的编程助手。它应当每完成一步就汇报并等待确认。

---

## 你的角色

在目标机器上把 garmin-cn-coach 装到能真实抓到一次数据，并把个人档案建好。只做环境、机械、配置类工作，训练内容由使用者决定。

## 硬规矩

1. 只操作使用者本人的一个账号，不做多账号或批量。
2. 不读取、复制、上传、回显 `storage_state.json`、`_edge_profile_copy/`、`_edge_backup/` 或任何 cookie 文件的内容。
3. 不执行 git push。
4. 不创建定时任务抓数据。
5. 任何一步失败，停下并原样粘贴报错，不要自行重试，尤其不要连续重试（会触发风控）。

## 步骤

### 1 环境盘点

- `python --version`，需要 3.9+
- 系统是否装了 Microsoft Edge
- 9222 端口是否被占用

产出一张三行的表。

### 2 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

验收：`python -c "import playwright, importlib.metadata as m; print(m.version('playwright'))"`。

不需要 `playwright install`，本项目用系统已装的 Edge。

### 3 自检

```powershell
python scripts/doctor.py
```

验收：无 FAIL。有 WARN 按提示处理。

### 4 首次抓取

先让使用者运行 `start-edge-debug.cmd`，在弹出的 Edge 里登录 connect.garmin.cn。然后：

```powershell
python scripts/fetch.py --cdp --probe
```

预期：打印 HTTP 200，`scripts/raw/` 下出现 `user_profile_base_*.json`。

| 报错 | 原因与处理 |
| --- | --- |
| 拿不到可用的调试端口 | Edge 已在运行且启动时没带调试开关。关掉所有 Edge 窗口重跑；若进程仍在，用任务管理器结束 msedge.exe，或改跑 start-edge-debug.cmd |
| 403 | 没抄到 Connect-Csrf-Token。确认已登录后重跑一次；连续两次就停手上报 |
| 404 | 端点变了。跑 `python scripts/sniff.py` 重新嗅探，按真实流量改 `scripts/endpoints.py` |

### 5 抓每日素材

```powershell
python scripts/fetch.py --cdp --morning
```

验收：`scripts/raw/` 下出现 sleep、heart_rate、hrv_daily、daily_summary 四类 JSON，且 sleep 文件大于 10 KB。

### 6 建个人档案

```powershell
cp rules/athlete.example.yaml rules/athlete.yaml
cp rules/training-rules.example.md rules/training-rules.md
```

两个都要。缺 athlete.yaml 无法开强度课，缺 training-rules.md 只能给评估不给处方。

逐项询问使用者，没答案就留 null，不要用"合理默认值"填：

| 字段 | 需要提供 | 缺失时 |
| --- | --- | --- |
| `physiology.hr_max` | 最大心率 | 留空，或从 hr_zones 的 maxHeartRateUsed 取，标 `hr_max_source: garmin` |
| `physiology.lactate_threshold` | 阈值心率与配速 | 手表测过才有，没有就留空 |
| `pace_prescription` | 五档训练配速 | 使用者给，或按近期成绩推算并注明来源 |
| `constraints` | 每周跑几天、哪天不跑、单次上限、有无健身房 | 必须问，这是安全边界 |
| `equipment` | 家里有什么器材 | 没有就写空数组 |
| `redlines` | 睡眠与静息心率红线 | 用模板默认值，念给使用者确认 |

完成后把 athlete.yaml 里所有 null 列一张表，说明哪些会导致当天不能安排强度课。

### 7 接入 AI（可选）

把 `skills/runplan/SKILL.md` 复制到使用者的 agent 技能目录。先问清用的是哪个框架。不用框架的话说明：把抓到的 JSON 和 athlete.yaml 一起交给任意聊天模型也能出课表。

### 8 汇报

| 项 | 内容 |
| --- | --- |
| Python 与 playwright 版本 | |
| doctor 结果 | |
| 探测抓取 | 状态码、落盘文件 |
| 每日素材 | 四个端点是否齐全 |
| athlete.yaml 待填项 | 几项 null |
| 建议下一步 | |

结束时提醒三点：抓完关掉那个 Edge 窗口；raw/ 是健康数据，给别人看前先裁剪；端点会失效，失效就跑 sniff.py。

---

## 说明

不提供一键脚本的原因：机器上的差异都在环境（Edge 版本、端口占用、Python 环境、是否已登录），解决方式需要判断。写成固定脚本会掩盖失败，写成给助手的指令更稳。

需要人拍板的只有两处：档案里的数字与约束条件。这两处必须问，不许编。
