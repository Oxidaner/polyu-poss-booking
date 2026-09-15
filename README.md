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
| `booking_step1.curl` | 第一步请求模板：选时段 → 进入确认页（已 gitignore） |
| `booking.curl` | 第二步请求模板：勾选 declare → 最终提交（已 gitignore） |
| `calibrate.py` | cURL 导入器：抓包 → 自动模板化 → 逐字段审查表 |
| `poss_client.py` | 自动登录 + 抓时间表 + 提交预订（支持 form / multipart） |
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
    "steps": [
      {"curl_file": "booking_step1.curl",
       "extract": {"csrf": "name=\"CSRFToken\"\\s+value=\"([0-9a-f-]{36})\""}},
      {"curl_file": "booking.curl"}
    ],
    "endpoint": "",
    "params": {},
    "success_keywords": ["success", "successful", "成功", "confirmed", "已确认"],
    "failure_keywords": ["unsuccessful", "not successful", "not available",
                         "already been booked", "失败", "名额已满"],
    "result_url": ""
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
| `steps` | 多步预订流程。POSS 是「选时段 → 确认页 → 最终提交」两步，每步一个 `curl_file`；<br>中间步骤可用 `extract` 从响应 HTML 提取字段（如确认页的新 CSRFToken） |
| `curl_file` | 单步预订模板文件（默认 `booking.curl`，若配置了 `steps` 则仅作为各步骤的默认回退） |
| `success_keywords` | 判定「预订成功」的结果页关键词 |
| `failure_keywords` | 判定「服务端明确拒绝」的结果页关键词（如时段已被别人抢走）。命中即换下一个候选继续抢；空数组 = 用内置默认值 |
| `result_url` | 预订结果页地址，默认 `make_book_result.do`。一般不用改 |
| `endpoint` / `params` | 遗留路径：不用 cURL 模板时手填端点与表单 |

**蹲守部分：** `fallback.targets` 不配则自动由 booking 派生（触发日 + 目标时段）。

## ⚠️ 必做：校准预订接口

查询时间表可以自己攒，但**提交预订必须照抄真实请求**——凭空猜一个会扣押金的
POST 端点是不负责任的。所以拿不到真实抓包时，代码会拒绝抢位（只做蹲守提醒）并推送警报。

POSS 的实际流程是**两步**：
1. 选中时段后点击提交 → POST `make_book.do` → 返回「确认页」（带 declare 复选框）
2. 勾选 declare 后再点击提交 → POST `make_book_submit.do` → 真正扣押金下单
3. 服务端处理完 → 302 跳到 `make_book_result.do` → **这一页才是「抢到没有」的判决书**

第 3 步不用你抓：脚本会跟着那个 302 自己走过去读结果页。

所以你需要**抓两条** cURL，并分别导入成两个模板：

```bash
# 第 1 步：选时段进入确认页
pbpaste | python3 calibrate.py --import - --out booking_step1.curl

# 第 2 步：勾选 declare 后的最终提交
pbpaste | python3 calibrate.py --import - --out booking.curl
```

`calibrate.py` 会打印逐字段审查表，并自动识别：

| 值长什么样 | 替换成 |
|---|---|
| 会话令牌 / 用户 ID / 场馆编号 / 设施编号 / 时段编号 | `{csrf}` `{fbUserId}` `{ctrId}` `{facilityId}` `{slotId}` |
| 日期、时间、日期时间（含 `18 Sep 2026 14:30` 这种） | `{date}` `{startTime}` `{startDateTime}` |
| 其余字段（`bookType`、`actvId` …） | 原样保留 |

几个设计细节：
- **multipart/form-data**（最终提交）已完整支持：boundary 不动，只替换每个 part 里的值。
- **不做「解码→重编码」**：`searchFormString` 这类字段的值内部本身含 `&`、`=`，
  解码后重新解析会把字段切碎。所以只做纯文本替换，浏览器原来的编码字节原样保留；
  需要编码的占位符会自动写成 `{key:url}`。
- **字段名带 `Facilities` 的日期时间字段**（如 `boMakeBookFacilities[0].startDateTime`）
  先按「值像不像日期时间」判定，再看字段名 —— 否则会被误判成 `{facilityId}`。

## 抢到没有？—— 结果页判定

提交后服务端会 302 到 `make_book_result.do`，那一页才是权威结论。脚本会跟过去读它，
把结果分成三种，**三种都会把结果页原文存进 `dumps/`**（Actions 里作为 `poss-dumps`
artifact 保留 14 天），判错了照原文改 `config.json` 里的关键字就行：

| 判定 | 触发条件 | 脚本动作 |
|---|---|---|
| ✅ 抢到 | 结果页命中 `success_keywords` | 记状态、推送、收工 |
| ❌ 明确没抢到 | 结果页命中 `failure_keywords` | 排除这个馆+时段，**继续试下一个候选** |
| ❓ 结果不明 | 两类关键词都没命中 | 立刻停止重试（防重复扣费）并推送让你人工确认 |

两个刻意的设计：

- **先判失败、再判成功。** 英文结果页里 `unsuccessful` 本身就含 `success` 子串，
  反过来判会把「预订失败」读成「抢到了」，然后就此收工 —— 你会以为订上了，实际没有。
  判错方向的代价不对称，所以顺序不能反。
- **比对的是去掉 `<script>`/`<style>` 后的内容。** 既不漏掉藏在隐藏字段里的结果标志，
  也不会因为某个 JS 库里恰巧出现 "success" 就把失败读成成功。
- **同一轮不重复提交已被拒的「馆+时段」。** 否则 120 秒的爆发窗口里会对着同一个
  满位时段提交几十次，除了给风控送素材毫无用处。

**验证**：`python3 monitor.py --dry-book` 会按顺序打印每一步**真正会发出去**的请求
（token 已打码），并检查占位符能否取到值。与抓包逐字段核对一致即可上线。
若提示某些占位符取不到值（典型是 `{facilityId}`），说明时间表数据里没这个字段，
先跑 `--dump` 看结构。

## 第一步：本地校准与试跑

```bash
cp .env.example .env      # 填入 POSS_NETID / POSS_PASSWORD（只写本地，别贴聊天）
pip3 install -r requirements.txt
# 抓两条 cURL 分别导入：
python3 calibrate.py --import - --out booking_step1.curl   # 选时段进入确认页
python3 calibrate.py --import - --out booking.curl         # 勾选 declare 最终提交
python3 monitor.py --dump         # 抓真实时间表结构 → dumps/，并检查占位符能否填满
python3 monitor.py --dry-book     # 演练两步流程并打印真实请求，不真提交
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
   **Secrets**：
   - `POSS_NETID`、`POSS_PASSWORD`
   - `BOOKING_CURL_STEP1`：把 `booking_step1.curl` 的**全文**粘进去
   - `BOOKING_CURL_STEP2`：把 `booking.curl` 的**全文**粘进去（两步流程）
   - 如果你只配了单步模板，用 `BOOKING_CURL` 即可
   - `PUSHPLUS_TOKEN`、`SERVERCHAN_KEY`、`SMTP_HOST`、`SMTP_USER`、`SMTP_PASS`、`MAIL_TO`
   **Variables**（可选）：`POSS_FB_USER_ID`（默认 657032）、`POSS_CONFIG`（整段 JSON，在线覆盖配置）、
   `BOOKING_CURL_FILE_STEP1` / `BOOKING_CURL_FILE_STEP2`（用文件路径而非 Secret 全文）
3. Actions 页启用 workflow。定时任务（UTC，香港时间 = UTC+8）：
   - `*/5 * * * *` 常规轮询（蹲守提醒）
   - `29 0 * * *` = 香港 08:29 预热，准点 08:30:00 开火
   - `35 0 * * *` = 香港 08:35 兜底重试

## 注意事项 / 风险

- **会自动花钱**：预订成功会扣押金。代码做了三层防护：同一触发日只抢一次（成功后跳过）；
  结果页读不出结论时立刻停止重试并推送让你人工确认（绝不盲重试）；
  模板里有取不到值的占位符时直接拒绝发送。
  只有服务端**明确**回了失败（如时段已被抢走）才会继续试下一个候选 —— 因为那说明钱没花出去。
  但**请自己核对 POSS 上的预订记录**。
- **`facilityId` 从时间表数据来**：POSS 的预订请求需要场馆内部的设施编号
  （`boMakeBookFacilities[0].facilityId`），这个值必须来自 timetable.json。
  解析器会继承父节点的字段，所以设施编号挂在分组节点上也能取到；
  但若 `--dry-book` 报「占位符取不到值」，就说明真实结构还没对齐，需要先 `--dump`。
- **预订完成后请手动确认**：若 POSS 还有「确认预订」步骤或支付环节，记得去做。
- **频率与风控**：常规轮询 5 分钟一次、会话缓存复用约 90 分钟，抢位仅集中在触发时刻。
- **风险自担**：自动化访问学校系统可能违反 IT 使用守则；密码仅存 GitHub Secrets（加密），
  代码与日志不落明文；`session.json` 含短期会话 cookie，仅存于你的私有仓库。
