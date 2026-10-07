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
<title>POSS 抢位目标选择器</title>
<style>
  :root{
    --bg:#f6f7f9; --panel:#fff; --line:#e3e6ec; --ink:#1c1f26; --dim:#6b7280;
    --gray:#b9bec7; --free:#ff2fbe; --paid:#4a6cf7; --unknown:#9fd0ff;
    --accent:#1f6feb; --warn:#b45309; --ok:#0f9d58;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.5 -apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif}
  header{padding:16px 20px;background:var(--panel);border-bottom:1px solid var(--line);
         display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:10}
  header h1{margin:0;font-size:16px;font-weight:650}
  header .sp{flex:1}
  button{font:inherit;padding:6px 12px;border:1px solid var(--line);border-radius:7px;
         background:#fff;color:var(--ink);cursor:pointer}
  button:hover{border-color:#c3c9d4}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
  button.primary:hover{background:#1a5fd0}
  button:disabled{opacity:.5;cursor:not-allowed}
  .wrap{padding:16px 20px 80px;max-width:1500px;margin:0 auto}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
         padding:14px 16px;margin-bottom:14px}
  .row{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-end}
  label.f{display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--dim)}
  select,input{font:inherit;padding:6px 9px;border:1px solid var(--line);border-radius:7px;
               background:#fff;color:var(--ink);min-width:180px}
  .msg{font-size:12px;color:var(--dim);margin-left:auto}
  .msg.err{color:#b3261e;white-space:pre-wrap;max-width:640px}
  .msg.ok{color:var(--ok)}
  /* 网格 */
  .gridwrap{overflow:auto;border:1px solid var(--line);border-radius:8px;background:#fff}
  table.grid{border-collapse:collapse;font-size:12px;width:100%}
  table.grid th{position:sticky;top:0;background:#fff;border-bottom:1px solid var(--line);
                padding:6px 4px;font-weight:600;white-space:nowrap;z-index:2}
  table.grid td{border:1px solid #eceff4;padding:0;text-align:center}
  table.grid th.tcol{position:sticky;left:0;background:#fff;border-right:1px solid var(--line);
                     z-index:3;min-width:78px;text-align:right;padding-right:8px}
  .cell{height:34px;min-width:86px;display:flex;align-items:center;justify-content:center;
        cursor:pointer;user-select:none;font-size:11px;color:#fff;position:relative}
  /* 灰色格子也可以点：现在不可订 ≠ 放位那一刻不可订，抢位目标本来就是给未来定的 */
  .cell.unavailable{background:var(--gray);color:#565c66}
  .cell.free{background:var(--free)}
  .cell.paid{background:var(--paid)}
  .cell.unknown{background:var(--unknown);color:#1c3f66;outline:1px dashed #5b9bd5;
                outline-offset:-3px}
  .cell.nodata{background:#f2f4f7;color:#c2c8d0}
  .cell.sel{box-shadow:inset 0 0 0 3px #111;font-weight:700}
  .cell.sel::after{content:"✓";position:absolute;top:1px;right:3px;font-size:10px}
  .legend{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px;font-size:12px;color:var(--dim)}
  .legend i{display:inline-block;width:13px;height:13px;border-radius:3px;margin-right:6px;
            vertical-align:-2px}
  /* 已选 */
  .chips{display:flex;flex-direction:column;gap:8px;margin-top:6px}
  .chip{display:flex;align-items:center;gap:10px;padding:8px 10px;border:1px solid var(--line);
        border-radius:8px;background:#fbfcfe;font-size:13px}
  .chip .who{font-weight:600}
  .chip .meta{color:var(--dim);font-size:12px}
  .chip button{padding:3px 8px;font-size:12px}
  .empty{color:var(--dim);font-size:13px;padding:6px 0}
  details{margin-top:10px}
  summary{cursor:pointer;color:var(--dim);font-size:12px}
  pre{background:#0f1117;color:#d7dae0;padding:12px;border-radius:8px;overflow:auto;
      max-height:340px;font-size:11.5px;line-height:1.45}
  .hint{font-size:12px;color:var(--dim);margin-top:6px}
  .warn{background:#fff8e6;border:1px solid #f0d9a0;color:#7a5300;padding:9px 12px;
        border-radius:8px;font-size:12.5px;margin-bottom:12px}
</style>
</head>
<body>
<header>
  <h1>POSS 抢位目标选择器</h1>
  <span id="status" class="msg"></span>
  <span class="sp"></span>
  <button id="reload">重新读取 POSS</button>
  <button id="relogin">重新登录</button>
</header>

<div class="wrap">
  <div id="banner"></div>

  <div class="panel">
    <div class="row">
      <label class="f">Activity（活动）
        <select id="activity"><option value="">— 载入中 —</option></select>
      </label>
      <label class="f">Center（中心）
        <select id="center"><option value="">— 载入中 —</option></select>
      </label>
      <label class="f" id="dsWrap" style="display:none">Category（分类）
        <select id="dataset"></select>
      </label>
      <label class="f">Date（日期窗口起始）
        <select id="datefrom"></select>
      </label>
      <button id="fetch">读取时间表</button>
      <span id="count" class="msg"></span>
    </div>
    <div class="hint" id="selectnote"></div>
  </div>

  <div class="panel">
    <div class="gridwrap"><table class="grid" id="grid"></table></div>
    <div class="legend">
      <span><i style="background:var(--gray)"></i>不可用</span>
      <span><i style="background:var(--free)"></i>$0 + 可退押金</span>
      <span><i style="background:var(--paid)"></i>收费</span>
      <span><i style="background:var(--unknown);outline:1px dashed #5b9bd5"></i>价格未知（原始数据没给金额字段）</span>
      <span><i style="background:#f2f4f7;border:1px solid #dfe3e9"></i>原始数据里没有这个时段</span>
      <span>点击格子加入/移出抢位目标（灰色也能选 —— 现在不可订 ≠ 放位那刻不可订）</span>
    </div>
    <details>
      <summary>看原始 timetable.json（字段对不上时照这个改解析器）</summary>
      <pre id="raw">（先点「读取时间表」）</pre>
    </details>
  </div>

  <div class="panel">
    <div class="row">
      <strong>抢位目标</strong>
      <span class="hint" id="cfgpath"></span>
      <span class="sp"></span>
      <button id="clear">清空</button>
      <button class="primary" id="save">保存到 config.json</button>
    </div>
    <div class="chips" id="chips"></div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
let META = null, GRID = null, SEL = new Map();

function say(t, cls){ const e=$("#status"); e.textContent=t||""; e.className="msg "+(cls||""); }
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function selText(id){ const e=$(id); return e && e.selectedOptions[0] ? e.selectedOptions[0].textContent.trim() : ""; }
function selVal(id){ const e=$(id); return e ? e.value : ""; }

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
  say("正在登录 POSS 并读取表单…");
  try{
    META = await api("/api/meta");
  }catch(e){
    say("读取失败：" + e.message, "err");
    $("#banner").innerHTML = '<div class="warn">拿不到 POSS 页面。先确认 <b>.env</b> 里的 '
      + 'POSS_NETID / POSS_PASSWORD 填对了，再点右上角「重新登录」。</div>';
    return;
  }
  fillSelect($("#activity"), roleOptions("activity"));
  fillSelect($("#center"), roleOptions("center"));
  const ds = META.roles.dataset;
  $("#dsWrap").style.display = ds ? "" : "none";
  if(ds) fillSelect($("#dataset"), ds.options);
  const dates = (META.date_options || META.window).map(d => ({value:d, label:d + " " + "日一二三四五六"[new Date(d+"T00:00:00").getDay()]}));
  fillSelect($("#datefrom"), dates);

  const found = Object.keys(META.roles);
  const missing = ["activity","center"].filter(r => !META.roles[r]);
  $("#selectnote").textContent =
    "页面上识别到的下拉框：" + META.selects.map(s => `${s.name||"?"}(${s.role})`).join("、")
    + (missing.length ? "　⚠️ 没认出来的角色：" + missing.join("、") + " —— 照上面括号里的名字告诉我，我改映射" : "");

  $("#cfgpath").textContent = "写入：" + META.config_file;
  SEL.clear();
  for(const t of (META.targets||[])) SEL.set(keyOf(t), t);
  renderChips();
  say("就绪，选好 Activity / Center 后点「读取时间表」", "ok");
  await fetchGrid();
}

function keyOf(t){ return [t.ctrId, t.date, t.from].join("|"); }

async function fetchGrid(){
  const ctr = $("#center").value;
  if(!ctr){ say("先在 Center 里选一个中心", "err"); return; }
  const q = new URLSearchParams({ctrId: ctr, start: $("#datefrom").value});
  if(META.roles.activity && $("#activity").value) q.set("actvId", $("#activity").value);
  if(META.roles.dataset && $("#dataset").value) q.set("dataSetId", $("#dataset").value);
  say("读取时间表中…");
  try{
    const res = await api("/api/timetable?" + q.toString());
    GRID = res.grid;
    $("#raw").textContent = JSON.stringify(res.raw, null, 2);
    $("#count").textContent = res.grid ? `解析到 ${res.slot_count} 个时段` : "解析失败";
    renderGrid();
    if(res.warning) say("原始数据拿到了，但解析不出时段：" + res.warning, "err");
    else say("时间表已更新", "ok");
  }catch(e){
    GRID = null; renderGrid();
    $("#count").textContent = "";
    say("读取时间表失败：" + e.message, "err");
  }
}

function cellClass(c){
  if(!c) return "nodata";
  if((c.left||0) <= 0) return "unavailable";
  if(c.price === null || c.price === undefined) return "unknown";
  return c.price > 0 ? "paid" : "free";
}

function renderGrid(){
  const t = $("#grid");
  if(!GRID){ t.innerHTML = ""; return; }
  let h = "<thead><tr><th class='tcol'>时间</th>";
  for(const d of GRID.dates){
    const w = "日一二三四五六"[new Date(d+"T00:00:00").getDay()];
    h += `<th>${d.slice(5)}<br><span style="font-weight:400;color:#6b7280">(${w})</span></th>`;
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
  t.innerHTML = h + "</tbody>";
  t.querySelectorAll(".cell").forEach(el=>{
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
  paintSelection();
  renderChips();
}

function paintSelection(){
  document.querySelectorAll(".cell").forEach(el=>{
    const k = [$("#center").value, el.dataset.d, el.dataset.t].join("|");
    el.classList.toggle("sel", SEL.has(k));
  });
}

function renderChips(){
  const box = $("#chips");
  const items = [...SEL.values()].sort((a,b)=>(a.date+a.from).localeCompare(b.date+b.from));
  if(!items.length){ box.innerHTML = '<div class="empty">还没选。在网格里点格子。</div>'; return; }
  box.innerHTML = "";
  for(const t of items){
    const k = keyOf(t);
    const div = document.createElement("div");
    div.className = "chip";
    div.innerHTML = `<span class="who">${esc(t.date)} ${esc(t.from)}–${esc(t.to)}</span>
      <span class="meta">${esc(t.name || t.ctrId)}${t.activity ? " · " + esc(t.activity) : ""}
      · 放位日 ${esc(releaseOf(t.date))}</span>`;
    const b = document.createElement("button");
    b.textContent = "移除";
    b.onclick = ()=>{ SEL.delete(k); renderChips(); paintSelection(); };
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
$("#clear").onclick = ()=>{ SEL.clear(); renderChips(); paintSelection(); };
$("#relogin").onclick = async ()=>{
  say("重新登录中…");
  try{ await api("/api/login", {method:"POST"}); await loadMeta(); }
  catch(e){ say("登录失败：" + e.message, "err"); }
};
$("#save").onclick = async ()=>{
  const targets = [...SEL.values()].sort((a,b)=>(a.date+a.from).localeCompare(b.date+b.from));
  if(!targets.length && !confirm("目标为空，确定要把 config.json 的 targets 清空吗？")) return;
  say("保存中…");
  try{
    const r = await api("/api/targets", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({targets})
    });
    say(`已保存 ${r.count} 个目标到 config.json`, "ok");
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
