#!/usr/bin/env python3
"""
POSS 体育课预订监控脚本
========================
轮询预订情况接口 → 和上次状态对比 → 一旦有空位冒出来，
立刻通过 微信推送 (PushPlus / Server酱) + 邮件 提醒。

所有敏感配置通过环境变量（本地测试）或 GitHub Secrets（线上）注入。
"""

import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText

import requests

# ==================== 配置（环境变量） ====================

API_URL = os.environ.get("POSS_API_URL", "").strip()
API_HEADERS = json.loads(os.environ.get("POSS_HEADERS") or "{}")
API_METHOD = os.environ.get("POSS_METHOD", "GET").upper()
API_BODY = os.environ.get("API_BODY", "").strip()

POSS_BOOK_URL = os.environ.get("POSS_BOOK_URL", "").strip()  # 抢位时手动打开的预订页面链接

PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "").strip()

SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()
MAIL_TO = os.environ.get("MAIL_TO", "").strip()

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
TZ = timezone(timedelta(hours=8))

# 同一个空位一直没人抢走时，隔多久再提醒一次（秒），防止刷屏
RENOTIFY_AFTER = 30 * 60

# ==================== 抓取预订情况 ====================


def fetch_raw():
    """执行那条 HTTP 请求，返回原始 JSON（或文本）。"""
    if not API_URL:
        print("[FATAL] 未配置 POSS_API_URL，无事可做。")
        sys.exit(1)

    kwargs = {"headers": API_HEADERS, "timeout": 30}
    if API_METHOD == "GET":
        resp = requests.get(API_URL, **kwargs)
    else:
        kwargs["data"] = API_BODY or None
        resp = requests.request(API_METHOD, API_URL, **kwargs)

    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return resp.text


def parse_availability(data) -> dict:
    """
    把接口返回解析成 {场次名: 剩余名额}。
    只需包含「还有名额」的场次；全部约满时返回空 dict 即可。

    ⚠️⚠️⚠️ TODO: 这里的默认实现只是占位示范。
    拿到真实接口返回结构后，必须按实际字段改写这一段。⚠️⚠️⚠️
    """
    # ---- 占位示范（假设返回形如 {"data": [{"name": "...", "quotaLeft": 0}]}）----
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        result = {}
        for item in data["data"]:
            name = item.get("name") or item.get("venueName") or str(item)
            left = item.get("quotaLeft", item.get("left", 0))
            if isinstance(left, int) and left > 0:
                result[str(name)] = left
        return result

    # 拿不到结构就原样打出来，方便把真实返回贴回去定制解析逻辑
    print("---- 接口原始返回（把这段贴给 AI 定制解析） ----")
    if isinstance(data, str):
        print(data[:2000])
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
    print("-" * 50)
    return {}


# ==================== 通知渠道 ====================


def notify_pushplus(title: str, content: str) -> bool:
    """PushPlus 微信推送（免费，www.pushplus.plus）。"""
    try:
        r = requests.post(
            "https://www.pushplus.plus/send",
            json={"token": PUSHPLUS_TOKEN, "title": title,
                  "content": content, "template": "txt"},
            timeout=15,
        )
        ok = r.json().get("code") == 200
        print(f"[PushPlus] {'OK' if ok else r.text[:200]}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"[PushPlus] 失败: {e}")
        return False


def notify_serverchan(title: str, content: str) -> bool:
    """Server酱 微信推送（sct.ftqq.com）。"""
    try:
        r = requests.post(
            f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
            data={"title": title, "desp": content},
            timeout=15,
        )
        ok = r.json().get("code") == 0
        print(f"[Server酱] {'OK' if ok else r.text[:200]}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"[Server酱] 失败: {e}")
        return False


def notify_email(subject: str, content: str) -> bool:
    """SMTP 邮件（SSL，如 QQ邮箱/163 的授权码）。"""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and MAIL_TO):
        return False
    try:
        msg = MIMEText(content, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = SMTP_USER
        msg["To"] = MAIL_TO
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [MAIL_TO], msg.as_string())
        print("[邮件] OK")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[邮件] 失败: {e}")
        return False


def notify_all(title: str, content: str):
    channels = 0
    if PUSHPLUS_TOKEN:
        channels += 1
        notify_pushplus(title, content)
    if SERVERCHAN_KEY:
        channels += 1
        notify_serverchan(title, content)
    if notify_email(title, content):
        channels += 1
    if channels == 0:
        print("[FATAL] 检测到空位，但一个通知渠道都没配！白蹲了！")


# ==================== 状态持久化（防止重复提醒） ====================


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {"slots": {}}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ==================== 主流程 ====================


def main():
    now = int(time.time())
    now_str = datetime.now(TZ).strftime("%m-%d %H:%M")

    raw = fetch_raw()
    available = parse_availability(raw)  # {场次: 剩余名额}，只含有名额的

    state = load_state()
    slots = state.setdefault("slots", {})

    # 找出「新冒出来的空位」或「上次提醒过但一直没被抢走」的场次
    to_alert = []
    for name, left in available.items():
        prev = slots.get(name, {})
        was_full = prev.get("left", 0) <= 0
        renotify = now - prev.get("last_notified", 0) > RENOTIFY_AFTER
        if was_full or renotify:
            to_alert.append((name, left))

    if to_alert:
        lines = [f"🔥 {name}：剩 {left} 个名额" for name, left in sorted(to_alert)]
        content = "\n".join(lines)
        if POSS_BOOK_URL:
            content += f"\n\n抢位入口：{POSS_BOOK_URL}"
        content += f"\n\n（{now_str} 抓到，手速就是一切）"
        print(f"发现空位！\n{content}")
        notify_all("🏀 POSS 有空位啦，快抢！", content)
    else:
        print(f"[{now_str}] 全满，继续蹲……（当前有名额场次数：{len(available)}）")

    # 更新并保存状态
    for name in list(slots):
        if name not in available:
            slots[name]["left"] = 0
    for name, left in available.items():
        s = slots.setdefault(name, {})
        s["left"] = left
        if any(n == name for n, _ in to_alert):
            s["last_notified"] = now
    save_state(state)


if __name__ == "__main__":
    main()
