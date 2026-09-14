# POSS 体育课蹲位器 🏀

监控 PolyU POSS 体育课预订系统，一旦有人取消预订放出空位，**第一时间微信推送 + 邮件提醒**。

原理很简单：GitHub Actions 每 5 分钟免费帮你跑一次 `monitor.py`，轮询那个预订情况接口，发现空位就推送。你只需要负责手速。

## 一、准备通知渠道

### 微信推送（二选一，推荐 PushPlus）

**PushPlus（免费）**
1. 微信扫码登录 https://www.pushplus.plus
2. 关注它的微信公众号（登录页面会提示）
3. 「一对一推送」页面复制你的 `TOKEN`

**Server酱（备选）**
1. 微信扫码登录 https://sct.ftqq.com
2. 「SendKey」页面复制 `SendKey`

### 邮件（以 QQ 邮箱为例）

1. QQ 邮箱 → 设置 → 账户 → 开启 SMTP 服务
2. 生成「授权码」（不是 QQ 密码！）
3. 记下：SMTP 服务器 `smtp.qq.com`、端口 `465`、你的邮箱、授权码、收件邮箱

## 二、部署到 GitHub

1. 新建一个 **Private** 仓库（务必私有！里面有你的登录 Cookie）
2. 把本项目所有文件推上去：

   ```bash
   git remote add origin git@github.com:<你的用户名>/poss-monitor.git
   git push -u origin main
   ```

3. 仓库页面 → **Settings → Secrets and variables → Actions**，配置：

   **Secrets（敏感信息）：**

   | 名称 | 内容 |
   |---|---|
   | `POSS_API_URL` | 预订情况接口的完整 URL |
   | `POSS_HEADERS` | 请求头 JSON，含 Cookie，如 `{"Cookie":"...","Authorization":"..."}` |
   | `API_BODY` | POST 请求才需要，请求体；GET 留空 |
   | `PUSHPLUS_TOKEN` | PushPlus 的 TOKEN（用 Server酱则配 `SERVERCHAN_KEY`） |
   | `SERVERCHAN_KEY` | Server酱 SendKey（可选，和 PushPlus 二选一或都配） |
   | `SMTP_HOST` | 如 `smtp.qq.com` |
   | `SMTP_USER` | 发件邮箱 |
   | `SMTP_PASS` | 邮箱授权码 |
   | `MAIL_TO` | 收件邮箱 |

   **Variables（非敏感，可选项）：**

   | 名称 | 内容 |
   |---|---|
   | `POSS_METHOD` | `GET` 或 `POST`，默认 GET |
   | `POSS_BOOK_URL` | 抢位时手动打开的预订页面链接（提醒消息里会带上） |
   | `SMTP_PORT` | 默认 `465` |

4. 仓库 **Actions** 页启用 `POSS Slot Monitor` workflow，可先点 **Run workflow** 手动跑一次测试

## 三、本地测试（可选）

```bash
export POSS_API_URL="https://..."
export POSS_HEADERS='{"Cookie":"..."}'
export PUSHPLUS_TOKEN="..."
python3 monitor.py
```

## ⚠️ 重要提醒

- **Cookie 会过期**：POSS 的登录态一般几天到几周失效。如果监控连续报错/返回登录页，去浏览器重新抓一次 Cookie，更新 `POSS_HEADERS` 这个 Secret 即可。
- **接口返回结构**：`monitor.py` 里 `parse_availability()` 是占位实现，拿到真实返回后需要按实际 JSON 字段定制（跑一次会把原始返回打在日志里，贴出来即可定制）。
- **频率**：GitHub Actions cron 每 5 分钟一次，实际可能有 1~3 分钟调度延迟。对蹲退订来说够用；想更快可以本地跑 `watch -n 30 python3 monitor.py`。
- 同一场次空位持续存在时，每 30 分钟重复提醒一次（`RENOTIFY_AFTER` 可调），不会刷屏。
