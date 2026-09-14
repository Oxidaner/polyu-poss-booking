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
| `endpoint` / `params` | ⚠️ **预订提交接口与表单（需抓包校准，见下）** |

**蹲守部分：** `fallback.targets` 不配则自动由 booking 派生（触发日 + 目标时段）。

## ⚠️ 必做：校准预订接口

查询时间表可以自己攒，但**提交预订必须照抄真实请求**——凭空猜一个会扣押金的
POST 端点是不负责任的，所以代码在校准前会拒绝抢位（只做蹲守提醒）并推送警报。

**抓取步骤：**
1. 登录 POSS，进入预订页，正常操作到「提交预订」那一步
2. F12 → Network → 找到点击提交后发出的请求（若还有确认步骤，两条都要）
3. 右键 **Copy as cURL**，得到 URL 路径和表单字段
4. 填进 `config.json`：
   - `booking.endpoint`：请求路径，如 `/starspossfbstud/secure/ui_make_book/xxx.do`
   - `booking.params`：表单字段模板，值里可用占位符
     `{ctrId}` `{date}` `{startTime}` `{endTime}` `{gym}` `{searchDate}`，
     以及时间表返回的任意字段名（如槽位 ID）

   例：真实请求体为 `slotId=12345&bookingDate=2026-09-22&startTime=16:30`，
   时间表返回里槽位 ID 字段叫 `slotId`，则写：
   ```json
   "params": {"slotId": "{slotId}", "bookingDate": "{date}", "startTime": "{startTime}"}
   ```
5. 验证：`python3 monitor.py --dry-book` —— 会查时间表、打印将要提交的表单但不真提交。
   对照抓包结果，字段一致即可上线。

## 第一步：本地校准与试跑

```bash
cp .env.example .env      # 填入 POSS_NETID / POSS_PASSWORD（只写本地，别贴聊天）
pip3 install -r requirements.txt
python3 monitor.py --dump       # 抓真实时间表结构 → dumps/
python3 monitor.py --dry-book   # 演练抢位并打印表单，不真提交
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
   **Variables**（可选）：`POSS_FB_USER_ID`（默认 657032）、`POSS_CONFIG`（整段 JSON，在线覆盖配置）
3. Actions 页启用 workflow。定时任务（UTC，香港时间 = UTC+8）：
   - `*/5 * * * *` 常规轮询（蹲守提醒）
   - `29 0 * * *` = 香港 08:29 预热，准点 08:30:00 开火
   - `35 0 * * *` = 香港 08:35 兜底重试

## 注意事项 / 风险

- **会自动花钱**：预订成功会扣押金。代码做了防重复（同一触发日只抢一次，成功后跳过），
  但**请自己核对 POSS 上的预订记录**。
- **预订完成后请手动确认**：若 POSS 还有「确认预订」步骤或支付环节，记得去做。
- **频率与风控**：常规轮询 5 分钟一次、会话缓存复用约 90 分钟，抢位仅集中在触发时刻。
- **风险自担**：自动化访问学校系统可能违反 IT 使用守则；密码仅存 GitHub Secrets（加密），
  代码与日志不落明文；`session.json` 含短期会话 cookie，仅存于你的私有仓库。
