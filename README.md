# POSS 蹲位器 🏀

监控 PolyU POSS 体育课预订系统（X123 / X202 健身房），一旦目标时段出现空位
（有人取消预订），**第一时间微信推送 + 邮件提醒**。

自动登录 NetID → 抓取预订时间表 → 过滤目标时段（周二 12:30–18:00，周三/四/五
14:30–18:00）→ 对比上次状态 → 新空位立即推送。GitHub Actions 每 5 分钟免费跑一次，
手机关机也在蹲。

## 文件结构

| 文件 | 作用 |
|---|---|
| `config.json` | **馆列表 + 目标时段 + 请求参数**（全部可配，改这里就行） |
| `poss_client.py` | 自动登录 + 抓 timetable.json（会话缓存复用） |
| `monitor.py` | 主程序：解析/过滤/去重/推送；`--dump` 校准模式 |
| `.github/workflows/monitor.yml` | GitHub Actions 定时任务 |
| `state.json` / `session.json` | 运行状态与会话缓存（由 Actions 自动回写） |
| `dumps/` | 校准数据（本地调试用，已 gitignore） |

## 配置说明（config.json）

```json
{
  "gyms": [
    {"name": "X123", "ctrId": "151"},
    {"name": "X202", "ctrId": "152", "extra_targets": [{"days": [4], "from": "08:00", "to": "12:00"}]},
    {"name": "X999", "ctrId": "999", "targets": [{"days": [0, 6], "from": "20:00", "to": "22:00"}]}
  ],
  "targets": [
    {"days": [1], "from": "12:30", "to": "18:00"},
    {"days": [2, 3, 4], "from": "14:30", "to": "18:00"}
  ],
  "request": {
    "bookType": "INDV",
    "dataSetId": "18",
    "actvId": "101",
    "facilityId": "",
    "showCourtAreaDetails": "true",
    "searchDates": []
  }
}
```

- **`gyms`**：想盯几个馆就加几条。`ctrId` 是 POSS 的场馆编号（X123=151、X202=152），
  `name` 只用于提醒消息和状态记录，随便起但不能重复。
- **`targets`**（全局）：所有馆默认盯的时段。`days` 里 `0`=周一 … `6`=周日；
  `from`/`to` 是 `HH:MM`；只要时段与窗口**有交集**就算命中。
- **馆级时段规则**（可选，三选一）：
  - 什么都不配 → 用全局 `targets`
  - `extra_targets` → **全局 + 追加**（例：X202 既盯全局时段，又多盯周五上午）
  - `targets` → **覆盖全局**（例：X999 只盯周末晚上，不看全局）
- **`request`**：timetable.json 的请求参数（换运动项目就改 `actvId`）。
  `searchDates` 留空数组表示用系统默认视图；填日期数组（如 `["2026-09-15"]`）
  则按日期逐个查询。
- **环境变量 `POSS_CONFIG`** 可以传整个 JSON 字符串，优先级高于 `config.json`
  （适合在 GitHub Variables 里在线改配置而不用改文件）。



## 第一步：本地校准（必做一次）

1. 复制 `.env.example` 为 `.env`，填入 `POSS_NETID`、`POSS_PASSWORD`
   （**密码只写进本地 .env，永远不要发到任何聊天/仓库里**）
2. 安装依赖并跑校准：
   ```bash
   pip3 install -r requirements.txt
   python3 monitor.py --dump
   ```
3. 成功后 `dumps/` 里会有两馆的时间表原始 JSON —— 拿到它们即可完成解析器定稿。

## 第二步：配置通知渠道

### 微信推送（PushPlus，免费）
1. 微信扫码登录 https://www.pushplus.plus 并关注其公众号
2. 「一对一推送」页复制 TOKEN → 填入 `.env` 的 `PUSHPLUS_TOKEN`

### 邮件（QQ 邮箱示例）
1. 设置 → 账户 → 开启 SMTP → 生成「授权码」
2. 填 `.env`：`SMTP_HOST=smtp.qq.com`、`SMTP_PORT=465`、
   `SMTP_USER=发件邮箱`、`SMTP_PASS=授权码`、`MAIL_TO=收件邮箱`

## 第三步：部署 GitHub Actions

1. 新建 **Private** 仓库（必须私有！）并推送代码：
   ```bash
   git remote add origin git@github.com:<你>/poss-monitor.git
   git push -u origin main
   ```
2. 仓库 Settings → Secrets and variables → Actions，添加 **Secrets**：

   | 名称 | 内容 |
   |---|---|
   | `POSS_NETID` | 你的 NetID |
   | `POSS_PASSWORD` | 你的 NetPassword |
   | `PUSHPLUS_TOKEN` | PushPlus token |
   | `SMTP_HOST` | 如 `smtp.qq.com` |
   | `SMTP_USER` / `SMTP_PASS` | 发件邮箱 / 授权码 |
   | `MAIL_TO` | 收件邮箱 |

   可选 **Variables**（非敏感）：`POSS_FB_USER_ID`（默认 657032）、
   `POSS_CONFIG`（装上面那段 JSON，可在线覆盖 `config.json`）

3. Actions 页启用 workflow，点 **Run workflow** 手动跑一次验证
4. 收到第一条推送 = 上线成功

## 工作机制 & 注意事项

- **自动登录**：每次运行先复用 `session.json` 里的缓存会话（约 90 分钟），
  失效才重新登录——避免每天 288 次登录触发学校风控。
- **去重**：名额 0→N 才提醒；空位持续存在每 30 分钟重提醒（`RENOTIFY_AFTER`）。
- **失明警报**：登录失败/接口异常会推送警报（每小时最多一条），绝不静默装死。
- **风险自担**：自动化访问学校系统理论上可能违反 IT 使用守则，频率已刻意保守
  （5 分钟一次、会话复用），但请知悉风险。密码仅存于 GitHub Secrets（加密），
  代码与日志不落明文；`session.json` 含短期会话 cookie，仅存在于你的私有仓库。
