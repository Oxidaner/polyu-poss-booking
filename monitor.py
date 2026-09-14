#!/usr/bin/env python3
"""
POSS 抢位器 / 蹲位器 - 主程序
==============================
两种模式自动切换：

  ① 抢位（config.booking）：在触发日（默认周二、周四）08:30 前预热等待，
     到点后爆发式抢「lead_days 天后（默认下周同一天）」的目标时段（默认 16:30–17:30）。
     抢到 → 立即推送；没抢到 → 推送一次「转入蹲守」。
  ② 蹲守提醒（config.fallback）：监控目标时段的空位（有人取消就放出来），
     出现空位立即 微信推送 + 邮件。

用法：
  python3 monitor.py                # 正常跑（Actions / 本地）
  python3 monitor.py --dump         # 校准：抓原始数据存 dumps/，不抢不推
  python3 monitor.py --dry-book     # 演练：演练抢位并打印将要提交的表单，不真提交
  python3 monitor.py --no-notify    # 静默跑（不发通知，用于测试）

测试用：设 POSS_FAKE_NOW=2026-09-15T08:31 可伪造当前时间（同时跳过等待）。
"""

import argparse
import json
import locale
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText

from poss_client import (
    BASE,
    BookingNotCalibrated,
    LoginFailed,
    PossClient,
    SessionExpired,
    find_unresolved,
    mask_secrets,
    render_template,
)

# %b（月份缩写）必须稳定输出英文 "Sep"：POSS 的 startDateTime 形如
# "18 Sep 2026 14:30"，若跟随系统语言（zh_CN）会变成 "18 9月 2026" 而请求失败。
try:
    locale.setlocale(locale.LC_TIME, "C")
except locale.Error:  # pragma: no cover
    pass

TZ = timezone(timedelta(hours=8))

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
SESSION_FILE = os.environ.get("SESSION_FILE", "session.json")
DUMP_DIR = os.environ.get("DUMP_DIR", "dumps")
CONFIG_FILE = os.environ.get("CONFIG_FILE", "config.json")
# 抓包导入的预订请求模板（calibrate.py 生成）
BOOKING_CURL_FILE = os.environ.get("BOOKING_CURL_FILE", "booking.curl")

POSS_BOOK_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"

# 同一空位一直没被抢走时，隔多久再提醒一次（秒）
RENOTIFY_AFTER = int(os.environ.get("RENOTIFY_AFTER", 30 * 60))
# 「监控失明」警报的最小间隔（秒）
FAIL_ALERT_INTERVAL = 60 * 60

# ---- 通知渠道（环境变量 / .env） ----
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "").strip()
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()
MAIL_TO = os.environ.get("MAIL_TO", "").strip()

# ---- 内置默认 ----
DEFAULT_GYMS = [
    {"name": "X123", "ctrId": "151"},
    {"name": "X202", "ctrId": "152"},
]
DEFAULT_BOOKING = {
    "enabled": True,
    "trigger_days": [1, 3],          # 周二、周四（0=周一）
    "trigger_time": "08:30",
    "warmup_minutes": 6,             # 触发前多少分钟内到达就原地等待
    "grace_minutes": 30,             # 触发后多少分钟内仍积极尝试抢
    "lead_days": 7,                  # 抢 7 天后（下周同一天）
    "burst_seconds": 120,            # 爆发抢位持续时长
    "retry_interval": 3,             # 每轮间隔秒数
    "gyms": [],                      # 空 = 按 gyms 顺序全部尝试
    "from": "16:30",
    "to": "17:30",
    "search_date_format": "%Y-%m-%d",
    "curl_file": "booking.curl",     # 抓包导入的预订请求模板（首选）
    "curl": "",                      # 也可直接把整段 cURL 写进配置（不推荐，含凭据）
    "steps": [],                     # 多步预订：每步一个 {"curl_file": "...", "extract": {...}}
    "endpoint": "",                  # 遗留路径：手工填端点（未用 cURL 时）
    "params": {},                    # 遗留路径：手工填表单模板
    "success_keywords": ["success", "successful", "成功", "已确认"],
}


def load_env_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# ==================== 配置 ====================


def load_config() -> dict:
    """优先级：环境变量 POSS_CONFIG（JSON）> config.json > 内置默认。"""
    raw = os.environ.get("POSS_CONFIG", "").strip()
    if raw:
        cfg = json.loads(raw)
    elif os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    cfg.setdefault("gyms", DEFAULT_GYMS)
    booking = dict(DEFAULT_BOOKING)
    booking.update(cfg.get("booking") or {})
    cfg["booking"] = booking
    cfg.setdefault("fallback", {"enabled": True})
    cfg.setdefault("request", {})
    _validate_config(cfg)
    return cfg


def _validate_config(cfg: dict):
    def bad(msg):
        raise ValueError(f"配置错误: {msg}")

    if not isinstance(cfg["gyms"], list) or not cfg["gyms"]:
        bad('gyms 必须是非空数组，例: [{"name": "X123", "ctrId": "151"}]')
    names = set()
    for g in cfg["gyms"]:
        if not isinstance(g, dict) or not g.get("name") or not g.get("ctrId"):
            bad(f"每个 gym 需要 name 和 ctrId: {g}")
        if g["name"] in names:
            bad(f"gym 名称重复: {g['name']}")
        names.add(g["name"])

    b = cfg["booking"]
    for d in b["trigger_days"]:
        if not isinstance(d, int) or not 0 <= d <= 6:
            bad(f"booking.trigger_days 取值 0-6（0=周一）: {d}")
    if not _parse_hm(b["trigger_time"]):
        bad(f"booking.trigger_time 必须是 HH:MM: {b['trigger_time']}")
    if not _parse_hm(b["from"]) or not _parse_hm(b["to"]):
        bad(f"booking.from/to 必须是 HH:MM: {b['from']}/{b['to']}")
    if b["from"] >= b["to"]:
        bad(f"booking 时段起点必须早于终点: {b['from']}-{b['to']}")
    for k in ("warmup_minutes", "grace_minutes", "lead_days", "burst_seconds", "retry_interval"):
        if not isinstance(b[k], int) or b[k] < 0:
            bad(f"booking.{k} 必须是非负整数: {b[k]}")
    unknown = [g for g in b["gyms"] if g not in names]
    if unknown:
        bad(f"booking.gyms 里有未定义的馆名: {unknown}（可选: {sorted(names)}）")

    steps = load_booking_steps(b)
    if b["enabled"]:
        missing = []
        for i, step in enumerate(steps):
            if not load_step_curl(step, b, i):
                missing.append(i + 1)
        if missing:
            print(f"[warn] 预订模板未配置完整（缺步骤 {missing}）—— 抢位将无法执行（仅蹲守提醒）。"
                  "请抓取手动预订那条「提交」请求的 cURL，"
                  "运行 `python3 calibrate.py --import <文件>` 生成 booking.curl。")

    if cfg["fallback"].get("enabled", True) and cfg["fallback"].get("targets"):
        _validate_targets(cfg["fallback"]["targets"], where="fallback.targets")

    dates = cfg["request"].get("searchDates", [])
    if not isinstance(dates, list):
        bad("request.searchDates 必须是日期字符串数组")


def _validate_targets(targets, where):
    if not isinstance(targets, list) or not targets:
        raise ValueError(f"配置错误: {where} 必须是非空数组")
    for t in targets:
        if not isinstance(t, dict) or not {"days", "from", "to"} <= set(t):
            raise ValueError(f"配置错误: {where} 每项需要 days/from/to: {t}")
        for d in t["days"]:
            if not isinstance(d, int) or not 0 <= d <= 6:
                raise ValueError(f"配置错误: {where} 的 days 取值 0-6（0=周一）: {t}")
        if not _parse_hm(t["from"]) or not _parse_hm(t["to"]):
            raise ValueError(f"配置错误: {where} 的 from/to 必须是 HH:MM: {t}")


def ctr_of(cfg, gym_name):
    for g in cfg["gyms"]:
        if g["name"] == gym_name:
            return g["ctrId"]
    raise ValueError(f"配置里没有馆: {gym_name}")


def fallback_targets(cfg):
    """蹲守目标时段：显式配置优先，否则由 booking 派生（触发日 + 抢位时段）。"""
    fb = cfg["fallback"]
    if fb.get("targets"):
        return fb["targets"]
    b = cfg["booking"]
    return [{"days": list(b["trigger_days"]), "from": b["from"], "to": b["to"]}]


def load_booking_curl(b: dict):
    """取预订请求模板。优先级：环境变量 BOOKING_CURL > 配置内联 curl > curl_file 文件。"""
    text = os.environ.get("BOOKING_CURL", "").strip()
    if text:
        return text
    if (b.get("curl") or "").strip():
        return b["curl"].strip()
    path = os.environ.get("BOOKING_CURL_FILE") or b.get("curl_file") or BOOKING_CURL_FILE
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return None


def load_booking_steps(b: dict) -> list:
    """返回预订步骤列表。配置了 steps 就用 steps；否则退化成单步（curl_file / curl）。"""
    steps = b.get("steps")
    if isinstance(steps, list) and steps:
        return steps
    return [{}]


def load_step_curl(step: dict, b: dict, idx: int = 0) -> str | None:
    """取某一步的 cURL 模板。

    优先级：step.curl 内联 > BOOKING_CURL_STEP{n} Secret > BOOKING_CURL_FILE_STEP{n} 文件路径 >
    step.curl_file / b.curl_file / booking.curl 文件。
    """
    text = (step.get("curl") or "").strip()
    if text:
        return text
    text = os.environ.get(f"BOOKING_CURL_STEP{idx + 1}", "").strip()
    if text:
        return text
    path = os.environ.get(f"BOOKING_CURL_FILE_STEP{idx + 1}", "")
    if not path:
        path = step.get("curl_file") or b.get("curl_file") or BOOKING_CURL_FILE
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return None


# ==================== 时间 ====================


def hk_now() -> datetime:
    fake = os.environ.get("POSS_FAKE_NOW")
    if fake:
        return datetime.strptime(fake, "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)
    return datetime.now(TZ)


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


def hm_to_min(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def booking_phase(cfg, now: datetime) -> str:
    """'pre'=预热等待 / 'open'=窗口内应抢 / 'closed'=不抢。"""
    b = cfg["booking"]
    if not b["enabled"] or now.weekday() not in b["trigger_days"]:
        return "closed"
    now_m = now.hour * 60 + now.minute
    t0 = hm_to_min(b["trigger_time"])
    if t0 <= now_m <= t0 + b["grace_minutes"]:
        return "open"
    if t0 - b["warmup_minutes"] <= now_m < t0:
        return "pre"
    return "closed"


def wait_until_trigger(cfg):
    """原地等到触发时刻（测试环境 POSS_FAKE_NOW 时直接跳过）。"""
    if os.environ.get("POSS_FAKE_NOW"):
        return
    b = cfg["booking"]
    h, m = (int(x) for x in b["trigger_time"].split(":"))
    target = hk_now().replace(hour=h, minute=m, second=0, microsecond=0)
    while hk_now() < target:
        time.sleep(1)
    print(f"[booking] 已到触发时刻 {b['trigger_time']}")


# ==================== 解析 timetable.json ====================
# ⚠️ 结构未校准：深度遍历 JSON，收集「像时段」的 dict（日期键 + 时间键 + 数量键）。
#    拿到真实样本（--dump）后替换为精确解析。⚠️

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
    """返回 [{date, start, end, left, raw}]。

    遍历时**继承祖先节点的标量字段**：POSS 常把 facilityId / ctrId 这类信息挂在
    分组节点上、时段挂在其下，只看叶子节点会漏掉它们，而预订模板正需要 facilityId。
    继承来的字段不覆盖节点自身的同名字段。
    """
    found = []

    def walk(node, inherited):
        if isinstance(node, dict):
            own = {k: v for k, v in node.items() if isinstance(v, (str, int, float))}
            merged = {**inherited, **own}
            date_v = _pick(node, DATE_KEYS)
            start_v = _pick(node, START_KEYS)
            if date_v and start_v:
                found.append({
                    "date": str(date_v),
                    "start": str(start_v),
                    "end": str(_pick(node, END_KEYS) or ""),
                    "left": _pick_int(node, LEFT_HINTS),
                    "raw": merged,
                })
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v, merged)
        elif isinstance(node, list):
            for item in node:
                walk(item, inherited)

    walk(data, {})
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


def slot_matches_target(slot, targets) -> bool:
    """时段与任一目标窗口有交集（按星期几 + HH:MM）。"""
    d = _parse_date(slot["date"])
    st = _parse_hm(slot["start"])
    if not d or not st:
        return False
    et = _parse_hm(slot["end"]) if slot.get("end") else None
    for t in targets:
        if d.weekday() not in t["days"]:
            continue
        if et:
            if st < t["to"] and et > t["from"]:
                return True
        elif t["from"] <= st < t["to"]:
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
    if channels == 0:
        print("[warn] 没有任何通知渠道可用（消息仅打印在日志）")
    return channels


# ==================== 状态 ====================


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {"slots": {}, "booked": {}}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ==================== 抢位引擎 ====================


def safe_format(template: str, ctx: dict) -> str:
    """占位符替换（支持 {key} 与 {key:%d/%m/%Y}）；未知占位符原样保留。"""
    return render_template(template, ctx)


def build_booking_context(client, b, slot, gym_name, ctr_id, target_date) -> dict:
    """组装占位符上下文：场馆 / 时段 / 日期 + timetable 原始字段 + 会话令牌。

    日期时间统一归一化成 ISO（"2026-09-18 14:30"），模板里用
    {startDateTime:url:%d %b %Y %H:%M} 这样的写法来控制最终呈现格式 ——
    格式只写在抓包模板里，不在代码里再搞一份，避免两处不一致。
    """
    d = _parse_date(slot["date"])
    date_iso = d.strftime("%Y-%m-%d") if d else str(slot["date"])
    start_iso = _parse_hm(slot["start"]) or str(slot["start"])
    end_iso = _parse_hm(slot["end"]) or str(slot["end"] or slot["start"])
    ctx = {
        "gym": gym_name,
        "ctrId": str(ctr_id),
        "date": date_iso,
        "startDate": date_iso,
        "rawDate": str(slot["date"]),
        "startTime": start_iso,
        "endTime": end_iso,
        "startDateTime": f"{date_iso} {start_iso}",
        "endDateTime": f"{date_iso} {end_iso}",
        "searchDate": target_date.strftime(b["search_date_format"]),
        "fbUserId": getattr(client, "fb_user_id", "") or "",
        "csrf": getattr(client, "api_csrf", "") or "",
    }
    # timetable 返回的原始字段也注入（如 slotId/facilityId），供 {同名} 占位符使用
    for k, v in (slot.get("raw") or {}).items():
        if isinstance(v, (str, int, float)):
            ctx.setdefault(str(k), str(v))
    return ctx


def build_booking_payload(b, slot, gym_name, ctr_id, target_date, ctx) -> dict:
    """遗留路径：按 config.booking.params 模板组装表单。"""
    return {
        k: (safe_format(v, ctx) if isinstance(v, str) else v)
        for k, v in (b.get("params") or {}).items()
    }


def pick_best(slots, b):
    """优先精确命中起点（如 16:30），否则取最早开始的。"""
    exact = [s for s in slots if _parse_hm(s["start"]) == b["from"]]
    pool = exact or slots
    return sorted(pool, key=lambda s: (_parse_date(s["date"]), _parse_hm(s["start"]) or ""))[0]


def _follow_redirects(client, url, headers, max_hops: int = 3):
    """中间步骤若返回 302，按 Location 跟进（最多 max_hops 次）。"""
    for _ in range(max_hops):
        r = client.s.get(url, headers=headers, allow_redirects=False, timeout=30)
        if r.status_code not in (301, 302):
            return r
        loc = r.headers.get("Location", "")
        if not loc:
            return r
        if "login" in loc.lower() or "loginhome" in loc.lower():
            raise SessionExpired(f"跟进确认页时被踢到登录页（302 → {loc}）")
        url = loc if loc.startswith("http") else BASE + loc
    return r


def run_booking_steps(client, b: dict, steps: list, ctx: dict, dry_book: bool = False,
                      success_keywords: list | None = None):
    """按顺序执行预订步骤。返回最后一步的 BookingResult。

    中间步骤的响应可配置 extract 正则，把捕获值写回 ctx 供后续步骤使用。
    """
    from poss_client import BookingResult

    last_req = None
    for idx, step in enumerate(steps):
        step_no = idx + 1
        curl_text = load_step_curl(step, b, idx)
        if not curl_text:
            raise BookingNotCalibrated(f"步骤 {step_no} 缺少 cURL 模板")
        req = client.build_booking_request(curl_text, ctx)
        leftover = find_unresolved(req["url"])
        if "json" not in (req["content_type"] or "").lower():
            leftover += find_unresolved(req["body"])
        if leftover:
            raise BookingNotCalibrated(
                f"步骤 {step_no} 模板里有取不到值的占位符: {', '.join(leftover)}"
            )

        if dry_book:
            print(f"[dry-book] --- 步骤 {step_no}/{len(steps)} ---")
            print(f"[dry-book] {req['method']} {mask_secrets(req['url'], ctx)}")
            for k, v in req["headers"].items():
                print(f"[dry-book]   {k}: {mask_secrets(v, ctx)}")
            if req["body"]:
                print(f"[dry-book] body: {mask_secrets(req['body'], ctx)}")
            if leftover:
                print(f"[dry-book] ❌ 占位符缺值: {leftover}")
            else:
                print(f"[dry-book] ✓ 占位符齐全")
            last_req = req
            continue

        r = client.s.post(
            req["url"], data=req["body"], headers=req["headers"],
            allow_redirects=False, timeout=30,
        )

        if r.status_code in (301, 302):
            loc = r.headers.get("Location", "")
            if "login" in loc.lower() or "loginhome" in loc.lower():
                raise SessionExpired(f"步骤 {step_no} 被重定向到登录页（302 → {loc}）")
            if idx == len(steps) - 1:
                # 最后一步出现非登录 302：结果不明，绝不再试
                return BookingResult(
                    False, f"HTTP 302 → {loc}（结果不明，请人工确认）",
                    (r.text or "")[:2000], inconclusive=True,
                )
            # 中间步骤：跟进到确认页
            url = loc if loc.startswith("http") else BASE + loc
            r = _follow_redirects(client, url, req["headers"])

        # 从当前响应提取字段给下一步用
        for key, pattern in step.get("extract", {}).items():
            m = re.search(pattern, r.text or "")
            if m:
                ctx[key] = m.group(1)
                print(f"[booking] 步骤 {step_no} 提取 {key} = {m.group(1)[:20]}...")

        if idx == len(steps) - 1:
            text = r.text or ""
            ok = r.status_code == 200 and any(k.lower() in text.lower() for k in (success_keywords or []))
            msg = "预订成功" if ok else f"HTTP {r.status_code} 未匹配成功标志: {text[:200]}"
            return BookingResult(ok, msg, text[:2000])

    if dry_book:
        # 演练只打印，不返回真实结果
        return BookingResult(False, "dry-book", "")
    # 正常流程不会到达这里（最后一步已返回）
    return BookingResult(False, "步骤执行异常", "")


def attempt_booking(client, cfg, state, now, dry_book=False) -> bool:
    """爆发式抢位。返回 True = 有结果（抢到 / 已提交待确认），False = 完全没抢到。"""
    b = cfg["booking"]
    today_key = now.date().isoformat()
    booked = state.setdefault("booked", {})
    if today_key in booked:
        prev = booked[today_key]
        note = prev.get("status") or f"{prev.get('gym', '?')} {prev.get('start', '?')}"
        print(f"[booking] {today_key} 已处理过（{note}），跳过（防重复扣费）")
        return True

    steps = load_booking_steps(b)
    for i, step in enumerate(steps):
        if not load_step_curl(step, b, i):
            raise BookingNotCalibrated(
                f"预订模板不完整：步骤 {i + 1} 缺少 cURL。"
                "请把 POSS 手动预订那条「提交」请求的 cURL 用 calibrate.py 导入。"
            )

    gym_names = b["gyms"] or [g["name"] for g in cfg["gyms"]]
    target = now.date() + timedelta(days=b["lead_days"])
    date_str = target.strftime(b["search_date_format"])
    window = [{"days": [target.weekday()], "from": b["from"], "to": b["to"]}]
    mode_desc = f"{len(steps)} 步 cURL 模板" if len(steps) > 1 else "cURL 模板"
    print(f"[booking] 目标：{date_str}（周{'一二三四五六日'[target.weekday()]}）"
          f" {b['from']}-{b['to']}，馆顺序 {gym_names}，提交方式 {mode_desc}")

    deadline = time.time() + (0 if dry_book else b["burst_seconds"])
    attempts = 0
    session_failures = 0
    max_session_failures = 3
    while True:
        attempts += 1
        for gym_name in gym_names:
            ctr = ctr_of(cfg, gym_name)
            try:
                raw = client.timetable(ctr, date_str, params=cfg["request"])
            except SessionExpired:
                session_failures += 1
                if session_failures > max_session_failures:
                    raise SessionExpired(
                        f"连续 {session_failures} 次会话失效，已放弃本轮抢位（继续重登会触发风控）"
                    )
                print(f"[booking] 查询时会话失效 → 重新登录（第 {session_failures} 次）")
                client.login()
                client.save_session(SESSION_FILE)
                continue
            cands = [s for s in parse_timetable(raw)
                     if slot_matches_target(s, window) and (s["left"] or 0) > 0]
            if not cands:
                continue
            best = pick_best(cands, b)
            ctx = build_booking_context(client, b, best, gym_name, ctr, target)

            if dry_book:
                print(f"[dry-book] 候选: {gym_name} {best['date']} {best['start']}-{best['end']} "
                      f"(剩 {best['left']})")
                run_booking_steps(client, b, steps, ctx, dry_book=True)
                return False

            try:
                res = run_booking_steps(client, b, steps, ctx, success_keywords=b["success_keywords"])
            except SessionExpired as e:
                session_failures += 1
                if session_failures > max_session_failures:
                    raise SessionExpired(
                        f"提交预订连续 {session_failures} 次会话失效，已放弃本轮"
                        f"（继续重登会触发风控）"
                    ) from e
                print(f"[booking] 提交时会话失效: {e} → 重新登录（第 {session_failures} 次）")
                client.login()
                continue
            session_failures = 0
            print(f"[booking] 第 {attempts} 轮 {gym_name} {best['start']}: {res.message}")

            if res.ok:
                booked[today_key] = {
                    "gym": gym_name, "date": best["date"], "start": best["start"],
                    "end": best["end"], "at": int(time.time()), "status": "booked",
                }
                save_state(state)
                notify_all(
                    "🎉 POSS 抢到了！",
                    f"{gym_name} {best['date']} {best['start']}–{best['end']}\n"
                    f"请登录 POSS 确认预订状态与押金扣除。\n"
                    f"（{now.strftime('%m-%d %H:%M')} 自动抢位）",
                )
                return True

            if res.inconclusive:
                booked[today_key] = {
                    "gym": gym_name, "date": best["date"], "start": best["start"],
                    "at": int(time.time()), "status": "inconclusive",
                }
                save_state(state)
                notify_all(
                    "❓ POSS 抢位结果不明，请立刻确认",
                    f"{gym_name} {best['date']} {best['start']}–{best['end']}\n"
                    f"服务端返回：{res.message}\n"
                    f"脚本已停止重试（避免重复扣费）。请马上去 POSS 看一眼订单状态。",
                )
                return True
        if dry_book or time.time() >= deadline:
            break
        time.sleep(b["retry_interval"])
    print(f"[booking] 爆发 {attempts} 轮未抢到")
    return False


# ==================== 蹲守提醒 ====================


def fetch_timetables(client, cfg):
    dates = cfg["request"].get("searchDates") or [""]

    def _grab():
        return {g["name"]: [client.timetable(g["ctrId"], d, params=cfg["request"]) for d in dates]
                for g in cfg["gyms"]}

    if client.load_session(SESSION_FILE):
        try:
            return _grab()
        except SessionExpired:
            pass
    client.login()
    client.save_session(SESSION_FILE)
    return _grab()


def monitor_fallback(client, cfg, now, args):
    targets = fallback_targets(cfg)
    data = fetch_timetables(client, cfg)

    state = load_state()
    slots = state.setdefault("slots", {})
    open_slots = []
    for gym in cfg["gyms"]:
        for raw in data[gym["name"]]:
            for s in parse_timetable(raw):
                if not slot_matches_target(s, targets) or (s["left"] or 0) <= 0:
                    continue
                open_slots.append({
                    "key": f"{gym['name']}|{s['date']}|{s['start']}",
                    "gym": gym["name"],
                    **{k: s[k] for k in ("date", "start", "end", "left")},
                })

    now_ts = int(time.time())
    to_alert = []
    for s in open_slots:
        prev = slots.get(s["key"], {})
        if prev.get("left", 0) <= 0 or now_ts - prev.get("last_notified", 0) > RENOTIFY_AFTER:
            to_alert.append(s)

    if to_alert:
        lines = [f"🔥 {s['gym']} {s['date']} {s['start']}–{s['end'] or '?'}：剩 {s['left']} 个名额"
                 for s in sorted(to_alert, key=lambda x: (x["date"], x["start"]))]
        content = "\n".join(lines) + f"\n\n抢位入口：{POSS_BOOK_URL}\n" \
                                     f"（{now.strftime('%m-%d %H:%M')} 抓到，手速就是一切）"
        print(f"发现空位！\n{content}")
        if not args.no_notify:
            notify_all("🏀 POSS 有空位啦，快抢！", content)
    else:
        print(f"[{now.strftime('%m-%d %H:%M')}] 目标时段暂无空位，继续蹲……")

    for key in list(slots):
        if not any(s["key"] == key for s in open_slots):
            slots[key]["left"] = 0
    for s in open_slots:
        e = slots.setdefault(s["key"], {})
        e["left"] = s["left"]
        if any(t["key"] == s["key"] for t in to_alert):
            e["last_notified"] = now_ts
    save_state(state)


# ==================== 主流程 ====================


def build_client():
    netid = os.environ.get("POSS_NETID", "").strip()
    password = os.environ.get("POSS_PASSWORD", "").strip()
    fb_user_id = os.environ.get("POSS_FB_USER_ID", "657032").strip()
    return PossClient(netid, password, fb_user_id)


def _report_template_gaps(client, cfg, steps: list, dumped: dict):
    """--dump 收尾：核对预订模板需要的占位符，真实时间表数据能否填上。"""
    b = cfg["booking"]
    target = hk_now().date() + timedelta(days=b["lead_days"])
    print("\n[calibrate] 预订模板占位符可用性检查")
    for gym in cfg["gyms"]:
        raw = dumped.get(gym["name"])
        if raw is None:
            continue
        try:
            slots = parse_timetable(raw)
        except ParseNeedsCalibration as e:
            print(f"  {gym['name']}: ❌ {e}")
            continue
        slot = slots[0]
        ctx = build_booking_context(client, b, slot, gym["name"], gym["ctrId"], target)
        all_leftover = []
        try:
            for idx, step in enumerate(steps):
                curl_text = load_step_curl(step, b, idx)
                if not curl_text:
                    all_leftover.append(f"步骤{idx + 1}无模板")
                    continue
                req = client.build_booking_request(curl_text, ctx)
                leftover = find_unresolved(req["url"])
                if "json" not in (req["content_type"] or "").lower():
                    leftover += find_unresolved(req["body"])
                if leftover:
                    all_leftover.append(f"步骤{idx + 1}: {', '.join(leftover)}")
                # 模拟从当前响应提取（没有真实响应，所以不覆盖 ctx）
        except Exception as e:  # noqa: BLE001
            print(f"  {gym['name']}: ❌ 模板渲染失败: {e}")
            continue
        verdict = "✓ 占位符全部可填，可上线" if not all_leftover else "❌ " + "; ".join(all_leftover)
        print(f"  {gym['name']}: 解析到 {len(slots)} 个时段 → {verdict}")
        print(f"    数据样本字段: {sorted(slot['raw'].keys())[:14]}")
    print("[calibrate] 若字段名与预期不符，把 dumps/ 里的 JSON 发给开发者校准解析器。")


def run(args):
    cfg = load_config()
    now = hk_now()
    client = build_client()

    # ---- 校准模式 ----
    if args.dump:
        fetch_timetables(client, cfg)  # 登录 + 建会话
        os.makedirs(DUMP_DIR, exist_ok=True)
        dates = cfg["request"].get("searchDates") or [""]
        dumped = {}
        for gym in cfg["gyms"]:
            safe = re.sub(r"[^\w-]", "_", gym["name"])
            for d in dates:
                raw = client.timetable(gym["ctrId"], d, params=cfg["request"])
                dumped[gym["name"]] = raw
                suffix = "" if d == "" else f"@{d}"
                p = os.path.join(DUMP_DIR, f"timetable_{safe}{suffix}.json")
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(raw, f, ensure_ascii=False, indent=2)
                print(f"[dump] {p}")
        r = client.s.get(POSS_BOOK_URL, timeout=30)
        with open(os.path.join(DUMP_DIR, "make_book.html"), "w", encoding="utf-8") as f:
            f.write(r.text)
        print(f"[dump] {DUMP_DIR}/make_book.html ({len(r.text)} bytes)")

        # 顺带核对：预订模板要的占位符，真实时间表数据给不给得起
        steps = load_booking_steps(cfg["booking"])
        if all(load_step_curl(s, cfg["booking"], i) for i, s in enumerate(steps)):
            _report_template_gaps(client, cfg, steps, dumped)
        else:
            print("[calibrate] 还没有完整 booking 模板，"
                  "抓一条「提交预订」请求后跑 calibrate.py --import")
        return

    # ---- 抢位 ----
    phase = booking_phase(cfg, now)
    if phase != "closed":
        if phase == "pre":
            print(f"[booking] 预热：等待至 {cfg['booking']['trigger_time']}")
            wait_until_trigger(cfg)
            now = hk_now()
        state = load_state()
        try:
            got = attempt_booking(client, cfg, state, now, dry_book=args.dry_book)
            if not got and not args.dry_book:
                _booking_failed_alert(cfg, state, now, args)
        except BookingNotCalibrated as e:
            print(f"[booking] 无法抢位：{e}")
            if not args.no_notify:
                notify_all("⚠️ POSS 抢位尚未校准", f"{e}\n\n本次仅执行蹲守提醒。")
    elif args.dry_book:
        print("[dry-book] 当前不在抢位窗口，仍演示抢位流程（查询 + 打印表单）")
        try:
            attempt_booking(client, cfg, load_state(), now, dry_book=True)
        except BookingNotCalibrated as e:
            print(f"[dry-book] {e}")

    # ---- 蹲守提醒 ----
    if cfg["fallback"].get("enabled", True):
        monitor_fallback(client, cfg, now, args)


def _booking_failed_alert(cfg, state, now, args):
    """「没抢到」提醒：每个触发日只推一次。"""
    key = now.date().isoformat()
    if state.get("booking_failed_notified") == key:
        return
    state["booking_failed_notified"] = key
    save_state(state)
    b = cfg["booking"]
    target = (now.date() + timedelta(days=b["lead_days"])).isoformat()
    if not args.no_notify:
        notify_all(
            "😤 POSS 没抢到，转入蹲守",
            f"目标：{target} {b['from']}-{b['to']}（{'/'.join(b['gyms']) or '全部馆'}）\n"
            f"已转入蹲守模式：有人取消放出空位会立即提醒你。",
        )


def main():
    parser = argparse.ArgumentParser(description="POSS 抢位器 / 蹲位器")
    parser.add_argument("--dump", action="store_true", help="校准模式：抓原始数据存 dumps/")
    parser.add_argument("--dry-book", action="store_true", help="演练抢位并打印表单，不真提交")
    parser.add_argument("--no-notify", action="store_true", help="不发通知（测试用）")
    args = parser.parse_args()

    load_env_file()

    try:
        run(args)
    except LoginFailed as e:
        print(f"[FATAL] 登录失败: {e}")
        _fail_alert(f"POSS 登录失败: {e}", args)
        sys.exit(1)
    except (SessionExpired, ParseNeedsCalibration) as e:
        print(f"[FATAL] {e}")
        _fail_alert(f"POSS 监控失明: {e}", args)
        sys.exit(1)
    except ValueError as e:  # 配置错误
        print(f"[FATAL] {e}")
        _fail_alert(f"POSS 配置错误: {e}", args)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"[FATAL] 未知错误: {type(e).__name__}: {e}")
        _fail_alert(f"POSS 未知错误: {type(e).__name__}: {e}", args)
        sys.exit(1)


def _fail_alert(msg, args):
    if args.no_notify:
        return
    state = load_state()
    now_ts = int(time.time())
    if now_ts - state.get("last_fail_alert", 0) < FAIL_ALERT_INTERVAL:
        print("[fail-alert] 距上次警报太近，跳过")
        return
    state["last_fail_alert"] = now_ts
    save_state(state)
    notify_all("🚨 POSS 蹲位器失明了", msg + "\n\n请检查 GitHub Secrets / 账号状态 / 配置。")


if __name__ == "__main__":
    main()
