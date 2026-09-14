#!/usr/bin/env python3
"""
POSS 蹲位器 - 主程序
====================
poss_client 自动登录 → 抓两馆 timetable.json → 解析时段 → 过滤目标时段
→ 与 state.json 对比 → 新空位立即 微信推送 + 邮件。

用法：
  python3 monitor.py            # 正常监控（本地测试/Actions）
  python3 monitor.py --dump     # 校准模式：登录+抓取，原始数据存 dumps/，不推送
  python3 monitor.py --no-notify # 监控但不推送（干跑测试）
"""

import argparse
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText

from poss_client import GYMS, LoginFailed, PossClient, SessionExpired

TZ = timezone(timedelta(hours=8))

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
SESSION_FILE = os.environ.get("SESSION_FILE", "session.json")
DUMP_DIR = os.environ.get("DUMP_DIR", "dumps")

POSS_BOOK_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"

# 同一空位一直没被抢走时，隔多久再提醒一次（秒）
RENOTIFY_AFTER = int(os.environ.get("RENOTIFY_AFTER", 30 * 60))
# 「监控失明」警报的最小间隔（秒），防止每 5 分钟轰炸一次
FAIL_ALERT_INTERVAL = 60 * 60

# ---- 通知渠道配置（环境变量 / .env） ----
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "").strip()
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()
MAIL_TO = os.environ.get("MAIL_TO", "").strip()

# ---- 目标时段：周二 12:30–18:00；周三/四/五 14:30–18:00 ----
# days: 0=周一 … 6=周日；可用环境变量 TARGET_SLOTS 覆盖（JSON 数组，同结构）
DEFAULT_TARGET_SLOTS = [
    {"days": [1], "from": "12:30", "to": "18:00"},
    {"days": [2, 3, 4], "from": "14:30", "to": "18:00"},
]


def load_target_slots():
    raw = os.environ.get("TARGET_SLOTS", "").strip()
    if raw:
        return json.loads(raw)
    return DEFAULT_TARGET_SLOTS


def load_env_file(path=".env"):
    """极简 .env 加载（本地测试用；线上用 GitHub Secrets 注入环境变量）。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# ==================== 解析 timetable.json ====================
# ⚠️ 结构未知（会话过期没能拿到样本）。以下为启发式解析：
#    深度遍历 JSON，收集「像时段」的 dict（同时含日期键 + 时间键 + 数量键）。
#    --dump 校准拿到真实结构后，这一段要替换成精确解析。⚠️

DATE_KEYS = ["bookdate", "bookingdate", "date", "daydate", "slotdate", "startdate"]
START_KEYS = ["starttime", "start", "booktime", "timefrom", "from", "bookstart"]
END_KEYS = ["endtime", "end", "timeto", "to", "bookend"]
LEFT_HINTS = [
    "vacancy", "vacant", "quotaleft", "left", "remain", "available", "avail",
    "nofavail", "empty", "free", "slotleft", "canslot",
]


class ParseNeedsCalibration(Exception):
    """JSON 结构超出启发式能力 —— 需要先跑 --dump 校准。"""


def parse_timetable(data) -> list:
    """返回 [{date:'YYYY-MM-DD', start:'HH:MM', end:'HH:MM', left:int, raw:...}]"""
    found = []

    def walk(node):
        if isinstance(node, dict):
            date_v = _pick(node, DATE_KEYS)
            start_v = _pick(node, START_KEYS)
            if date_v and start_v:
                end_v = _pick(node, END_KEYS) or ""
                left = _pick_int(node, LEFT_HINTS)
                found.append({
                    "date": str(date_v), "start": str(start_v),
                    "end": str(end_v), "left": left, "raw": node,
                })
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    if not found:
        raise ParseNeedsCalibration(
            "启发式解析颗粒无收：timetable.json 结构未知，请先跑 `python3 monitor.py --dump` "
            "并把 dumps/ 里的文件交给开发者校准解析器。"
        )
    return found


def _pick(d: dict, keys):
    for k in d:
        if k.lower() in keys:
            return d[k]
    return None


def _pick_int(d: dict, hints):
    for k, v in d.items():
        if any(h in k.lower() for h in hints) and isinstance(v, (int, float)):
            return int(v)
    return None


# ==================== 时段过滤 ====================


def _parse_hm(s):
    s = str(s).strip()
    for fmt in ("%H:%M:%S", "%H:%M", "%H%M"):
        try:
            return datetime.strptime(s, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return None


def _parse_date(s):
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def slot_matches_target(slot, targets) -> bool:
    """时段与任一目标窗口有交集（按香港时区的星期几 + HH:MM）。"""
    d = _parse_date(slot["date"])
    st = _parse_hm(slot["start"])
    if not d or not st:
        return False
    et = _parse_hm(slot["end"]) if slot.get("end") else None
    slot_start = st
    slot_end = et or st  # 没有结束时间就按点处理
    for t in targets:
        if d.weekday() not in t["days"]:
            continue
        if slot_start < t["to"] and (slot_end > t["from"] if slot_end != slot_start else slot_start >= t["from"]):
            return True
    return False


# ==================== 通知 ====================


def notify_pushplus(title, content) -> bool:
    try:
        import requests
        r = requests.post(
            "https://www.pushplus.plus/send",
            json={"token": PUSHPLUS_TOKEN, "title": title, "content": content, "template": "txt"},
            timeout=15,
        )
        ok = r.json().get("code") == 200
        print(f"[PushPlus] {'OK' if ok else r.text[:200]}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"[PushPlus] 失败: {e}")
        return False


def notify_serverchan(title, content) -> bool:
    try:
        import requests
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


def notify_email(subject, content) -> bool:
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


def notify_all(title, content):
    channels = 0
    if PUSHPLUS_TOKEN:
        channels += 1
        notify_pushplus(title, content)
    if SERVERCHAN_KEY:
        channels += 1
        notify_serverchan(title, content)
    if notify_email(title, content):
        channels += 1
    return channels


# ==================== 状态 ====================


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


def build_client():
    netid = os.environ.get("POSS_NETID", "").strip()
    password = os.environ.get("POSS_PASSWORD", "").strip()
    fb_user_id = os.environ.get("POSS_FB_USER_ID", "657032").strip()
    return PossClient(netid, password, fb_user_id)


def fetch_all(client) -> dict:
    """抓两馆时间表。会话失效自动登录一次。"""
    def _grab():
        return {gym: client.timetable(ctr) for gym, ctr in GYMS.items()}

    if client.load_session(SESSION_FILE):
        try:
            print("[session] 复用缓存会话")
            return _grab()
        except SessionExpired:
            print("[session] 缓存会话已失效 → 重新登录")

    client.login()
    client.save_session(SESSION_FILE)
    print("[session] 登录成功，会话已缓存")
    return _grab()


def run(args):
    targets = load_target_slots()
    now = int(time.time())
    now_str = datetime.now(TZ).strftime("%m-%d %H:%M")

    client = build_client()
    data = fetch_all(client)

    # ---- 校准模式：原样落盘，不解析不推送 ----
    if args.dump:
        os.makedirs(DUMP_DIR, exist_ok=True)
        for gym, raw in data.items():
            p = os.path.join(DUMP_DIR, f"timetable_{gym}.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
            print(f"[dump] {p}")
        # 顺带把预订页也存一份（看时段/日期字段的呈现方式）
        r = client.s.get("https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do", timeout=30)
        with open(os.path.join(DUMP_DIR, "make_book.html"), "w", encoding="utf-8") as f:
            f.write(r.text)
        print(f"[dump] {DUMP_DIR}/make_book.html ({len(r.text)} bytes)")
        # 结构速览
        for gym, raw in data.items():
            print(f"[dump] {gym} 顶层类型: {type(raw).__name__}, "
                  f"键: {list(raw.keys()) if isinstance(raw, dict) else 'N/A'}")
        return

    # ---- 正常监控 ----
    state = load_state()
    slots = state.setdefault("slots", {})

    # 解析 + 过滤出目标时段内、有名额的场次
    open_slots = []  # [{key, gym, date, start, end, left}]
    for gym, raw in data.items():
        for s in parse_timetable(raw):
            if not slot_matches_target(s, targets):
                continue
            if s["left"] is None or s["left"] <= 0:
                continue
            key = f"{gym}|{s['date']}|{s['start']}"
            open_slots.append({"key": key, "gym": gym, **{k: s[k] for k in ("date", "start", "end", "left")}})

    # 对比上次状态 → 找出需要提醒的（新空位 或 超过重提醒间隔的持续空位）
    to_alert = []
    for s in open_slots:
        prev = slots.get(s["key"], {})
        was_full = prev.get("left", 0) <= 0
        renotify = now - prev.get("last_notified", 0) > RENOTIFY_AFTER
        if was_full or renotify:
            to_alert.append(s)

    if to_alert:
        lines = [f"🔥 {s['gym']} {s['date']} {s['start']}–{s['end'] or '?'}：剩 {s['left']} 个名额"
                 for s in sorted(to_alert, key=lambda x: (x["date"], x["start"]))]
        content = "\n".join(lines) + f"\n\n抢位入口：{POSS_BOOK_URL}\n（{now_str} 抓到，手速就是一切）"
        print(f"发现空位！\n{content}")
        if not args.no_notify:
            n = notify_all("🏀 POSS 有空位啦，快抢！", content)
            if n == 0:
                print("[FATAL] 检测到空位但没有任何通知渠道可用！")
    else:
        print(f"[{now_str}] 目标时段全满，继续蹲……（全周有名额场次：{len(open_slots)}）")

    # 更新状态
    for key in list(slots):
        if not any(s["key"] == key for s in open_slots):
            slots[key]["left"] = 0
    for s in open_slots:
        e = slots.setdefault(s["key"], {})
        e["left"] = s["left"]
        if any(t["key"] == s["key"] for t in to_alert):
            e["last_notified"] = now
    save_state(state)


def main():
    parser = argparse.ArgumentParser(description="POSS 蹲位器")
    parser.add_argument("--dump", action="store_true", help="校准模式：抓原始数据存 dumps/，不推送")
    parser.add_argument("--no-notify", action="store_true", help="监控但不发送通知")
    args = parser.parse_args()

    load_env_file()

    try:
        run(args)
    except LoginFailed as e:
        print(f"[FATAL] 登录失败: {e}")
        _fail_alert(f"POSS 蹲位器登录失败: {e}", args)
        sys.exit(1)
    except (SessionExpired, ParseNeedsCalibration) as e:
        print(f"[FATAL] {e}")
        _fail_alert(f"POSS 蹲位器失明: {e}", args)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"[FATAL] 未知错误: {type(e).__name__}: {e}")
        _fail_alert(f"POSS 蹲位器未知错误: {type(e).__name__}: {e}", args)
        sys.exit(1)


def _fail_alert(msg, args):
    """监控挂了的警报，限频推送（凭证错时避免每 5 分钟轰炸）。"""
    if args.no_notify:
        return
    state = load_state()
    now = int(time.time())
    if now - state.get("last_fail_alert", 0) < FAIL_ALERT_INTERVAL:
        print("[fail-alert] 距上次警报太近，跳过")
        return
    state["last_fail_alert"] = now
    save_state(state)
    notify_all("🚨 POSS 蹲位器失明了", msg + "\n\n请检查 GitHub Secrets / 账号状态。")


if __name__ == "__main__":
    main()
