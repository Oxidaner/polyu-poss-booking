#!/usr/bin/env python3
"""
POSS 选课 / 抢位目标选择器（本地 UI）
=====================================
跑起来，浏览器里点选「要抢哪一节课」，直接写进 config.json 的 booking.targets。

  python3 poss_ui.py            # 起服务并打开浏览器
  python3 poss_ui.py --port 9000 --no-browser

为什么必须起本地服务而不是一个静态 HTML：
  · 要带着你的 POSS 会话去查 timetable.json（跨域 + 登录态，浏览器直接调不了）；
  · 要能写 config.json（网页沙箱写不了你磁盘上的文件）。

安全：只绑 127.0.0.1，局域网里别人碰不到；凭据只从 .env / 环境变量读，不落盘。
"""

import argparse
import json
import os
import re
import threading
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from poss_client import (
    DEFAULT_BODY,
    LoginFailed,
    PROTECTED_BODY_KEYS,
    PossClient,
    SessionExpired,
    parse_selects,
    to_curl,
)
import calibrate
import monitor as M

CONFIG_FILE = os.environ.get("CONFIG_FILE", "config.json")
DEFAULT_PORT = 8765
WINDOW_DAYS = 8          # POSS 只放「今天起 8 天」的场
PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][\w-]*(?::[^}]*)?\}")


def config_path() -> str:
    """配置路径只有一个真相来源：monitor.CONFIG_FILE（支持 CONFIG_FILE 环境变量）。"""
    return M.CONFIG_FILE

_lock = threading.Lock()
_client: PossClient | None = None
_selects_cache: list = []


# ==================== 会话 ====================

def get_client(force_login: bool = False) -> PossClient:
    global _client
    if _client is None:
        _client = M.build_client()
    if force_login:
        _client.login()
        return _client
    _client.ensure_login()
    return _client


# ==================== 配置读写 ====================

def load_config_raw() -> dict:
    p = config_path()
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def deep_merge(base, patch):
    """把 patch 递归合并进 base：两边都是 dict 就递归，否则 patch 覆盖。"""
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    out = dict(base)
    for k, v in patch.items():
        cur = out.get(k)
        out[k] = deep_merge(cur, v) if isinstance(v, dict) and isinstance(cur, dict) else v
    return out


def with_defaults(cfg: dict) -> dict:
    """把内置默认值补进配置。

    `request` 的默认值住在 poss_client.DEFAULT_BODY 里（bookType/dataSetId/actvId…），
    不补的话 UI 上那几个框会是空的 —— 用户看到的是「没配」，实际却有生效值。
    """
    cfg = dict(cfg)
    booking = dict(M.DEFAULT_BOOKING)
    booking.update(cfg.get("booking") or {})
    cfg["booking"] = booking
    cfg.setdefault("gyms", M.DEFAULT_GYMS)
    fb = {"enabled": True}
    fb.update(cfg.get("fallback") or {})
    cfg["fallback"] = fb
    req = dict(DEFAULT_BODY)
    req.update(cfg.get("request") or {})
    cfg["request"] = req
    return cfg


def effective_config() -> dict:
    """补齐默认值后的完整配置（UI 展示与校验都用它）。"""
    return with_defaults(load_config_raw())


def save_config(patch: dict) -> dict:
    """把 UI 提交的配置片段合并进 config.json。

    先合并 → 补默认值 → 校验，**校验不过就绝不落盘**，
    否则坏配置进了仓库、等 Actions 跑起来才炸。
    """
    cfg = with_defaults(deep_merge(load_config_raw(), patch or {}))
    M._validate_config(cfg)
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def save_targets(targets: list) -> dict:
    """只改 booking.targets，其余字段原样保留。"""
    return save_config({"booking": {"targets": targets}})


# ---- 账号与通知（走 .env，绝不进 config.json）----
# config.json 是要提交进仓库的，凭据放进去等于把密码贴到 GitHub 上。
ENV_FILE = os.environ.get("ENV_FILE", ".env")

# 表单字段 → 环境变量名
ENV_FIELDS = {
    "netid": "POSS_NETID",
    "fbUserId": "POSS_FB_USER_ID",
    "pushplus": "PUSHPLUS_TOKEN",
    "serverchan": "SERVERCHAN_KEY",
    "smtpHost": "SMTP_HOST",
    "smtpPort": "SMTP_PORT",
    "smtpUser": "SMTP_USER",
    "mailTo": "MAIL_TO",
}
# 这三个只在「填了」的时候才覆盖，留空表示不改（避免每次保存都把密码清掉）
ENV_SECRET_FIELDS = {"password": "POSS_PASSWORD", "smtpPass": "SMTP_PASS"}


def read_env() -> dict:
    """读 .env 成 dict。文件是 UI 管理的真相来源。"""
    out = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, v = s.split("=", 1)
                    out[k.strip()] = v.strip()
    return out


def write_env(updates: dict) -> str:
    """把键值写进 .env：保留原有注释与其他行，只改指定键；权限压到 0600。"""
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
    seen = set()
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                out.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")
    try:
        os.chmod(ENV_FILE, 0o600)      # 只有自己能读
    except OSError:
        pass
    for k, v in updates.items():       # 立刻生效，免得还要重启
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    M.reload_notify_env()
    return ENV_FILE


def account_status() -> dict:
    """账号/通知配置状态。**密码永不回传给前端**，只说配没配。"""
    env = read_env()
    return {
        "ok": True,
        "env_file": os.path.abspath(ENV_FILE),
        "exists": os.path.exists(ENV_FILE),
        "netid": env.get("POSS_NETID", ""),
        "password_set": bool(env.get("POSS_PASSWORD")),
        "ready": bool(env.get("POSS_NETID") and env.get("POSS_PASSWORD")),
        "fbUserId": env.get("POSS_FB_USER_ID") or os.environ.get("POSS_FB_USER_ID", "657032"),
        "pushplus_set": bool(env.get("PUSHPLUS_TOKEN")),
        "serverchan_set": bool(env.get("SERVERCHAN_KEY")),
        "smtpHost": env.get("SMTP_HOST", ""),
        "smtpPort": env.get("SMTP_PORT", ""),
        "smtpUser": env.get("SMTP_USER", ""),
        "smtpPass_set": bool(env.get("SMTP_PASS")),
        "mailTo": env.get("MAIL_TO", ""),
        "channels": sum(bool(env.get(k)) for k in
                        ("PUSHPLUS_TOKEN", "SERVERCHAN_KEY", "SMTP_PASS")),
    }


def save_account(payload: dict) -> dict:
    """保存登录凭据与通知渠道到 .env。密码留空 = 不改。"""
    updates = {}
    for field, key in ENV_FIELDS.items():
        if field in payload:
            updates[key] = str(payload.get(field) or "").strip()
    for field, key in ENV_SECRET_FIELDS.items():
        v = payload.get(field)
        if v:                                  # 留空 = 保持原密码
            updates[key] = str(v).strip()
    if not updates:
        raise ValueError("没有要保存的内容")
    # 表单里 NetID 是预填的，提交成空 = 用户手动删掉了 → 大概率是误操作，直接拒掉。
    # 也顺手挡住「只保存了通知渠道、却一个 NetID 都没有」这种半截配置。
    merged_netid = updates.get("POSS_NETID", read_env().get("POSS_NETID", "")).strip()
    if not merged_netid:
        raise ValueError("NetID 不能为空（确实要清空请直接编辑 .env）")
    write_env(updates)
    return account_status()


def test_notify() -> dict:
    """发一条测试通知，验证渠道真的通。"""
    M.reload_notify_env()
    n = M.notify_all("POSS 通知测试", "收到这条说明通知渠道配好了。")
    if n == 0:
        raise ValueError("没有任何通知渠道可用（PushPlus / Server酱 / 邮件至少配一个）")
    return {"channels": n}


def allowed_param_keys() -> set:
    """允许从 UI 透传给 timetable 的参数名。

    只认「已知默认键」+「POSS 页面上真实存在的 <select name>」——
    这样站点换了字段名能自动跟上（不用改代码），同时挡住乱塞参数。
    """
    keys = set(DEFAULT_BODY)
    keys.update(s["name"] for s in _selects_cache if s.get("name"))
    return keys - PROTECTED_BODY_KEYS


# ---- 预订模板（.curl 文件，不是 config.json）----

def template_status(cfg: dict | None = None) -> dict:
    """每一步的 cURL 模板导入了没有 —— 没导入就根本抢不了位。"""
    b = (cfg or effective_config())["booking"]
    steps = M.load_booking_steps(b)
    out = []
    for i, st in enumerate(steps):
        path = st.get("curl_file") or b.get("curl_file") or M.BOOKING_CURL_FILE
        txt = M.load_step_curl(st, b, i)
        out.append({
            "step": i + 1,
            "file": str(path),
            "imported": bool(txt),
            "bytes": len(txt or ""),
            "extract": sorted((st.get("extract") or {}).keys()),
        })
    return {"steps": out, "ready": bool(out) and all(s["imported"] for s in out)}


def import_template(idx: int, curl_text: str) -> dict:
    """把一条抓包 cURL 模板化后写进第 idx 步的 .curl 文件。

    刻意**不写进 config.json** —— 模板里可能残留会话凭据痕迹，
    而 config.json 是要提交进仓库的。
    """
    if not (curl_text or "").strip():
        raise ValueError("cURL 文本是空的")
    cfg = effective_config()
    b = cfg["booking"]
    steps = M.load_booking_steps(b)
    if not (0 <= idx < len(steps)):
        raise ValueError(f"没有第 {idx + 1} 步（当前共 {len(steps)} 步）")
    info = calibrate.import_curl(curl_text)
    text = to_curl(info["method"], info["url"], info["headers"], info["data"])
    path = steps[idx].get("curl_file") or b.get("curl_file") or M.BOOKING_CURL_FILE
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n")
    return {
        "step": idx + 1, "file": path, "bytes": len(text),
        "method": info["method"], "mode": info["mode"],
        "placeholders": sorted(set(PLACEHOLDER_RE.findall(text))),
    }


# ==================== 时间表 → 网格 ====================

def date_options(selects: list, today=None) -> list:
    """Date 下拉框的可选项：优先用 POSS 页面上的日期，否则今天起 8 天。"""
    today = today or M.hk_now().date()
    for s in selects:
        if s["role"] != "date":
            continue
        ds = []
        for o in s["options"]:
            d = M._parse_date(o["value"]) or M._parse_date(o["label"])
            if d:
                ds.append(d)
        if len(ds) >= 2:
            return [d.isoformat() for d in sorted(set(ds))]
    return [(today + timedelta(days=i)).isoformat() for i in range(WINDOW_DAYS)]


def build_window(start: str | None, days: int = WINDOW_DAYS) -> list:
    """网格的列 = 从 start 起 days 天。

    有意不直接用页面 date 下拉框的选项当列 —— POSS 那个框是「搜索起始日」，
    选项可能只有两三条；而真正的可订窗口是「今天起 8 天」，照它画会缺列。
    """
    base = M._parse_date(start) or M.hk_now().date()
    return [(base + timedelta(days=i)).isoformat() for i in range(days)]


DEFAULT_TIMES = ["08:30", "09:30", "10:30", "11:30", "12:30", "13:30", "14:30",
                 "15:30", "16:30", "17:30", "18:30", "19:30", "20:30", "21:30"]


def build_grid(slots: list, window: list) -> dict:
    """把时段摊成 日期×时间 的网格（对齐 POSS 那张彩色表）。"""
    times = sorted({s["start"] for s in slots if M._parse_hm(s["start"])},
                   key=lambda x: M._parse_hm(x))
    if not times:
        times = list(DEFAULT_TIMES)
    cells = {}
    for s in slots:
        d = M._parse_date(s["date"])
        if not d:
            continue
        key = f"{d.isoformat()}|{s['start']}"
        # 同格多条时保留名额多的那条
        prev = cells.get(key)
        if prev and (prev.get("left") or 0) >= (s["left"] or 0):
            continue
        cells[key] = {
            "date": d.isoformat(), "start": s["start"], "end": s["end"],
            "left": s["left"], "price": s["price"], "deposit": s["deposit"],
            "status": s["status"],
        }
    return {"dates": window, "times": times, "cells": cells}


# ==================== HTTP ====================

class Handler(BaseHTTPRequestHandler):
    server_version = "PossUI/1.0"

    def log_message(self, fmt, *a):        # 静音，别把 timetable 内容刷屏
        pass

    # ---- 工具 ----
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _err(self, msg, code=400):
        self._json({"ok": False, "error": str(msg)}, code)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---- 路由 ----
    def do_GET(self):                                   # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            elif u.path == "/api/meta":
                self._json(self.api_meta())
            elif u.path == "/api/timetable":
                self._json(self.api_timetable(q))
            elif u.path == "/api/targets":
                self._json({"ok": True, "targets": load_config_raw().get("booking", {}).get("targets", [])})
            elif u.path == "/api/config":
                self._json(self.api_get_config())
            elif u.path == "/api/template":
                self._json({"ok": True, "status": template_status()})
            elif u.path == "/api/account":
                self._json(account_status())
            else:
                self._err("not found", 404)
        except ValueError as e:
            self._err(e, 400)
        except (LoginFailed, SessionExpired) as e:
            self._err(f"{type(e).__name__}: {e}", 401)
        except Exception as e:                          # noqa: BLE001
            self._err(f"{type(e).__name__}: {e}", 500)

    def do_POST(self):                                  # noqa: N802
        u = urlparse(self.path)
        try:
            if u.path == "/api/login":
                with _lock:
                    get_client(force_login=True)
                self._json({"ok": True})
            elif u.path == "/api/targets":
                body = self._read_body()
                targets = body.get("targets")
                if not isinstance(targets, list):
                    return self._err("targets 必须是数组")
                with _lock:
                    cfg = save_targets(targets)
                print(f"[ui] 已写入 {config_path()}：{len(targets)} 个目标")
                self._json({"ok": True, "count": len(targets),
                            "targets": cfg["booking"]["targets"]})
            elif u.path == "/api/config":
                body = self._read_body()
                with _lock:
                    cfg = save_config(body)
                print(f"[ui] 已写入 {config_path()}（设置）")
                self._json({"ok": True, "config": cfg})
            elif u.path == "/api/template":
                body = self._read_body()
                step = int(body.get("step") or 1) - 1
                with _lock:
                    info = import_template(step, body.get("curl") or "")
                print(f"[ui] 已导入第 {info['step']} 步模板 → {info['file']}（{info['bytes']} 字节）")
                self._json({"ok": True, "info": info, "status": template_status()})
            elif u.path == "/api/account":
                body = self._read_body()
                global _client
                with _lock:
                    acct = save_account(body)
                    _client = None          # 凭据变了，丢弃旧会话
                print(f"[ui] 已写入 {ENV_FILE}（账号与通知）")
                self._json({"ok": True, "account": acct})
            elif u.path == "/api/notify-test":
                with _lock:
                    self._json({"ok": True, **test_notify()})
            else:
                self._err("not found", 404)
        except ValueError as e:                         # 配置校验失败
            self._err(e, 400)
        except (LoginFailed, SessionExpired) as e:
            self._err(f"{type(e).__name__}: {e}", 401)
        except Exception as e:                          # noqa: BLE001
            self._err(f"{type(e).__name__}: {e}", 500)

    # ---- API ----
    def api_meta(self):
        global _selects_cache
        with _lock:
            client = get_client()
            html = client.fetch_make_book_html()
        _selects_cache = parse_selects(html)
        selects = _selects_cache
        roles = {}
        for s in selects:
            roles.setdefault(s["role"], s)
        cfg = with_defaults(load_config_raw())
        booking = cfg["booking"]
        today = M.hk_now().date()
        return {
            "ok": True,
            "selects": selects,
            "roles": roles,
            "window": build_window(None),
            "date_options": date_options(selects, today),
            "today": today.isoformat(),
            "targets": booking.get("targets") or [],
            "booking": {
                "trigger_time": booking["trigger_time"],
                "lead_days": booking["lead_days"],
                "from": booking["from"],
                "to": booking["to"],
                "trigger_days": booking["trigger_days"],
            },
            "template": template_status(),
            "config_file": os.path.abspath(config_path()),
        }

    def api_get_config(self):
        cfg = effective_config()
        b = cfg["booking"]
        return {
            "ok": True,
            "config": cfg,
            "template": template_status(cfg),
            "defaults": M.DEFAULT_BOOKING,
            "derived": {
                "mode": "targets" if b.get("targets") else "derived",
                "release_rule": f"目标日 − {b['lead_days']} 天 {b['trigger_time']}",
                "config_file": os.path.abspath(config_path()),
                # booking.gyms 会盖掉顶层 gyms 的顺序，UI 里要提示
                "booking_gyms_override": bool(b.get("gyms")),
            },
        }

    def api_timetable(self, q):
        ctr = (q.get("ctrId") or [""])[0].strip()
        if not ctr:
            raise ValueError("缺少 ctrId（先在「中心」里选一个）")
        params = {}
        for key, vals in q.items():
            if key in ("ctrId", "start"):
                continue
            v = (vals or [""])[0].strip()
            if v and key in allowed_param_keys():
                params[key] = v
        with _lock:
            client = get_client()
            raw = client.timetable(ctr, "", params=params)
        window = build_window((q.get("start") or [""])[0] or None)
        try:
            slots = M.parse_timetable(raw)
        except M.ParseNeedsCalibration as e:
            # 解析不出来也要把原始数据交出去，否则用户没法校准
            return {"ok": True, "raw": raw, "window": window, "grid": None,
                    "slot_count": 0, "warning": str(e)}
        return {"ok": True, "raw": raw, "window": window,
                "grid": build_grid(slots, window),
                "slot_count": len(slots)}


# ==================== 前端 ====================

PAGE = r"""<!DOCTYPE html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>POSS 抢位控制台</title>
<style>
  :root{
    --bg:#f5f6f8; --panel:#fff; --line:#e6e9ef; --line-2:#eef1f6;
    --ink:#12151c; --dim:#697386; --faint:#98a1b3;
    --accent:#4f46e5; --accent-h:#4338ca;
    --free:#ec4899; --paid:#3b82f6; --gray:#c9d0da; --gray-ink:#5a6474;
    --nodata:#edf1f6; --ok:#0f766e; --err:#b42318;
    --r:13px; --r-sm:9px;
    --sh:0 1px 2px rgba(16,24,40,.05), 0 1px 3px rgba(16,24,40,.04);
    --sh-lg:0 6px 20px rgba(16,24,40,.10);
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{
    background:var(--bg); color:var(--ink);
    font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB",
         "Microsoft YaHei",Inter,"Helvetica Neue",Arial,sans-serif;
    -webkit-font-smoothing:antialiased;
  }
  h1,h2,h3{margin:0;font-weight:650}
  .sp{flex:1}

  /* ---------- 顶栏 ---------- */
  header{
    position:sticky; top:0; z-index:30; height:64px; padding:0 24px;
    display:flex; align-items:center; gap:13px;
    background:rgba(255,255,255,.85); backdrop-filter:saturate(180%) blur(14px);
    -webkit-backdrop-filter:saturate(180%) blur(14px);
    border-bottom:1px solid var(--line);
  }
  .logo{
    width:30px; height:30px; border-radius:10px; flex:none;
    background:linear-gradient(135deg,#6366f1,#a855f7); color:#fff;
    display:grid; place-items:center; font-weight:700; font-size:14px;
    box-shadow:0 2px 10px rgba(99,102,241,.38);
  }
  .brand h1{font-size:15px; letter-spacing:-.01em}
  .brand .sub{font-size:11.5px; color:var(--faint); margin-top:1px}

  .pill{
    font-size:12px; color:var(--dim); background:#f2f4f8; border:1px solid var(--line);
    padding:4px 11px; border-radius:999px; max-width:46ch;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
    transition:.2s;
  }
  .pill.ok{color:var(--ok); background:#f0fdfa; border-color:#b6ece5}
  .pill.err{color:var(--err); background:#fff5f4; border-color:#fbd3ce}
  .pill.busy{color:#5b21b6; background:#f5f3ff; border-color:#ddd6fe}

  /* ---------- 按钮 ---------- */
  button{
    font:inherit; font-size:13px; padding:7px 13px; border-radius:var(--r-sm);
    border:1px solid var(--line); background:#fff; color:var(--ink); cursor:pointer;
    box-shadow:var(--sh); display:inline-flex; align-items:center; gap:6px;
    white-space:nowrap;
    transition:transform .13s, box-shadow .13s, border-color .13s, background .13s;
  }
  button:hover{border-color:#d3d9e3; transform:translateY(-1px); box-shadow:var(--sh-lg)}
  button:active{transform:translateY(0)}
  button:focus-visible{outline:none; box-shadow:0 0 0 3px rgba(79,70,229,.25)}
  button.primary{
    background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600;
    box-shadow:0 1px 2px rgba(79,70,229,.28), 0 5px 14px rgba(79,70,229,.24);
  }
  button.primary:hover{background:var(--accent-h); border-color:var(--accent-h)}
  button.primary:disabled{opacity:.45; cursor:not-allowed; transform:none; box-shadow:none}
  button.dirty::after{
    content:""; width:7px; height:7px; border-radius:50%; background:#fbbf24;
    box-shadow:0 0 0 3px rgba(251,191,36,.28);
  }

  /* ---------- 布局 ---------- */
  .wrap{max-width:1560px; margin:0 auto; padding:20px 24px 72px}
  .panel{
    background:var(--panel); border:1px solid var(--line); border-radius:var(--r);
    padding:16px 18px; margin-bottom:16px; box-shadow:var(--sh);
  }
  .panel-head{display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:13px}
  .panel-head h2{font-size:13.5px; letter-spacing:-.005em}
  .panel-head .hint{margin:0}

  .toolbar{
    display:grid; gap:12px;
    grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
    align-items:end;
  }
  .field{display:flex; flex-direction:column; gap:6px; min-width:0}
  .field label{
    font-size:11px; font-weight:650; color:var(--faint);
    letter-spacing:.06em; text-transform:uppercase;
  }
  .field.act button{width:100%; justify-content:center; height:38px}
  select{
    font:inherit; font-size:13.5px; width:100%; height:38px;
    padding:0 34px 0 11px; color:var(--ink); background:#fff;
    border:1px solid var(--line); border-radius:var(--r-sm);
    appearance:none; -webkit-appearance:none; cursor:pointer;
    transition:border-color .13s, box-shadow .13s;
    background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M3 4.6L6 7.6L9 4.6' fill='none' stroke='%23697386' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/></svg>");
    background-repeat:no-repeat; background-position:right 11px center;
  }
  select:hover{border-color:#cfd6e2}
  select:focus{outline:none; border-color:var(--accent); box-shadow:0 0 0 3px rgba(79,70,229,.13)}

  input[type=text],input[type=time],input[type=number]{
    font:inherit; font-size:13.5px; width:100%; height:38px; padding:0 11px;
    border:1px solid var(--line); border-radius:var(--r-sm);
    background:#fff; color:var(--ink); transition:border-color .13s, box-shadow .13s;
  }
  input[type=time]{font-variant-numeric:tabular-nums}
  input:focus{outline:none; border-color:var(--accent); box-shadow:0 0 0 3px rgba(79,70,229,.13)}

  textarea{
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-size:12.5px; line-height:1.55; width:100%; padding:9px 11px;
    border:1px solid var(--line); border-radius:var(--r-sm);
    background:#fff; color:var(--ink); resize:vertical;
    transition:border-color .13s, box-shadow .13s;
  }
  textarea:focus{outline:none; border-color:var(--accent); box-shadow:0 0 0 3px rgba(79,70,229,.13)}

  /* auto-fill 而不是 auto-fit：后者会把空轨道折叠掉，
     于是「2 个开关」那行变成 2 列各占一半，跟下面「6 个数字框」的列宽对不齐 */
  .rows{display:grid; gap:12px; grid-template-columns:repeat(auto-fill,minmax(148px,1fr))}
  .rows.wide{grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}
  .row-actions{display:flex; gap:10px; flex-wrap:wrap; margin-top:16px; padding-top:16px;
               border-top:1px solid var(--line-2)}

  .stats{display:flex; flex-wrap:wrap; gap:8px; margin-top:14px}
  .stat{
    display:inline-flex; align-items:baseline; gap:6px; font-size:12px; color:var(--dim);
    background:#f7f8fb; border:1px solid var(--line-2); padding:5px 11px; border-radius:999px;
  }
  .stat b{color:var(--ink); font-weight:650; font-variant-numeric:tabular-nums; font-size:12.5px}
  .hint{font-size:12px; color:var(--faint); margin-top:10px; line-height:1.6}
  .warn{
    background:#fffbeb; border:1px solid #fde68a; color:#92400e;
    padding:11px 14px; border-radius:var(--r-sm); font-size:12.5px; margin-bottom:14px;
  }

  /* ---------- 网格 ---------- */
  .gridwrap{
    overflow:auto; max-height:62vh; border:1px solid var(--line-2);
    border-radius:var(--r-sm); background:var(--panel);
    transition:opacity .15s;
  }
  .gridwrap.busy{opacity:.45; pointer-events:none}
  table.grid{border-collapse:collapse; width:100%; font-size:12px}
  table.grid th{
    position:sticky; top:0; z-index:2; background:var(--panel);
    padding:9px 6px 8px; font-weight:650; color:var(--ink); white-space:nowrap;
    border-bottom:1px solid var(--line);
  }
  table.grid th .dow{
    display:block; font-size:10.5px; font-weight:500; color:var(--faint); margin-top:1px;
  }
  table.grid th.tcol{
    left:0; z-index:3; text-align:right; padding-right:11px; min-width:68px;
    font-variant-numeric:tabular-nums; font-weight:500; color:var(--dim);
    border-right:1px solid var(--line);
  }
  table.grid thead th.tcol{z-index:4}
  table.grid th.today{color:var(--accent)}
  table.grid th.today .dow{color:var(--accent)}
  table.grid th.today::after{
    content:""; position:absolute; left:50%; transform:translateX(-50%); bottom:2px;
    width:16px; height:2px; border-radius:2px; background:var(--accent);
  }
  table.grid td{padding:2px; border:none}

  .cell{
    height:34px; border-radius:8px; display:grid; place-items:center; cursor:pointer;
    user-select:none; font-size:11.5px; font-weight:650; position:relative;
    background-image:linear-gradient(180deg,rgba(255,255,255,.17),rgba(0,0,0,.05));
    transition:transform .12s, box-shadow .12s;
  }
  .cell:hover{transform:translateY(-1px); box-shadow:0 3px 10px rgba(16,24,40,.22); z-index:1}
  .cell.unavailable{background-color:var(--gray); color:var(--gray-ink)}
  .cell.free{background-color:var(--free); color:#fff}
  .cell.paid{background-color:var(--paid); color:#fff}
  .cell.unknown{background-color:#a5cdf5; color:#12395f}
  .cell.nodata{
    background-color:var(--nodata); color:#c7cfdb;
    background-image:none; box-shadow:inset 0 0 0 1px #e2e8f1;
  }
  .cell.sel{
    box-shadow:0 0 0 2px var(--panel), 0 0 0 4px var(--accent);
    transform:translateY(-1px); z-index:1;
  }
  .cell.sel::after{
    content:""; position:absolute; top:4px; right:4px; width:6px; height:6px;
    border-radius:50%; background:#fff; box-shadow:0 0 0 1.5px rgba(0,0,0,.28);
  }

  .legend{display:flex; flex-wrap:wrap; gap:8px; margin-top:14px}
  .lg{
    display:inline-flex; align-items:center; gap:7px; font-size:11.5px; color:var(--dim);
    background:#f7f8fb; border:1px solid var(--line-2); padding:5px 11px; border-radius:999px;
  }
  .lg i{width:11px; height:11px; border-radius:3.5px; display:inline-block; flex:none}

  .placeholder{padding:64px 20px; text-align:center; color:var(--faint); font-size:13.5px}
  .placeholder .big{font-size:28px; margin-bottom:10px; opacity:.45}

  /* ---------- 目标列表 ---------- */
  .chips{display:grid; grid-template-columns:repeat(auto-fill,minmax(310px,1fr)); gap:10px}
  .chip{
    display:flex; align-items:center; gap:12px; padding:11px 12px 11px 15px;
    border:1px solid var(--line); border-radius:var(--r-sm); background:#fff;
    position:relative; overflow:hidden;
    transition:border-color .15s, box-shadow .15s, transform .15s;
  }
  .chip::before{
    content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--accent);
  }
  .chip:hover{border-color:#d3d9e3; box-shadow:var(--sh-lg); transform:translateY(-1px)}
  .chip .body{flex:1; min-width:0}
  .chip .t{
    font-weight:650; font-size:13.5px; font-variant-numeric:tabular-nums;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  }
  .chip .m{
    font-size:11.5px; color:var(--faint); margin-top:2px;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  }
  .chip button{opacity:0; padding:4px 10px; font-size:12px}
  .chip:hover button, .chip:focus-within button{opacity:1}
  .empty{
    grid-column:1/-1; text-align:center; color:var(--faint); font-size:13px;
    padding:28px 0; border:1px dashed var(--line); border-radius:var(--r-sm); background:#fafbfd;
  }

  /* ---------- 设置 ---------- */
  .sec{border-top:1px solid var(--line-2); margin-top:18px; padding-top:16px}
  .sec:first-of-type{border-top:none; margin-top:0; padding-top:0}
  .sec > h3{
    font-size:11.5px; font-weight:650; color:var(--dim);
    letter-spacing:.04em; margin-bottom:11px;
  }
  .switch{
    display:inline-flex; align-items:center; gap:9px; cursor:pointer; user-select:none;
    font-size:13.5px; height:38px;
  }
  .switch input{position:absolute; opacity:0; width:0; height:0}
  .switch .track{
    width:38px; height:22px; border-radius:999px; background:#d5dae3;
    position:relative; transition:background .18s; flex:none;
  }
  .switch .track::after{
    content:""; position:absolute; top:2px; left:2px; width:18px; height:18px;
    border-radius:50%; background:#fff; box-shadow:0 1px 3px rgba(0,0,0,.25);
    transition:transform .18s;
  }
  .switch input:checked + .track{background:var(--accent)}
  .switch input:checked + .track::after{transform:translateX(16px)}
  .switch input:focus-visible + .track{box-shadow:0 0 0 3px rgba(79,70,229,.25)}

  .days{display:flex; gap:7px; flex-wrap:wrap}
  .day{cursor:pointer; user-select:none}
  .day input{position:absolute; opacity:0; width:0; height:0}
  .day span{
    display:inline-block; padding:7px 13px; font-size:12.5px; border-radius:8px;
    border:1px solid var(--line); background:#fff; transition:.13s;
  }
  .day input:checked + span{
    background:var(--accent); border-color:var(--accent); color:#fff; font-weight:650;
  }
  .day input:focus-visible + span{box-shadow:0 0 0 3px rgba(79,70,229,.25)}

  .tpl{
    display:flex; align-items:center; gap:10px; padding:10px 12px;
    border:1px solid var(--line); border-radius:var(--r-sm); background:#fbfcfe;
    margin-bottom:9px; font-size:12.5px;
  }
  .tpl .dot{width:8px; height:8px; border-radius:50%; flex:none}
  .tpl .dot.ok{background:#10b981; box-shadow:0 0 0 3px rgba(16,185,129,.18)}
  .tpl .dot.no{background:#ef4444; box-shadow:0 0 0 3px rgba(239,68,68,.18)}
  .tpl code{font-family:ui-monospace,Menlo,Consolas,monospace; font-size:11.5px; color:var(--dim)}
  .tpl .sp{flex:1}

  details.adv > summary{
    cursor:pointer; color:var(--dim); font-size:13px; font-weight:600; list-style:none;
    display:inline-flex; align-items:center; gap:7px; user-select:none;
    padding:8px 0; margin-top:6px;
  }
  details.adv > summary::-webkit-details-marker{display:none}
  details.adv > summary::before{content:"▸"; font-size:10px; transition:transform .15s}
  details.adv[open] > summary::before{transform:rotate(90deg)}

  details.raw{margin-top:14px}
  details.raw > summary{
    cursor:pointer; color:var(--dim); font-size:12.5px; list-style:none;
    display:inline-flex; align-items:center; gap:6px; user-select:none;
  }
  details.raw > summary::-webkit-details-marker{display:none}
  details.raw > summary::before{content:"▸"; font-size:10px; transition:transform .15s}
  details.raw[open] > summary::before{transform:rotate(90deg)}
  pre{
    background:#0f1117; color:#d7dae0; padding:14px; border-radius:var(--r-sm);
    overflow:auto; max-height:340px; font-size:11.5px; line-height:1.5; margin:10px 0 0;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }

  /* ---------- 窄屏 ---------- */
  @media (max-width:760px){
    header{height:auto; min-height:56px; padding:10px 16px; flex-wrap:wrap; gap:8px 10px}
    .brand .sub{display:none}
    .pill{order:9; flex-basis:100%; max-width:none; text-align:center}
    .wrap{padding:14px 14px 56px}
    .panel{padding:14px 13px}
    .chip button{opacity:1}
  }
</style>
</head>
<body>

<header>
  <div class="logo">P</div>
  <div class="brand">
    <h1>POSS 抢位控制台</h1>
    <div class="sub">选目标 · 调参数 · 一步到位</div>
  </div>
  <span id="status" class="pill"></span>
  <span class="sp"></span>
  <button id="reload">重新读取</button>
  <button id="relogin">重新登录</button>
</header>

<div class="wrap">
  <div id="banner"></div>

  <!-- ⓪ 登录与通知 -->
  <section class="panel">
    <div class="panel-head">
      <h2>登录与通知</h2>
      <span class="hint" id="acctStatus" style="margin:0"></span>
      <span class="sp"></span>
      <button id="acctToggle">修改</button>
    </div>
    <div id="acctBody" style="display:none">
      <div class="sec">
        <h3>POSS 账号（写进 .env，不进仓库）</h3>
        <div class="rows wide">
          <div class="field"><label>NetID</label>
            <input type="text" id="ac_netid" autocomplete="username" placeholder="例如 21012345d"></div>
          <div class="field"><label>NetPassword</label>
            <input type="password" id="ac_password" autocomplete="current-password"
                   placeholder="留空 = 不改密码"></div>
          <div class="field"><label>fbUserId（一般不用改）</label>
            <input type="text" id="ac_fbuserid" placeholder="657032"></div>
        </div>
        <div class="hint">密码只写进 <code id="acPath">.env</code>（权限 0600，已 gitignore），
          <b>永远不会写进 config.json</b>，也不会回显到页面上。</div>
      </div>

      <div class="sec">
        <h3>通知渠道（至少配一个）</h3>
        <div class="rows wide">
          <div class="field"><label>PushPlus Token</label>
            <input type="password" id="ac_pushplus" placeholder="留空 = 不改"></div>
          <div class="field"><label>Server酱 SendKey</label>
            <input type="password" id="ac_serverchan" placeholder="留空 = 不改"></div>
        </div>
        <div class="rows wide" style="margin-top:12px">
          <div class="field"><label>SMTP 服务器</label>
            <input type="text" id="ac_smtphost" placeholder="smtp.qq.com"></div>
          <div class="field"><label>端口</label>
            <input type="text" id="ac_smtpport" placeholder="465"></div>
          <div class="field"><label>发信邮箱</label>
            <input type="text" id="ac_smtpuser" placeholder="you@qq.com"></div>
          <div class="field"><label>SMTP 授权码</label>
            <input type="password" id="ac_smtppass" placeholder="留空 = 不改"></div>
          <div class="field"><label>收件邮箱</label>
            <input type="text" id="ac_mailto" placeholder="you@connect.polyu.hk"></div>
        </div>
      </div>

      <div class="row-actions">
        <button class="primary" id="acctSave">保存并登录</button>
        <button id="acctTest">发一条测试通知</button>
      </div>
    </div>
  </section>

  <!-- ① 选目标 -->
  <section class="panel">
    <div class="toolbar">
      <div class="field"><label>Activity 活动</label>
        <select id="activity"><option value="">载入中…</option></select></div>
      <div class="field"><label>Center 中心</label>
        <select id="center"><option value="">载入中…</option></select></div>
      <div class="field" id="dsWrap" style="display:none"><label>Category 分类</label>
        <select id="dataset"></select></div>
      <div class="field"><label>Date 窗口起始</label>
        <select id="datefrom"></select></div>
      <div class="field act"><label>&nbsp;</label>
        <button class="primary" id="fetch">读取时间表</button></div>
    </div>
    <div class="stats" id="stats"></div>
    <div class="hint" id="selectnote"></div>
  </section>

  <!-- ② 时间表 -->
  <section class="panel">
    <div class="panel-head">
      <h2>可订时间表</h2>
      <span class="hint" id="gridCaption" style="margin:0"></span>
      <span class="sp"></span>
      <span class="hint" id="count" style="margin:0"></span>
    </div>
    <div class="gridwrap"><div id="grid"></div></div>
    <div class="legend">
      <span class="lg"><i style="background:var(--gray)"></i>不可用</span>
      <span class="lg"><i style="background:var(--free)"></i>$0 + 可退押金</span>
      <span class="lg"><i style="background:var(--paid)"></i>收费</span>
      <span class="lg"><i style="background:#a5cdf5"></i>价格未知</span>
      <span class="lg"><i style="background:var(--nodata);box-shadow:inset 0 0 0 1px #e0e5ee"></i>原始数据无此段</span>
      <span class="lg">灰色也能点 —— 现在不可订 ≠ 放位那刻不可订</span>
    </div>
    <details class="raw">
      <summary>看原始 timetable.json（字段对不上时照这个改解析器）</summary>
      <pre id="raw">（先点「读取时间表」）</pre>
    </details>
  </section>

  <!-- ③ 目标 -->
  <section class="panel">
    <div class="panel-head">
      <h2>抢位目标</h2>
      <span class="sp"></span>
      <button id="clear">清空</button>
      <button class="primary" id="save">保存目标</button>
    </div>
    <div class="chips" id="chips"></div>
  </section>

  <!-- ④ 设置 -->
  <section class="panel">
    <div class="panel-head">
      <h2>设置</h2>
      <span class="hint" id="cfgpath" style="margin:0"></span>
      <span class="sp"></span>
      <button id="resetCfg">恢复默认值</button>
      <button class="primary" id="saveCfg">保存设置</button>
    </div>

    <div class="sec">
      <h3>常用</h3>
      <div class="rows">
        <label class="switch"><input type="checkbox" id="bk_enabled"><span class="track"></span>自动抢位</label>
        <label class="switch"><input type="checkbox" id="fb_enabled"><span class="track"></span>蹲守提醒</label>
      </div>
      <div class="rows" style="margin-top:12px">
        <div class="field"><label>放位时刻</label><input type="time" id="bk_trigger_time"></div>
        <div class="field"><label>提前几天抢</label><input type="number" id="bk_lead_days" min="0" max="60"></div>
        <div class="field"><label>预热（分钟）</label><input type="number" id="bk_warmup" min="0" max="120"></div>
        <div class="field"><label>宽限（分钟）</label><input type="number" id="bk_grace" min="0" max="600"></div>
        <div class="field"><label>爆发时长（秒）</label><input type="number" id="bk_burst" min="0" max="3600"></div>
        <div class="field"><label>重试间隔（秒）</label><input type="number" id="bk_retry" min="0" max="600"></div>
      </div>
      <div class="hint" id="ruleHint"></div>
    </div>

    <details class="adv" id="adv">
      <summary>高级设置</summary>

      <div class="sec">
        <h3>触发日（只在没配抢位目标时生效）</h3>
        <div class="days" id="days"></div>
        <div class="hint">配了抢位目标后，脚本按每个目标各自的「放位日」干活，这里就不再起作用。</div>
      </div>

      <div class="sec">
        <h3>默认时段（派生模式 / 蹲守用）</h3>
        <div class="rows">
          <div class="field"><label>起</label><input type="time" id="bk_from"></div>
          <div class="field"><label>止</label><input type="time" id="bk_to"></div>
          <div class="field"><label>日期格式</label><input type="text" id="bk_datefmt" placeholder="%Y-%m-%d"></div>
        </div>
      </div>

      <div class="sec">
        <h3>场馆列表（名字=ctrId，每行一个）</h3>
        <textarea id="ta_gyms" rows="4" spellcheck="false"></textarea>
        <div class="hint" id="gymHint"></div>
      </div>

      <div class="sec">
        <h3>请求参数</h3>
        <div class="rows">
          <div class="field"><label>bookType</label><input type="text" id="rq_booktype"></div>
          <div class="field"><label>dataSetId</label><input type="text" id="rq_datasetid"></div>
          <div class="field"><label>actvId</label><input type="text" id="rq_actvid"></div>
          <div class="field"><label>facilityId</label><input type="text" id="rq_facilityid"></div>
          <div class="field"><label>searchDates（逗号分隔）</label><input type="text" id="rq_searchdates"></div>
          <div class="field"><label>&nbsp;</label>
            <label class="switch"><input type="checkbox" id="rq_showcourt"><span class="track"></span>显示场地详情</label></div>
        </div>
        <div class="hint">目标里带了 actvId / dataSetId 时，以目标为准。</div>
      </div>

      <div class="sec">
        <h3>结果判定</h3>
        <div class="rows wide">
          <div class="field"><label>成功关键词（逗号分隔）</label><input type="text" id="kw_success"></div>
          <div class="field"><label>失败关键词（逗号分隔）</label><input type="text" id="kw_failure"></div>
        </div>
        <div class="field" style="margin-top:12px"><label>结果页地址（留空用默认）</label>
          <input type="text" id="bk_result_url" placeholder="https://www40.polyu.edu.hk/.../make_book_result.do"></div>
        <div class="hint">判定顺序是<b>先失败后成功</b>：英文 <code>unsuccessful</code> 含 <code>success</code>，
          反过来判会把失败读成抢到。真跑一次后照 <code>dumps/</code> 里的结果页原文微调。</div>
      </div>

      <div class="sec">
        <h3>蹲守目标（JSON，留空 = 跟随抢位目标）</h3>
        <textarea id="ta_fbtargets" rows="3" spellcheck="false" placeholder="[]"></textarea>
      </div>

      <div class="sec">
        <h3>预订流程（JSON，导入 cURL 时自动生成，一般别手改）</h3>
        <textarea id="ta_steps" rows="4" spellcheck="false"></textarea>
      </div>

      <div class="sec">
        <h3>预订模板（抓包导入）</h3>
        <div id="tplStatus"></div>
        <div class="rows wide" style="margin-top:12px">
          <div class="field">
            <label>第 1 步 · 选时段进确认页</label>
            <textarea id="tpl1" rows="4" spellcheck="false" placeholder="在 POSS 里选中时段点提交，右键 → 复制 → 以 cURL 格式复制，粘到这里"></textarea>
            <button style="margin-top:8px" data-step="1" class="imp">导入第 1 步</button>
          </div>
          <div class="field">
            <label>第 2 步 · 勾选 declare 最终提交</label>
            <textarea id="tpl2" rows="4" spellcheck="false" placeholder="勾上 declare 再提交，同样复制 cURL 粘到这里"></textarea>
            <button style="margin-top:8px" data-step="2" class="imp">导入第 2 步</button>
          </div>
        </div>
        <div class="hint">
          模板写到 <code>booking_step1.curl</code> / <code>booking.curl</code>（已被 .gitignore 排除），
          <b>不会写进 config.json</b> —— 里面可能残留会话凭据痕迹，而 config.json 是要提交进仓库的。
          部署到 Actions 时，把这两个文件的全文分别贴进 Secrets 的
          <code>BOOKING_CURL_STEP1</code> / <code>BOOKING_CURL_STEP2</code>。
        </div>
      </div>
    </details>
  </section>
</div>

<script>
const $ = s => document.querySelector(s);
const DOWS = ["一","二","三","四","五","六","日"];   // 0=周一，与后端一致
let META = null, GRID = null, CFG = null, DEFAULTS = null, TPL = null;
let SEL = new Map(), DIRTY = false, CFG_DIRTY = false;

function say(t, cls){ const e=$("#status"); e.textContent=t||""; e.className="pill "+(cls||""); }
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function selText(id){ const e=$(id); return e && e.selectedOptions[0] ? e.selectedOptions[0].textContent.trim() : ""; }
function selVal(id){ const e=$(id); return e ? e.value : ""; }
function dow(d){ return DOWS[(new Date(d+"T00:00:00").getDay() + 6) % 7]; }
function markDirty(v){ DIRTY = v; $("#save").classList.toggle("dirty", v); }
function markCfgDirty(v){ CFG_DIRTY = v; $("#saveCfg").classList.toggle("dirty", v); }

/* 顶部横幅只有一个写入口：账号、模板、抓取错误都往这里报，
   否则几处各自 innerHTML 会互相覆盖（先写的那个就永远看不见了）。*/
let META_ERROR = "";
function renderBanner(){
  const warns = [];
  if(META_ERROR) warns.push(META_ERROR);
  if(ACCT && !ACCT.ready) warns.push(
    '<b>先填 POSS 账号</b>：在「登录与通知」里填 NetID 和 NetPassword，点「保存并登录」。'
    + '凭据写进 <code>.env</code>（已 gitignore），不会进仓库。');
  if(TPL && !TPL.ready) warns.push(
    '<b>预订模板还没导入</b>：脚本能读时间表、能蹲守提醒，但不会真的下单。'
    + '展开「设置 → 高级设置 → 预订模板」，把两条 cURL 粘进去导入即可。');
  $("#banner").innerHTML = warns.map(w => `<div class="warn">${w}</div>`).join("");
}

async function api(path, opts){
  const r = await fetch(path, opts);
  const j = await r.json().catch(()=>({ok:false,error:"返回不是 JSON"}));
  if(!r.ok || j.ok===false) throw new Error(j.error || ("HTTP "+r.status));
  return j;
}
function post(path, obj){
  return api(path, {method:"POST", headers:{"Content-Type":"application/json"},
                    body: JSON.stringify(obj)});
}

/* ==================== 目标选择 ==================== */

function fillSelect(el, items){
  el.innerHTML = "";
  let picked = false;
  for(const it of items){
    const o = document.createElement("option");
    o.value = it.value; o.textContent = it.label;
    if(it.selected){ o.selected = true; picked = true; }
    el.appendChild(o);
  }
  if(!picked){
    const first = [...el.options].find(o => o.value !== "");
    if(first) el.value = first.value;
  }
}
function roleOptions(role){
  const s = META.roles[role];
  return s ? s.options.filter(o => o.value !== "") : [];
}

async function loadMeta(){
  say("正在登录 POSS 并读取表单…", "busy");
  try{
    META = await api("/api/meta");
  }catch(e){
    say("读取失败", "err");
    META_ERROR = '拿不到 POSS 页面。先确认「登录与通知」里的账号密码填对了，'
               + '再点右上角「重新登录」。原始错误：<code>' + esc(e.message) + '</code>';
    renderBanner();
    return;
  }
  META_ERROR = "";
  renderBanner();
  fillSelect($("#activity"), roleOptions("activity"));
  fillSelect($("#center"), roleOptions("center"));
  const ds = META.roles.dataset;
  $("#dsWrap").style.display = ds ? "" : "none";
  if(ds) fillSelect($("#dataset"), ds.options);
  const dates = (META.date_options || META.window).map(d => ({value:d, label:d + " " + dow(d)}));
  fillSelect($("#datefrom"), dates);

  const missing = ["activity","center"].filter(r => !META.roles[r]);
  $("#selectnote").textContent =
    "识别到的下拉框：" + META.selects.map(s => `${s.name||"?"}(${s.role})`).join("、")
    + (missing.length ? "　⚠️ 没认出的角色：" + missing.join("、") + " —— 把上面括号里的名字告诉我，我改映射" : "");

  $("#cfgpath").textContent = "写入：" + META.config_file;
  SEL.clear();
  for(const t of (META.targets||[])) SEL.set(keyOf(t), t);
  markDirty(false);
  renderChips(); renderStats();
  say("就绪", "ok");
  await fetchGrid();
}

function keyOf(t){ return [t.ctrId, t.date, t.from].join("|"); }

async function fetchGrid(){
  const ctr = $("#center").value;
  if(!ctr){ say("先在 Center 里选一个中心", "err"); return; }
  // 参数名：页面 select 的真实 name + API 的规范名，**两个都发**。
  // 页面字段名和 AJAX 接口的参数名不一定一致 —— 只发一个就可能被服务端忽略，
  // 表现就是「换了选项格子不变」。多发的那个服务端不认识会自己忽略，无害。
  const q = new URLSearchParams({ctrId: ctr, start: $("#datefrom").value});
  const put = (role, canonical, val) => {
    if(!val) return;
    const r = META.roles[role];
    if(r && r.name) q.set(r.name, val);
    q.set(canonical, val);
  };
  put("activity", "actvId", $("#activity").value);
  put("dataset", "dataSetId", $("#dataset").value);

  // 先把"当前视图"和加载态显示出来 —— 请求还没回来也能看到选的是什么，
  // 否则用户没法判断到底是没刷新、还是数据本来就一样。
  $("#gridCaption").textContent =
    [selText("#activity"), selText("#center"), `${$("#datefrom").value} 起 8 天`]
      .filter(Boolean).join("　·　");
  $("#count").textContent = "读取中…";
  $(".gridwrap").classList.add("busy");
  say("读取时间表中…", "busy");
  try{
    const res = await api("/api/timetable?" + q.toString());
    GRID = res.grid;
    $("#raw").textContent = JSON.stringify(res.raw, null, 2);
    $("#count").textContent = res.grid ? `解析到 ${res.slot_count} 个时段` : "解析失败";
    renderGrid(); renderStats();
    if(res.warning) say("原始数据拿到了，但解析不出时段", "err");
    else say("时间表已更新", "ok");
  }catch(e){
    GRID = null; renderGrid();
    $("#count").textContent = "";
    say("读取时间表失败", "err");
    $("#grid").innerHTML = `<div class="placeholder"><div class="big">⚠️</div>${esc(e.message)}</div>`;
  }finally{
    $(".gridwrap").classList.remove("busy");
  }
}

function cellClass(c){
  if(!c) return "nodata";
  if((c.left||0) <= 0) return "unavailable";
  if(c.price === null || c.price === undefined) return "unknown";
  return c.price > 0 ? "paid" : "free";
}

function renderGrid(){
  const box = $("#grid");
  if(!GRID){
    box.innerHTML = '<div class="placeholder"><div class="big">🗓️</div>'
      + '选好 Activity / Center 后点「读取时间表」</div>';
    return;
  }
  let h = "<table class='grid'><thead><tr><th class='tcol'>时间</th>";
  for(const d of GRID.dates){
    const cls = (META && d === META.today) ? " class='today'" : "";
    h += `<th${cls}>${d.slice(5)}<span class="dow">周${dow(d)}</span></th>`;
  }
  h += "</tr></thead><tbody>";
  for(const tm of GRID.times){
    h += `<tr><th class="tcol">${tm}</th>`;
    for(const d of GRID.dates){
      const c = GRID.cells[d + "|" + tm];
      const cls = cellClass(c);
      const label = c && c.left != null ? c.left : "";
      const title = c
        ? `${d} ${tm}${c.end?("–"+c.end):""}\n剩 ${c.left ?? "?"} 个\n`
          + `价格 ${c.price ?? "未知"} / 押金 ${c.deposit ?? "未知"}` + (c.status?`\n${c.status}`:"")
        : `${d} ${tm}\n原始数据里没有这个时段\n（仍然可以选为目标）`;
      h += `<td><div class="cell ${cls}" data-d="${d}" data-t="${tm}" title="${esc(title)}">${label}</div></td>`;
    }
    h += "</tr>";
  }
  box.innerHTML = h + "</tbody></table>";
  box.querySelectorAll(".cell").forEach(el=>{
    el.addEventListener("click", ()=>toggle(el.dataset.d, el.dataset.t));
  });
  paintSelection();
}

function toggle(date, tm){
  const ctr = $("#center").value;
  const k = [ctr, date, tm].join("|");
  if(SEL.has(k)){ SEL.delete(k); }
  else{
    const c = (GRID && GRID.cells[date + "|" + tm]) || {};
    SEL.set(k, {
      ctrId: ctr,
      name: selText("#center"),
      date, from: tm, to: c.end || tm,
      activity: selText("#activity"),
      actvId: selVal("#activity"),
      dataSetId: selVal("#dataset")
    });
  }
  markDirty(true); paintSelection(); renderChips(); renderStats();
}

function paintSelection(){
  document.querySelectorAll(".cell").forEach(el=>{
    const k = [$("#center").value, el.dataset.d, el.dataset.t].join("|");
    el.classList.toggle("sel", SEL.has(k));
  });
}

function renderStats(){
  const box = $("#stats");
  const n = SEL.size;
  const lead = (CFG && CFG.booking.lead_days) ?? 7;
  const tt = (CFG && CFG.booking.trigger_time) || "08:30";
  const parts = [
    `<span class="stat">已选 <b>${n}</b> 个目标</span>`,
    `<span class="stat">放位规则 <b>目标日 −${lead} 天 ${esc(tt)}</b></span>`,
  ];
  const earliest = [...SEL.values()].map(t => releaseOf(t.date)).sort()[0];
  if(earliest) parts.push(`<span class="stat">最早放位日 <b>${esc(earliest)}</b></span>`);
  box.innerHTML = parts.join("");
}

function releaseOf(date){
  const lead = (CFG && CFG.booking.lead_days) ?? 7;
  const tt = (CFG && CFG.booking.trigger_time) || "08:30";
  const d = new Date(date + "T00:00:00");
  d.setDate(d.getDate() - lead);
  return d.toISOString().slice(0,10) + " " + tt;
}

function renderChips(){
  const box = $("#chips");
  const items = [...SEL.values()].sort((a,b)=>(a.date+a.from).localeCompare(b.date+b.from));
  if(!items.length){ box.innerHTML = '<div class="empty">还没选。在上面的网格里点格子。</div>'; return; }
  box.innerHTML = "";
  for(const t of items){
    const k = keyOf(t);
    const div = document.createElement("div");
    div.className = "chip";
    div.innerHTML = `<div class="body">
        <div class="t">${esc(t.date)}　${esc(t.from)}–${esc(t.to)}</div>
        <div class="m">${esc(t.name || t.ctrId)}${t.activity ? " · " + esc(t.activity) : ""}
          · 放位日 ${esc(releaseOf(t.date))}</div>
      </div>`;
    const b = document.createElement("button");
    b.textContent = "移除";
    b.onclick = ()=>{ SEL.delete(k); markDirty(true); renderChips(); paintSelection(); renderStats(); };
    div.appendChild(b);
    box.appendChild(div);
  }
}

/* ==================== 设置 ==================== */

async function loadConfig(){
  const r = await api("/api/config");
  CFG = r.config; DEFAULTS = r.defaults; TPL = r.template;
  fillSettings(); renderTplStatus(); markCfgDirty(false);
}

function fillSettings(){
  const b = CFG.booking, f = CFG.fallback || {}, q = CFG.request || {};
  $("#bk_enabled").checked = !!b.enabled;
  $("#fb_enabled").checked = f.enabled !== false;
  $("#bk_trigger_time").value = b.trigger_time || "08:30";
  $("#bk_lead_days").value = b.lead_days ?? 7;
  $("#bk_warmup").value = b.warmup_minutes ?? 6;
  $("#bk_grace").value = b.grace_minutes ?? 30;
  $("#bk_burst").value = b.burst_seconds ?? 120;
  $("#bk_retry").value = b.retry_interval ?? 3;

  $("#days").innerHTML = DOWS.map((d, i) =>
    `<label class="day"><input type="checkbox" value="${i}"${(b.trigger_days||[]).includes(i) ? " checked" : ""}><span>${d}</span></label>`
  ).join("");

  $("#bk_from").value = b.from || "16:30";
  $("#bk_to").value = b.to || "17:30";
  $("#bk_datefmt").value = b.search_date_format || "%Y-%m-%d";
  $("#ta_gyms").value = (CFG.gyms || []).map(g => `${g.name}=${g.ctrId}`).join("\n");
  $("#gymHint").innerHTML = b.gyms && b.gyms.length
    ? `⚠️ config.json 里的 <code>booking.gyms</code> 非空（${esc(b.gyms.join("、"))}），`
      + `它会盖掉这里的顺序。把它清空才会用上面的列表。`
    : "派生模式下按这个顺序依次尝试；蹲守也按它决定盯哪些馆。";

  $("#rq_booktype").value = q.bookType ?? "";
  $("#rq_datasetid").value = q.dataSetId ?? "";
  $("#rq_actvid").value = q.actvId ?? "";
  $("#rq_facilityid").value = q.facilityId ?? "";
  $("#rq_searchdates").value = (q.searchDates || []).join(", ");
  $("#rq_showcourt").checked = String(q.showCourtAreaDetails) === "true";

  $("#kw_success").value = (b.success_keywords || []).join(", ");
  $("#kw_failure").value = (b.failure_keywords || []).join(", ");
  $("#bk_result_url").value = b.result_url || "";
  $("#ta_fbtargets").value = JSON.stringify(f.targets || [], null, 2);
  $("#ta_steps").value = JSON.stringify(b.steps || [], null, 2);
  renderRuleHint();
}

function renderRuleHint(){
  const n = (CFG && CFG.booking.targets || []).length;
  $("#ruleHint").textContent = n
    ? `已配 ${n} 个抢位目标：脚本只在每个目标的「放位日」（目标日 − 提前天数）那一刻开抢，跟上面的触发日无关。`
    : "还没配抢位目标：脚本会按「今天 + 提前天数」的派生逻辑，在触发日那天开抢默认时段。";
}

function renderTplStatus(){
  const box = $("#tplStatus");
  if(!TPL) { box.innerHTML = ""; return; }
  box.innerHTML = TPL.steps.map(s =>
    `<div class="tpl"><span class="dot ${s.imported ? "ok" : "no"}"></span>
      <b>第 ${s.step} 步</b>
      <code>${esc(s.file)}</code>
      <span class="sp"></span>
      <span>${s.imported ? esc(s.bytes + " 字节") : "尚未导入"}</span>
    </div>`
  ).join("") + (TPL.ready
    ? '<div class="hint" style="color:var(--ok)">✓ 预订模板齐了，可以真提交。</div>'
    : '<div class="hint" style="color:var(--err)">✗ 模板不全 —— 现在只会蹲守提醒，不会真抢。</div>');
  renderBanner();
}

function num(id, name, min=0, max=1e9){
  const v = parseInt($(id).value, 10);
  if(!Number.isFinite(v) || v < min || v > max)
    throw new Error(`${name} 要是 ${min}–${max} 之间的整数`);
  return v;
}
function hhmm(id, name){
  const v = ($(id).value || "").trim();
  if(!/^\d{1,2}:\d{2}$/.test(v)) throw new Error(`${name} 要写成 HH:MM`);
  return v;
}
function listOf(id){
  return $(id).value.split(/[,，\n]/).map(s => s.trim()).filter(Boolean);
}
function jsonOf(id, name){
  const t = $(id).value.trim();
  if(!t) return [];
  try{ return JSON.parse(t); }
  catch(e){ throw new Error(`${name} 不是合法 JSON：${e.message}`); }
}
function gymsOf(id){
  const out = [];
  for(const raw of $(id).value.split("\n")){
    const s = raw.trim();
    if(!s || s.startsWith("#")) continue;
    const i = s.search(/[=,，:：\t]/);
    if(i < 0) throw new Error(`场馆列表每行要写成「名字=ctrId」，这行看不懂：${s}`);
    const name = s.slice(0, i).trim(), ctrId = s.slice(i + 1).trim();
    if(!name || !ctrId) throw new Error(`场馆这行缺名字或 ctrId：${s}`);
    out.push({name, ctrId});
  }
  if(!out.length) throw new Error("场馆列表不能为空");
  return out;
}

function collectSettings(){
  return {
    gyms: gymsOf("#ta_gyms"),
    booking: {
      enabled: $("#bk_enabled").checked,
      trigger_time: hhmm("#bk_trigger_time", "放位时刻"),
      lead_days: num("#bk_lead_days", "提前几天抢", 0, 60),
      warmup_minutes: num("#bk_warmup", "预热", 0, 120),
      grace_minutes: num("#bk_grace", "宽限", 0, 600),
      burst_seconds: num("#bk_burst", "爆发时长", 0, 3600),
      retry_interval: num("#bk_retry", "重试间隔", 0, 600),
      trigger_days: [...document.querySelectorAll("#days input:checked")].map(e => +e.value),
      from: hhmm("#bk_from", "默认时段起"),
      to: hhmm("#bk_to", "默认时段止"),
      search_date_format: $("#bk_datefmt").value.trim() || "%Y-%m-%d",
      success_keywords: listOf("#kw_success"),
      failure_keywords: listOf("#kw_failure"),
      result_url: $("#bk_result_url").value.trim(),
      steps: jsonOf("#ta_steps", "预订流程"),
      targets: [...SEL.values()].sort((a,b)=>(a.date+a.from).localeCompare(b.date+b.from)),
    },
    fallback: {
      enabled: $("#fb_enabled").checked,
      targets: jsonOf("#ta_fbtargets", "蹲守目标"),
    },
    request: {
      bookType: $("#rq_booktype").value.trim(),
      dataSetId: $("#rq_datasetid").value.trim(),
      actvId: $("#rq_actvid").value.trim(),
      facilityId: $("#rq_facilityid").value.trim(),
      searchDates: listOf("#rq_searchdates"),
      showCourtAreaDetails: $("#rq_showcourt").checked ? "true" : "false",
    },
  };
}

/* ==================== 事件 ==================== */

$("#fetch").onclick = fetchGrid;
$("#reload").onclick = loadMeta;
$("#center").onchange = ()=>{ if(META) fetchGrid(); };
$("#activity").onchange = ()=>{ if(META) fetchGrid(); };
$("#dataset").onchange = ()=>{ if(META) fetchGrid(); };
$("#datefrom").onchange = ()=>{ if(META) fetchGrid(); };
$("#clear").onclick = ()=>{
  if(!SEL.size) return;
  SEL.clear(); markDirty(true); renderChips(); paintSelection(); renderStats();
};
$("#relogin").onclick = async ()=>{
  say("重新登录中…", "busy");
  try{ await post("/api/login", {}); await loadMeta(); }
  catch(e){ say("登录失败", "err"); }
};

$("#save").onclick = async ()=>{
  const targets = [...SEL.values()].sort((a,b)=>(a.date+a.from).localeCompare(b.date+b.from));
  if(!targets.length && !confirm("目标为空，确定要把 targets 清空吗？")) return;
  say("保存目标中…", "busy");
  try{
    await post("/api/targets", {targets});
    markDirty(false); renderRuleHint();
    say(`已保存 ${targets.length} 个目标`, "ok");
  }catch(e){ say("保存失败：" + e.message, "err"); }
};

$("#saveCfg").onclick = async ()=>{
  let patch;
  try{ patch = collectSettings(); }
  catch(e){ say("设置有问题：" + e.message, "err"); return; }
  say("保存设置中…", "busy");
  try{
    const r = await post("/api/config", patch);
    CFG = r.config;
    markCfgDirty(false); markDirty(false);
    fillSettings(); renderStats(); renderChips();
    say("设置已保存", "ok");
  }catch(e){ say("保存失败：" + e.message, "err"); }
};

$("#resetCfg").onclick = ()=>{
  if(!DEFAULTS) return;
  CFG = JSON.parse(JSON.stringify(CFG));
  const keep = CFG.booking.targets;
  CFG.booking = Object.assign({}, CFG.booking, DEFAULTS);
  CFG.booking.targets = keep;          // 目标不属于"默认值"，别给清了
  fillSettings(); markCfgDirty(true);
  say("已填回默认值，点「保存设置」才会写盘", "ok");
};

document.querySelectorAll(".imp").forEach(btn => {
  btn.onclick = async ()=>{
    const step = +btn.dataset.step;
    const text = $("#tpl" + step).value;
    if(!text.trim()){ say(`第 ${step} 步的 cURL 还没粘进来`, "err"); return; }
    say(`导入第 ${step} 步模板中…`, "busy");
    try{
      const r = await post("/api/template", {step, curl: text});
      TPL = r.status;
      $("#tpl" + step).value = "";
      $("#banner").dataset.tplWarn = "";
      renderTplStatus();
      say(`第 ${step} 步已导入（${r.info.placeholders.length} 个占位符）`, "ok");
    }catch(e){ say("导入失败：" + e.message, "err"); }
  };
});

// 设置区任何改动都标脏
document.querySelector("#adv").addEventListener("input", ()=>markCfgDirty(true));
document.querySelector("#adv").addEventListener("change", ()=>markCfgDirty(true));
["bk_enabled","fb_enabled","bk_trigger_time","bk_lead_days","bk_warmup","bk_grace",
 "bk_burst","bk_retry"].forEach(id => {
  const el = $("#" + id);
  el.addEventListener("input", ()=>{ markCfgDirty(true); });
  el.addEventListener("change", ()=>{ markCfgDirty(true); });
});

/* ==================== 登录与通知 ==================== */

let ACCT = null;

async function loadAccount(){
  ACCT = await api("/api/account");
  fillAccount();
}

function fillAccount(){
  $("#acPath").textContent = ACCT.env_file;
  $("#ac_netid").value = ACCT.netid || "";
  $("#ac_fbuserid").value = ACCT.fbUserId || "";
  $("#ac_smtphost").value = ACCT.smtpHost || "";
  $("#ac_smtpport").value = ACCT.smtpPort || "";
  $("#ac_smtpuser").value = ACCT.smtpUser || "";
  $("#ac_mailto").value = ACCT.mailTo || "";
  // 密码类字段永远不回显，用 placeholder 说明「已经配过了」
  $("#ac_password").value = "";
  $("#ac_pushplus").value = "";
  $("#ac_serverchan").value = "";
  $("#ac_smtppass").value = "";
  $("#ac_password").placeholder = ACCT.password_set ? "已设置 · 留空 = 不改" : "还没设密码";
  $("#ac_pushplus").placeholder = ACCT.pushplus_set ? "已设置 · 留空 = 不改" : "留空 = 不改";
  $("#ac_serverchan").placeholder = ACCT.serverchan_set ? "已设置 · 留空 = 不改" : "留空 = 不改";
  $("#ac_smtppass").placeholder = ACCT.smtpPass_set ? "已设置 · 留空 = 不改" : "留空 = 不改";

  const chans = [
    ACCT.pushplus_set && "PushPlus",
    ACCT.serverchan_set && "Server酱",
    ACCT.smtpPass_set && "邮件",
  ].filter(Boolean);
  $("#acctStatus").innerHTML = ACCT.ready
    ? `<span style="color:var(--ok)">✓ 已配好 ${esc(ACCT.netid)}</span>`
      + (chans.length ? ` · 通知渠道：${esc(chans.join(" / "))}` : " · ⚠️ 还没配通知渠道")
    : '<span style="color:var(--err)">✗ 还没配账号，下面的东西都用不了</span>';

  // 没配好就自动展开，配好了就收起
  $("#acctBody").style.display = ACCT.ready ? "none" : "";
  $("#acctToggle").textContent = ACCT.ready ? "修改" : "收起";
  renderBanner();
}

$("#acctToggle").onclick = ()=>{
  const b = $("#acctBody");
  const open = b.style.display === "none";
  b.style.display = open ? "" : "none";
  $("#acctToggle").textContent = open ? "收起" : "修改";
};

$("#acctSave").onclick = async ()=>{
  const payload = {
    netid: $("#ac_netid").value.trim(),
    password: $("#ac_password").value,          // 空 = 不改
    fbUserId: $("#ac_fbuserid").value.trim(),
    pushplus: $("#ac_pushplus").value,
    serverchan: $("#ac_serverchan").value,
    smtpHost: $("#ac_smtphost").value.trim(),
    smtpPort: $("#ac_smtpport").value.trim(),
    smtpUser: $("#ac_smtpuser").value.trim(),
    smtpPass: $("#ac_smtppass").value,          // 空 = 不改
    mailTo: $("#ac_mailto").value.trim(),
  };
  if(!payload.netid){ say("NetID 不能为空", "err"); return; }
  say("保存中…", "busy");
  try{
    const r = await post("/api/account", payload);
    ACCT = r.account;
    fillAccount();
    say("已保存，正在登录 POSS…", "busy");
    await post("/api/login", {});
    say("登录成功", "ok");
    await loadMeta();
  }catch(e){
    say("保存/登录失败：" + e.message, "err");
    $("#acctBody").style.display = "";
    $("#acctToggle").textContent = "收起";
  }
};

$("#acctTest").onclick = async ()=>{
  say("发送测试通知中…", "busy");
  try{
    const r = await post("/api/notify-test", {});
    say(`测试通知已发出（${r.channels} 个渠道）`, "ok");
  }catch(e){ say("测试失败：" + e.message, "err"); }
};

/* ==================== 启动 ==================== */

(async ()=>{
  // 配置必须先加载，且**不依赖登录**：config.json 与 POSS 会话无关。
  // 顺序错了的话，没登录时设置面板是一片空白，一点「保存设置」就把配置清光了。
  try{ await loadConfig(); }
  catch(e){ say("读配置失败：" + e.message, "err"); return; }

  try{ await loadAccount(); }
  catch(e){ say("读账号配置失败：" + e.message, "err"); }

  renderGrid();
  if(ACCT && ACCT.ready){
    await loadMeta();
  }else{
    say("先填 POSS 账号", "err");
    renderBanner();
  }
})();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="POSS 抢位目标选择器（本地 UI）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    M.load_env_file()
    env = read_env()
    if not (env.get("POSS_NETID") and env.get("POSS_PASSWORD")):
        print(f"[ui] 还没配 POSS 账号。打开页面后在最上面的「登录与通知」里填，"
              f"会写进 {os.path.abspath(ENV_FILE)}（已 gitignore，不进仓库）。")

    url = f"http://127.0.0.1:{args.port}/"
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[ui] 打开 {url}  （Ctrl-C 退出）")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[ui] 已退出")


if __name__ == "__main__":
    main()
