# POSS 蹲位器 设计方案 v1.0（已定稿）

> 状态：**已确认**。2026-09-14 grill 会话定稿，等待 timetable.json 真实样本后实施。

## 1. 目标

监控 PolyU POSS 体育馆预订系统，一旦目标时段出现空位（有人取消），第一时间
微信推送 + 邮件提醒用户手动去抢。**只提醒，不代抢**（用户已明确选择）。

## 2. 已确认的全部决策

| 决策点 | 结论 |
|---|---|
| 部署平台 | GitHub Actions，Private 仓库，cron 每 5 分钟 |
| 提醒渠道 | 微信推送（PushPlus 或 Server酱）+ SMTP 邮件，双通道 |
| 认证 | **自动登录**：NetID + NetPassword 存 GitHub Secrets |
| MFA | 无（登录仅账号密码，已实测登录表单 + 用户确认） |
| 动作 | 只提醒，用户手动抢位 |
| 监控范围 | X123（ctrId=151）+ X202（ctrId=152）两馆都盯 |
| 目标时段 | **周二 12:30–18:00；周三/四/五 14:30–18:00** |
| 轮询节奏 | 先 5 分钟，观察空位存活时间后再决定是否本地加速 |
| 凭据安全 | NetPassword 仅存 Secrets，代码/日志不落明文 |

## 3. 已实测的事实（2026-09-14 探测）

- 数据接口（GET，两馆各一次）：
  ```
  https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/timetable.json
    ?CSRFToken=<token>&fbUserId=657032&bookType=INDV&dataSetId=18
    &actvId=101&searchDate=&ctrId={151|152}&facilityId=&showCourtAreaDetails=true
  ```
- 裸访问 → 302 → 登录页。必须携带有效会话。
- `/starspossfbstud/` 登录请求 301 两跳兜回 `/poss/` 统一登录页：**POSS 家族共用登录入口**。
- 登录表单：`POST /poss/j_security_check`
  字段：`j_username`、`j_password`、`otheruser=N`、`buttonAction=loginButton`、
  `CSRFToken`（登录页每次加载生成，需先 GET 页面提取）
- 表单层无 MFA 字段；会话 Cookie 为 F5 BIG-IP `TS01*` 系列 + 登录后会话标识。

## 4. 架构与流程

```
GitHub Actions (cron */5, Private repo, secrets: NetID/NetPassword/推送token/SMTP)
  └─ monitor.py
       ├─ ① GET /poss/secure/login/loginhome.do → 提取 CSRFToken + 种 TS cookie
       ├─ ② POST /poss/j_security_check (NetID/NetPassword) → 建立会话
       ├─ ③ 打开预订页提取接口用的 CSRFToken
       ├─ ④ GET timetable.json ×2 (ctrId=151, 152)
       ├─ ⑤ parse: 提取每个时段剩余名额
       ├─ ⑥ 过滤: 只保留 周二12:30-18:00 / 周三四五14:30-18:00 的时段
       ├─ ⑦ 与 state.json(回写仓库) 对比 → 新空位 → 推送+邮件
       └─ ⑧ 容错: 登录失败/302/解析异常 → 推送「监控失明」警报，绝不静默失败
```

- **去重**：名额 0→N 触发；空位持续存在每 30 分钟重提醒（可调）。
- **状态持久化**：state.json 由 workflow commit 回仓库。

## 5. 待办

1. ⏳ 用户提供 timetable.json 的 Copy as cURL（含登录态）→ 定制解析器 ⑤
2. 实测确认：/poss/ 会话能否直访 /starspossfbstud/ 接口（疑点，部署时验证）
3. 实测确认：接口 CSRFToken 的下发位置（疑点，cURL 样本可解答）
4. 用户注册 PushPlus/Server酱 + 邮箱授权码，配置 Secrets
5. 部署 + 手动触发一次 workflow 验证全链路
