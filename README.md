# POSS 抢位器 / 蹲位器 🏀

自动登录 PolyU POSS，在放位日（默认**目标日前 7 天的 08:30**）准点帮你抢目标时段；
没抢到就自动转入**蹲守模式**，有人取消放出空位立即 微信推送 + 邮件提醒。

**所有配置都在本地网页里改**，不用手编 JSON：

```bash
python3 poss_ui.py        # 自动打开 http://127.0.0.1:8765
```

## UI 里能配什么

| 面板 | 能改什么 |
|---|---|
| **登录与通知** | POSS 的 NetID / NetPassword、fbUserId；PushPlus / Server酱 / 邮件渠道，还能一键发测试通知 |
| **选目标** | Activity / Center / Category / Date 四个下拉框（直接抓 POSS 页面上的），点格子选时段，可多选 |
| **抢位目标** | 每个目标的日期、时段、场馆、活动；每个都显示自己的**放位日** |
| **设置 · 常用** | 自动抢位开关、蹲守开关、放位时刻、提前几天抢、预热 / 宽限 / 爆发时长 / 重试间隔 |
| **设置 · 高级** | 触发日、默认时段、日期格式、场馆列表、请求参数、成功 / 失败关键词、结果页地址、蹲守目标、预订流程 |
| **设置 · 预订模板** | 粘贴两条抓包 cURL 直接导入（写进 `.curl` 文件，**不进 config.json**） |

面板顶部会直接告诉你两件事，免得你以为配好了其实没配：

- **账号配了没** —— 没配就什么都干不了；
- **预订模板导入了没** —— 没导入就只能蹲守提醒、不会真下单。

## 凭据放在哪（重要）

| 内容 | 存哪 | 会不会进仓库 |
|---|---|---|
| NetID / NetPassword / fbUserId | `.env`（UI 写入，权限 0600） | ❌ 已 gitignore |
| PushPlus / Server酱 / SMTP 凭据 | 同上 | ❌ |
| 抢位目标、时段、关键词等参数 | `config.json` | ✅ 要提交，所以里面**不含凭据** |
| 抓包得到的预订模板 | `booking_step1.curl` / `booking.curl` | ❌ 已 gitignore |

UI 只绑 `127.0.0.1`；**密码永不回传给浏览器**，页面上只显示「已设置 / 未设置」。
密码框留空 = 不改（不然你每次调通知渠道都会把密码清掉）。

部署到 Actions 时凭据走 **Secrets**（`POSS_NETID` / `POSS_PASSWORD`），
`.env` 只服务本地。

## 工作原理

```
放位日 08:29  → 预热运行，脚本原地等到 08:30:00（不听 GitHub 调度的延迟）
放位日 08:30  → 爆发式抢位（默认持续 120 秒，每 3 秒一轮）：
                   登录 → 查目标日期时间表 → 找目标时段的空位 → 立即提交预订
                   一个目标抢到就继续抢下一个（想抢几节就配几个目标）
     抢到      → 推送「🎉 抢到了」（该目标当天不再重复抢，防重复扣费）
     没抢到    → 推送「😤 转入蹲守」+ 每 5 分钟轮询，出现空位立即提醒
非放位日      → 只做蹲守提醒
```

> **放位日**：POSS 只放「今天起 8 天」的场，所以想抢 `D` 那天的场，就得在 `D − 7` 天
> 的 08:30 去抢。配了抢位目标后，脚本按每个目标各自的放位日决定要不要干活 ——
> 也就是说放位日不一定落在周二/周四，配什么日子就什么时候抢。UI 里每个目标都会标出它的放位日。

> 不想开网页也行：`config.json` 仍然是唯一真相来源，UI 只是它的编辑器。
> 用 `booking.targets` 明确列出目标，或什么都不写走「今天 + 提前天数」的派生逻辑。

## 文件结构

| 文件 | 作用 |
|---|---|
| `poss_ui.py` | **本地控制台**：选目标 + 调所有设置 + 导入 cURL，写回 config.json |
| `config.json` | **抢位时间/目标/馆列表/活动/蹲守目标 全部可配** |
| `booking_step1.curl` | 第一步请求模板：选时段 → 进入确认页（已 gitignore） |
| `booking.curl` | 第二步请求模板：勾选 declare → 最终提交（已 gitignore） |
| `calibrate.py` | cURL 导入器：抓包 → 自动模板化 → 逐字段审查表 |
| `poss_client.py` | 自动登录 + 抓时间表 + 提交预订（支持 form / multipart） |
| `monitor.py` | 主程序（抢位引擎 + 蹲守提醒 + 校准模式） |
| `.github/workflows/monitor.yml` | GitHub Actions 定时任务 |
| `state.json` / `session.json` | 运行状态与会话缓存（Actions 自动回写） |
| `dumps/` | 校准数据 + 预订结果页原文（已 gitignore） |

## 用 UI 配好一切（推荐）

```bash
pip3 install -r requirements.txt
python3 poss_ui.py            # 自动打开 http://127.0.0.1:8765
```

**⓪ 填账号**

页面最上面就是「登录与通知」。填 NetID 和 NetPassword，点「保存并登录」——
凭据写进 `.env`（自动 `chmod 600`、已 gitignore），**不会进 config.json，也不会回显到页面上**。
顺手把通知渠道也填了，点「发一条测试通知」验证真的能收到。

（习惯命令行的话：`cp .env.example .env` 然后自己填也行。）

**① 选目标**

1. **Activity / Center / Category / Date** 四个下拉框直接从 POSS 的 `make_book.do` 页面抓下来 ——
   不是写死的常量，POSS 有什么就有什么。
2. 点「读取时间表」→ 画出 `日期 × 时段` 的彩色网格（灰=不可用，洋红=$0+可退押金，蓝=收费）。
3. **点格子选中要抢的时段**（可以选多个）。灰色的格子也能点 —— 现在不可订 ≠ 放位那一刻不可订，
   抢位目标本来就是给未来定的。
4. 「保存目标」→ 写进 `booking.targets`，每个目标会显示它自己的**放位日**。

**② 调设置**

「设置 · 常用」里改开关、放位时刻、提前几天、爆发时长等；更细的都在「设置 · 高级设置」里。
改完点「保存设置」。**校验不过不会写盘** —— 坏配置进不了仓库。

**③ 导入预订模板**

「设置 · 高级设置 → 预订模板」里把两条抓包 cURL 粘进去点导入。顶部状态会显示
`✓ 预订模板齐了` 或 `✗ 模板不全 —— 现在只会蹲守提醒，不会真抢`。
模板写进 `.curl` 文件（已 gitignore），**不会写进 config.json**。

写入的配置长这样，直接可以提交到仓库给 Actions 用：

```json
"targets": [
  {"ctrId": "151", "name": "Block X - X123 Fitness Room",
   "date": "2026-10-14", "from": "16:30", "to": "17:30",
   "activity": "Badminton 羽毛球", "actvId": "101", "dataSetId": "18"}
]
```

> UI 只绑 `127.0.0.1`，局域网里别人访问不到；凭据只从 `.env` 读，不落盘。
> 下拉框认不出来时页面会直接把抓到的名字列出来（如 `someField(other)`），照那个名字改
> `poss_client._ROLE_HINTS` 就行。

## 配置说明（config.json）

> 下面这些**都能在 UI 里改**，这一节是给想直接看文件的人看的。

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
    "targets": [],
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
| `gyms` | 尝试顺序；空数组 = 按顶层 `gyms` 顺序全部试（**仅派生模式用**） |
| `targets` | **显式抢位目标**（UI 生成）。非空时不再走「今天+7天」的派生逻辑，改按每个目标各自的放位日触发。<br>每项：`{ctrId, name, date, from, to, activity, actvId, dataSetId}` |
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

所以你需要**抓两条** cURL，并分别导入成两个模板。两条路选一条：

**在 UI 里导入（推荐）** —— 展开「设置 → 高级设置 → 预订模板」，把 cURL 粘进对应的框，点导入。

**命令行导入：**

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
| ✅ 抢到 | 结果页命中 `success_keywords` | 记状态、推送，**继续抢下一个目标** |
| ❌ 明确没抢到 | 结果页命中 `failure_keywords` | 排除这个馆+时段，继续试其他候选 |
| ❓ 结果不明 | 两类关键词都没命中 | **立刻全部停手**（防重复扣费）并推送让你人工确认 |

三个刻意的设计：

- **先判失败、再判成功。** 英文结果页里 `unsuccessful` 本身就含 `success` 子串，
  反过来判会把「预订失败」读成「抢到了」，然后就此收工 —— 你会以为订上了，实际没有。
  判错方向的代价不对称，所以顺序不能反。
- **比对的是去掉 `<script>`/`<style>` 后的内容。** 既不漏掉藏在隐藏字段里的结果标志，
  也不会因为某个 JS 库里恰巧出现 "success" 就把失败读成成功。
- **结果不明时立刻全线停手。** 抢到一个不会停（那是你要的多节），但一旦结果读不出来，
  再往下提交就是在赌押金了 —— 这时宁可停下来让人去看一眼。
- **同一轮不重复提交已被拒的「馆+时段」。** 否则 120 秒的爆发窗口里会对着同一个
  满位时段提交几十次，除了给风控送素材毫无用处。

**验证**：`python3 monitor.py --dry-book` 会按顺序打印每一步**真正会发出去**的请求
（token 已打码），并检查占位符能否取到值。与抓包逐字段核对一致即可上线。
若提示某些占位符取不到值（典型是 `{facilityId}`），说明时间表数据里没这个字段，
先跑 `--dump` 看结构。

## 第一步：本地校准与试跑

```bash
pip3 install -r requirements.txt
python3 poss_ui.py        # 起本地控制台：账号 + 选目标 + 调设置 + 导入 cURL，全在这一个页面里
python3 monitor.py --dump         # 抓真实时间表结构 → dumps/，并检查占位符能否填满
python3 monitor.py --dry-book     # 演练两步流程并打印真实请求，不真提交
```

（不想开网页时，账号可以 `cp .env.example .env` 手填，cURL 可以
`python3 calibrate.py --import - --out booking_step1.curl`。）

## 第二步：配置通知渠道

- **微信推送（PushPlus，免费）**：扫码登录 https://www.pushplus.plus 并关注公众号，
  「一对一推送」复制 TOKEN → `.env` 的 `PUSHPLUS_TOKEN`
- **邮件（QQ 邮箱示例）**：设置 → 账户 → 开启 SMTP → 生成授权码 →
  填 `SMTP_HOST=smtp.qq.com`、`SMTP_PORT=465`、`SMTP_USER`、`SMTP_PASS`、`MAIL_TO`

## 第三步：部署 GitHub Actions

1. 新建 **Private** 仓库（必须私有！）并推送代码
2. Settings → Secrets and variables → Actions：
   **Secrets**：
   - `POSS_NETID`、`POSS_PASSWORD`
   - `BOOKING_CURL_STEP1`：把 `booking_step1.curl` 的**全文**粘进去
   - `BOOKING_CURL_STEP2`：把 `booking.curl` 的**全文**粘进去（两步流程）
   - 如果你只配了单步模板，用 `BOOKING_CURL` 即可
   - `PUSHPLUS_TOKEN`、`SERVERCHAN_KEY`、`SMTP_HOST`、`SMTP_USER`、`SMTP_PASS`、`MAIL_TO`
   **Variables**（可选）：`POSS_FB_USER_ID`（默认 657032）、`POSS_CONFIG`（整段 JSON，在线覆盖配置）、
   `BOOKING_CURL_FILE_STEP1` / `BOOKING_CURL_FILE_STEP2`（用文件路径而非 Secret 全文）
3. **把 UI 配好的东西带上云**：
   - `config.json`（含 `booking.targets` 和你在「设置」里调的所有参数）——
     直接提交进仓库（最简单，里面没有凭据），或把整个 JSON 贴进 `POSS_CONFIG` 这个 Variable。
   - `.curl` 模板 —— 文件本身被 gitignore 了，所以必须走上面那两个 Secret。
     在 UI 的「预订模板」里能看到每步的字节数，粘 Secret 前对一下长度就知道有没有粘错。
4. Actions 页启用 workflow。定时任务（UTC，香港时间 = UTC+8）：
   - `*/5 * * * *` 常规轮询（蹲守提醒）
   - `29 0 * * *` = 香港 08:29 预热，准点 08:30:00 开火（**每天**跑，所以任意放位日都能接住）
   - `35 0 * * *` = 香港 08:35 兜底重试

## 注意事项 / 风险

- **会自动花钱，而且是「每抢到一节扣一次」**：配了 N 个目标就可能扣 N 次押金。
  代码的防护：每个目标跨运行只提交一次（`state.json` 的 `booked_targets` 记着谁已经成功过）；
  结果页读不出结论时**立刻全线停手**并推送让你人工确认（绝不盲重试）；
  模板里有取不到值的占位符时直接拒绝发送。
  只有服务端**明确**回了失败（如时段已被抢走）才会继续试下一个候选 —— 因为那说明钱没花出去。
  但**请自己核对 POSS 上的预订记录**，也别一次点太多目标。
- **`facilityId` 从时间表数据来**：POSS 的预订请求需要场馆内部的设施编号
  （`boMakeBookFacilities[0].facilityId`），这个值必须来自 timetable.json。
  解析器会继承父节点的字段，所以设施编号挂在分组节点上也能取到；
  但若 `--dry-book` 报「占位符取不到值」，就说明真实结构还没对齐，需要先 `--dump`。
- **换活动/中心要重新校准模板**：`booking.curl` 里的 `centerName` / `facilityName` 是抓包时
  那个中心的字面值。UI 换了 Center 之后，请重新抓一条该中心的提交请求并 `calibrate.py --import`，
  否则字段名对得上、值却是旧中心的。
- **预订完成后请手动确认**：若 POSS 还有「确认预订」步骤或支付环节，记得去做。
- **频率与风控**：常规轮询 5 分钟一次、会话缓存复用约 90 分钟，抢位仅集中在触发时刻。
- **UI 会直接改 `config.json`**：所有保存都是「读原文 → 深合并 → 校验 → 写回」，
  校验不过就不写盘；但如果你同时手工编辑着这个文件，两边会互相覆盖。改之前先停一下 UI。
- **风险自担**：自动化访问学校系统可能违反 IT 使用守则；密码仅存 GitHub Secrets（加密），
  代码与日志不落明文；`session.json` 含短期会话 cookie，仅存于你的私有仓库。
