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
    LoginFailed,
    PossClient,
    SessionExpired,
    parse_selects,
)
import monitor as M

CONFIG_FILE = os.environ.get("CONFIG_FILE", "config.json")
DEFAULT_PORT = 8765
WINDOW_DAYS = 8          # POSS 只放「今天起 8 天」的场


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


def save_targets(targets: list) -> dict:
    """把 targets 写回 config.json（其余字段原样保留），并做一次校验。"""
    p = config_path()
    cfg = load_config_raw()
    booking = dict(M.DEFAULT_BOOKING)
    booking.update(cfg.get("booking") or {})
    booking["targets"] = targets
    cfg["booking"] = booking
    cfg.setdefault("gyms", M.DEFAULT_GYMS)
    cfg.setdefault("fallback", {"enabled": True})
    cfg.setdefault("request", {})
    M._validate_config(cfg)          # 配置非法就别落盘，免得 Actions 里炸
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


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
        cfg = load_config_raw()
        booking = dict(M.DEFAULT_BOOKING)
        booking.update(cfg.get("booking") or {})
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
            "config_file": os.path.abspath(config_path()),
        }

    def api_timetable(self, q):
        ctr = (q.get("ctrId") or [""])[0].strip()
        if not ctr:
            raise ValueError("缺少 ctrId（先在「中心」里选一个）")
        params = {}
        for key in ("actvId", "dataSetId", "bookType"):
            v = (q.get(key) or [""])[0].strip()
            if v:
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
<title>POSS 抢位目标选择器</title>
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
  h1,h2{margin:0;font-weight:650}
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
    white-space:nowrap;   /* 别让「重新登录」在窄屏被折成两行 */
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
  /* 有未保存改动时，保存按钮上亮个小点 */
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
  }
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

  /* ---------- 原始数据 ---------- */
  details{margin-top:14px}
  summary{
    cursor:pointer; color:var(--dim); font-size:12.5px; list-style:none;
    display:inline-flex; align-items:center; gap:6px; user-select:none;
  }
  summary::-webkit-details-marker{display:none}
  summary::before{content:"▸"; font-size:10px; transition:transform .15s}
  details[open] summary::before{transform:rotate(90deg)}
  pre{
    background:#0f1117; color:#d7dae0; padding:14px; border-radius:var(--r-sm);
    overflow:auto; max-height:340px; font-size:11.5px; line-height:1.5; margin:10px 0 0;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }

  /* ---------- 窄屏（手机 / 半屏窗口）---------- */
  @media (max-width:760px){
    header{height:auto; min-height:56px; padding:10px 16px; flex-wrap:wrap; gap:8px 10px}
    .brand .sub{display:none}
    .pill{order:9; flex-basis:100%; max-width:none; text-align:center}
    .wrap{padding:14px 14px 56px}
    .panel{padding:14px 13px}
    .chip button{opacity:1}          /* 触屏没有 hover，移除按钮得一直露着 */
  }
</style>
</head>
<body>

<header>
  <div class="logo">P</div>
  <div class="brand">
    <h1>POSS 抢位目标选择器</h1>
    <div class="sub">点选要抢的时段 → 写进 config.json</div>
  </div>
  <span id="status" class="pill"></span>
  <span class="sp"></span>
  <button id="reload">重新读取</button>
  <button id="relogin">重新登录</button>
</header>

<div class="wrap">
  <div id="banner"></div>

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

  <section class="panel">
    <div class="panel-head">
      <h2>可订时间表</h2>
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
    <details>
      <summary>看原始 timetable.json（字段对不上时照这个改解析器）</summary>
      <pre id="raw">（先点「读取时间表」）</pre>
    </details>
  </section>

  <section class="panel">
    <div class="panel-head">
      <h2>抢位目标</h2>
      <span class="hint" id="cfgpath" style="margin:0"></span>
      <span class="sp"></span>
      <button id="clear">清空</button>
      <button class="primary" id="save">保存到 config.json</button>
    </div>
    <div class="chips" id="chips"></div>
  </section>
</div>

<script>
const $ = s => document.querySelector(s);
let META = null, GRID = null, SEL = new Map(), DIRTY = false;

function say(t, cls){ const e=$("#status"); e.textContent=t||""; e.className="pill "+(cls||""); }
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function selText(id){ const e=$(id); return e && e.selectedOptions[0] ? e.selectedOptions[0].textContent.trim() : ""; }
function selVal(id){ const e=$(id); return e ? e.value : ""; }
function dow(d){ return "日一二三四五六"[new Date(d+"T00:00:00").getDay()]; }
function markDirty(v){ DIRTY = v; $("#save").classList.toggle("dirty", v); }

async function api(path, opts){
  const r = await fetch(path, opts);
  const j = await r.json().catch(()=>({ok:false,error:"返回不是 JSON"}));
  if(!r.ok || j.ok===false) throw new Error(j.error || ("HTTP "+r.status));
  return j;
}

function fillSelect(el, items){
  el.innerHTML = "";
  let picked = false;
  for(const it of items){
    const o = document.createElement("option");
    o.value = it.value; o.textContent = it.label;
    if(it.selected){ o.selected = true; picked = true; }
    el.appendChild(o);
  }
  // 抓下来的 selected 状态要照搬；没有的话别停在空占位项上
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
    $("#banner").innerHTML = '<div class="warn">拿不到 POSS 页面。先确认 <b>.env</b> 里的 '
      + 'POSS_NETID / POSS_PASSWORD 填对了，再点右上角「重新登录」。</div>';
    return;
  }
  $("#banner").innerHTML = "";
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
  renderChips();
  renderStats();
  say("就绪", "ok");
  await fetchGrid();
}

function keyOf(t){ return [t.ctrId, t.date, t.from].join("|"); }

async function fetchGrid(){
  const ctr = $("#center").value;
  if(!ctr){ say("先在 Center 里选一个中心", "err"); return; }
  const q = new URLSearchParams({ctrId: ctr, start: $("#datefrom").value});
  if(META.roles.activity && $("#activity").value) q.set("actvId", $("#activity").value);
  if(META.roles.dataset && $("#dataset").value) q.set("dataSetId", $("#dataset").value);
  say("读取时间表中…", "busy");
  try{
    const res = await api("/api/timetable?" + q.toString());
    GRID = res.grid;
    $("#raw").textContent = JSON.stringify(res.raw, null, 2);
    $("#count").textContent = res.grid ? `解析到 ${res.slot_count} 个时段` : "解析失败";
    renderGrid();
    renderStats();
    if(res.warning) say("原始数据拿到了，但解析不出时段", "err");
    else say("时间表已更新", "ok");
  }catch(e){
    GRID = null; renderGrid();
    $("#count").textContent = "";
    say("读取时间表失败", "err");
    $("#grid").innerHTML = `<div class="placeholder"><div class="big">⚠️</div>${esc(e.message)}</div>`;
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
  markDirty(true);
  paintSelection();
  renderChips();
  renderStats();
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
  const lead = (META && META.booking.lead_days) || 7;
  const tt = (META && META.booking.trigger_time) || "08:30";
  const earliest = [...SEL.values()].map(t => releaseOf(t.date)).sort()[0];
  const parts = [
    `<span class="stat">已选 <b>${n}</b> 个目标</span>`,
    `<span class="stat">放位规则 <b>目标日 −${lead} 天 ${esc(tt)}</b></span>`,
  ];
  if(earliest) parts.push(`<span class="stat">最早放位日 <b>${esc(earliest)}</b></span>`);
  box.innerHTML = n ? parts.join("") : parts.slice(0,2).join("");
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
    b.onclick = ()=>{
      SEL.delete(k); markDirty(true);
      renderChips(); paintSelection(); renderStats();
    };
    div.appendChild(b);
    box.appendChild(div);
  }
}

function releaseOf(date){
  const lead = (META && META.booking.lead_days) || 7;
  const d = new Date(date + "T00:00:00");
  d.setDate(d.getDate() - lead);
  return d.toISOString().slice(0,10) + " " + ((META && META.booking.trigger_time) || "08:30");
}

$("#fetch").onclick = fetchGrid;
$("#reload").onclick = loadMeta;
$("#center").onchange = ()=>{ if(META) fetchGrid(); };
$("#activity").onchange = ()=>{ if(META) fetchGrid(); };
$("#dataset").onchange = ()=>{ if(META) fetchGrid(); };
$("#datefrom").onchange = ()=>{ if(META) fetchGrid(); };
$("#clear").onclick = ()=>{
  if(!SEL.size) return;
  SEL.clear(); markDirty(true);
  renderChips(); paintSelection(); renderStats();
};
$("#relogin").onclick = async ()=>{
  say("重新登录中…", "busy");
  try{ await api("/api/login", {method:"POST"}); await loadMeta(); }
  catch(e){ say("登录失败", "err"); }
};
$("#save").onclick = async ()=>{
  const targets = [...SEL.values()].sort((a,b)=>(a.date+a.from).localeCompare(b.date+b.from));
  if(!targets.length && !confirm("目标为空，确定要把 config.json 的 targets 清空吗？")) return;
  say("保存中…", "busy");
  try{
    const r = await api("/api/targets", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({targets})
    });
    markDirty(false);
    say(`已保存 ${r.count} 个目标`, "ok");
  }catch(e){ say("保存失败：" + e.message, "err"); }
};

loadMeta();
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
    if not (os.environ.get("POSS_NETID") and os.environ.get("POSS_PASSWORD")):
        print("[ui] ⚠️ 没读到 POSS_NETID / POSS_PASSWORD。"
              "先 `cp .env.example .env` 并填好，再重跑。")

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
