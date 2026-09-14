# POSS 抢位器 / 蹲位器 🏀

自动登录 PolyU POSS，在触发日（默认**周二、周四 08:30**）准点帮你抢**下周同一天**
（默认 7 天后）的目标时段（默认 **16:30–17:30**）；没抢到就自动转入**蹲守模式**，
有人取消放出空位立即 微信推送 + 邮件提醒。

## 工作原理

```
触发日 08:29  → 预热运行，脚本原地等到 08:30:00（不听 GitHub 调度的延迟）
触发日 08:30  → 爆发式抢位（默认持续 120 秒，每 3 秒一轮）：
                   登录 → 查目标日期时间表 → 找 16:30–17:30 的空位 → 立即提交预订
                   馆顺序依次尝试（X123 → X202，可配）
     抢到      → 推送「🎉 抢到了」（当天不再重复抢，防重复扣费）
     没抢到    → 推送「😤 转入蹲守」+ 每 5 分钟轮询，出现空位立即提醒
非触发日      → 只做蹲守提醒
```

## 文件结构

| 文件 | 作用 |
|---|---|
| `config.json` | **抢位时间/目标时段/馆列表/蹲守目标 全部可配** |
| `booking.curl` | **预订请求模板**（由 `calibrate.py` 从抓包生成，已 gitignore） |
| `calibrate.py` | cURL 导入器：抓包 → 自动模板化 → 逐字段审查表 |
| `poss_client.py` | 自动登录 + 抓时间表 + 提交预订 |
| `monitor.py` | 主程序（抢位引擎 + 蹲守提醒 + 校准模式） |
| `.github/workflows/monitor.yml` | GitHub Actions 定时任务 |
| `state.json` / `session.json` | 运行状态与会话缓存（Actions 自动回写） |
| `dumps/` | 校准数据（本地调试用，已 gitignore） |

## 配置说明（config.json）

```json
{
  "gyms": [
    {"name": "X123", "ctrId": "151"},
    {"name": "X202", "ctrId": "152"}
  ],
  "booking": {
    "enabled": true,
    "trigger_days": [1, 3],
    "trigger_time": "08:30",
    "warmup_minutes": 6,
    "grace_minutes": 30,
    "lead_days": 7,
    "burst_seconds": 120,
    "retry_interval": 3,
    "gyms": ["X123", "X202"],
    "from": "16:30",
    "to": "17:30",
    "search_date_format": "%Y-%m-%d",
    "curl_file": "booking.curl",
    "endpoint": "",
    "params": {},
    "success_keywords": ["success", "successful", "成功", "已确认"]
  },
  "fallback": {
    "enabled": true,
    "targets": [{"days": [1, 3], "from": "16:30", "to": "17:30"}]
  },
  "request": {
    "bookType": "INDV", "dataSetId": "18", "actvId": "101",
    "facilityId": "", "showCourtAreaDetails": "true", "searchDates": []
  }
}
```

**抢位部分：**

| 字段 | 含义 |
|---|---|
| `trigger_days` | 哪几天开抢。`0`=周一 … `6`=周日。默认 `[1, 3]` = 周二、周四 |
| `trigger_time` | 开抢时刻（香港时间），默认 `08:30` |
| `lead_days` | 抢几天后的场：`7` = 下周同一天 |
| `from` / `to` | 目标时段，默认 `16:30`–`17:30`。时段与窗口有交集即为候选 |
| `gyms` | 尝试顺序；空数组 = 按顶层 `gyms` 顺序全部试 |
| `burst_seconds` / `retry_interval` | 爆发时长 / 每轮间隔秒数 |
| `warmup_minutes` | 提前多少分钟到达就原地等待（默认 6 分钟） |
| `grace_minutes` | 开抢后多少分钟内仍积极尝试（默认 30 分钟） |
| `curl_file` | 预订请求模板文件（默认 `booking.curl`，见下节） |
| `success_keywords` | 判定「预订成功」的响应关键词（默认含 `success`/`成功`） |
| `endpoint` / `params` | 遗留路径：不用 cURL 模板时手填端点与表单 |

**蹲守部分：** `fallback.targets` 不配则自动由 booking 派生（触发日 + 目标时段）。

## ⚠️ 必做：校准预订接口（一条命令）

查询时间表可以自己攒，但**提交预订必须照抄真实请求**——凭空猜一个会扣押金的
POST 端点是不负责任的。所以拿不到真实抓包时，代码会拒绝抢位（只做蹲守提醒）并推送警报。

**你只需要做两件事：**

**① 抓包**：登录 POSS → 操作到「提交预订」那一步 → F12 → Network →
找到点击提交后的那条请求 → 右键 **Copy as cURL**。

**② 导入**（不用手填任何字段）：
```bash
pbpaste | python3 calibrate.py --import -        # macOS，直接从剪贴板读
# 或：python3 calibrate.py --import booking_raw.txt
```
它会打印一张逐字段审查表，并写出 `booking.curl`。识别规则：

| 值长什么样 | 替换成 |
|---|---|
| 会话令牌 / 用户 ID / 场馆编号 / 设施编号 / 时段编号 | `{csrf}` `{fbUserId}` `{ctrId}` `{facilityId}` `{slotId}` |
| 日期、时间、日期时间（含 `18 Sep 2026 14:30` 这种） | `{date}` `{startTime}` `{startDateTime:url:%d %b %Y %H:%M}` |
| 其余字段（`bookType`、`actvId` …） | 原样保留 |

两个设计细节，都不是可选项：
- **不做「解码→重编码」**：`searchFormString` 这类字段的值内部本身含 `&`、`=`，
  解码后重新解析会把字段切碎。所以只做纯文本替换，浏览器原来的编码字节原样保留；
  需要编码的占位符会自动写成 `{key:url}`。
- **字段名带 `Facilities` 的日期时间字段**（如 `boMakeBookFacilities[0].startDateTime`）
  先按「值像不像日期时间」判定，再看字段名 —— 否则会被误判成 `{facilityId}`。

**③ 验证**：`python3 monitor.py --dry-book` —— 打印脚本**真正会发出去**的那条请求
（token 已打码），并检查每个占位符能否取到值。与抓包逐字段核对一致即可上线。
若提示某些占位符取不到值（典型是 `{facilityId}`），说明时间表数据里没这个字段，
先跑 `--dump` 看结构。

## 第一步：本地校准与试跑

```bash
cp .env.example .env      # 填入 POSS_NETID / POSS_PASSWORD（只写本地，别贴聊天）
pip3 install -r requirements.txt
python3 calibrate.py --import -   # 粘贴预订请求 cURL → 生成 booking.curl
python3 monitor.py --dump         # 抓真实时间表结构 → dumps/，并检查占位符能否填满
python3 monitor.py --dry-book     # 演练抢位并打印真实请求，不真提交
```

## 第二步：配置通知渠道

- **微信推送（PushPlus，免费）**：扫码登录 https://www.pushplus.plus 并关注公众号，
  「一对一推送」复制 TOKEN → `.env` 的 `PUSHPLUS_TOKEN`
- **邮件（QQ 邮箱示例）**：设置 → 账户 → 开启 SMTP → 生成授权码 →
  填 `SMTP_HOST=smtp.qq.com`、`SMTP_PORT=465`、`SMTP_USER`、`SMTP_PASS`、`MAIL_TO`

## 第三步：部署 GitHub Actions

1. 新建 **Private** 仓库（必须私有！）并推送代码
2. Settings → Secrets and variables → Actions：
   **Secrets**：`POSS_NETID`、`POSS_PASSWORD`、`PUSHPLUS_TOKEN`、
   `SMTP_HOST`、`SMTP_USER`、`SMTP_PASS`、`MAIL_TO`
   **`BOOKING_CURL`**：把 `booking.curl` 的**全文**粘进去（预订模板走 Secret 而不是
   提交进仓库，避免会话凭据痕迹入库）
   **Variables**（可选）：`POSS_FB_USER_ID`（默认 657032）、`POSS_CONFIG`（整段 JSON，在线覆盖配置）
3. Actions 页启用 workflow。定时任务（UTC，香港时间 = UTC+8）：
   - `*/5 * * * *` 常规轮询（蹲守提醒）
   - `29 0 * * *` = 香港 08:29 预热，准点 08:30:00 开火
   - `35 0 * * *` = 香港 08:35 兜底重试

## 注意事项 / 风险

- **会自动花钱**：预订成功会扣押金。代码做了三层防护：同一触发日只抢一次（成功后跳过）；
  响应是「结果不明」的跳转时立刻停止重试并推送让你人工确认（绝不重试第二次）；
  模板里有取不到值的占位符时直接拒绝发送。
  但**请自己核对 POSS 上的预订记录**。
- **`facilityId` 从时间表数据来**：POSS 的预订请求需要场馆内部的设施编号
  （`boMakeBookFacilities[0].facilityId`），这个值必须来自 timetable.json。
  解析器会继承父节点的字段，所以设施编号挂在分组节点上也能取到；
  但若 `--dry-book` 报「占位符取不到值」，就说明真实结构还没对齐，需要先 `--dump`。
- **预订完成后请手动确认**：若 POSS 还有「确认预订」步骤或支付环节，记得去做。
- **频率与风控**：常规轮询 5 分钟一次、会话缓存复用约 90 分钟，抢位仅集中在触发时刻。
- **风险自担**：自动化访问学校系统可能违反 IT 使用守则；密码仅存 GitHub Secrets（加密），
  代码与日志不落明文；`session.json` 含短期会话 cookie，仅存于你的私有仓库。
