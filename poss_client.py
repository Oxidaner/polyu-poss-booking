#!/usr/bin/env python3
"""
POSS 会话客户端：自动登录 + 抓取预订时间表
================================================
链路（2026-09-14 实测设计）：
  ① GET /poss/secure/login/loginhome.do   → 提取登录页 CSRFToken
  ② POST /poss/j_security_check           → NetID/NetPassword 登录（表单层无 MFA）
  ③ GET /starspossfbstud/.../make_book.do → 提取接口用 CSRFToken（LtpaToken2 跨应用 SSO）
  ④ POST .../timetable.json               → 预订时间表（POST，参数在 body）

会话复用：登录成功后把 cookie 序列化到 session.json，
下次运行先试旧会话，302/失效才重新登录（避免每 5 分钟登录一次触发风控）。
"""

import json
import os
import re
import shlex
import time
from datetime import datetime
from urllib.parse import quote_plus

import requests

BASE = "https://www40.polyu.edu.hk"
LOGIN_PAGE = f"{BASE}/poss/secure/login/loginhome.do"
LOGIN_POST = f"{BASE}/poss/j_security_check"
MAKE_BOOK = f"{BASE}/starspossfbstud/secure/ui_make_book/make_book.do"
MAKE_BOOK_RESULT = f"{BASE}/starspossfbstud/secure/ui_make_book/make_book_result.do"
TIMETABLE = f"{BASE}/starspossfbstud/secure/ui_make_book/timetable.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

# timetable.json 请求体的默认参数（可被 config.json 的 request 段覆盖）
DEFAULT_BODY = {
    "bookType": "INDV",
    "dataSetId": "18",
    "actvId": "101",
    "facilityId": "",
    "showCourtAreaDetails": "true",
}
# 允许从配置覆盖的键（白名单，防止配置注入无关参数）
OVERRIDABLE = set(DEFAULT_BODY)

UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _decode_ansic(s: str) -> str:
    r"""把 shell 的 $'...' ANSI-C 引号解码成真实字符（\r \n \xHH 等）。"""
    if s.startswith("$'") and s.endswith("'"):
        inner = s[2:-1]
        try:
            return inner.encode("latin1").decode("unicode_escape")
        except UnicodeDecodeError:
            pass
    return s


def _decode_ansic_loose(token: str) -> str:
    """shlex 会把 $'...' 的外层单引号剥掉只剩 $body\\r\\n，这里做兜底解码。"""
    if not token.startswith("$"):
        return token
    body = token[1:]
    if not any(seq in body for seq in ("\\r", "\\n", "\\t", "\\x", "\\0")):
        return token
    try:
        return body.encode("latin1").decode("unicode_escape")
    except UnicodeDecodeError:
        return token


def _ansic_escape(s: str) -> str:
    """把含换行/控制字符的字符串编码成 $'...' 形式，便于写回 cURL 文件。"""
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "'":
            out.append("\\'")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 32 or ord(ch) > 126:
            out.append("\\x%02x" % ord(ch))
        else:
            out.append(ch)
    return "$'" + "".join(out) + "'"


# ==================== cURL 解析与模板渲染 ====================
# 用途：浏览器「Copy as cURL」导出的原始请求 → 结构化请求要素 → 占位符模板，
# 这样校准预订接口时只需粘贴一次，无需人工映射字段名。

# 由 requests 自行管理 / 属于浏览器指纹的头，转发时丢弃；cookie 由会话自己维护
DROP_HEADERS = {"host", "content-length", "connection", "accept-encoding", "cookie"}

# 需要读取下一个 token 作为值的 curl 选项
_CURL_VALUE_FLAGS = {
    "-H", "--header", "-b", "--cookie", "-d", "--data", "--data-raw", "--data-binary",
    "--data-urlencode", "-X", "--request", "--url", "-A", "--user-agent",
    "-e", "--referer", "-o", "--output", "-u", "--user", "--connect-timeout", "--max-time",
}
# 无需值的开关选项
_CURL_BARE_FLAGS = {
    "--compressed", "-k", "--insecure", "-s", "--silent", "-L", "--location",
    "-i", "--include", "-g", "--globoff", "-v", "--verbose", "-N", "--no-buffer",
    "--fail", "-f", "--tlsv1.2", "--http1.1",
}

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d", "%d/%m/%y",
                 "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S")
_TIME_FORMATS = ("%H:%M:%S", "%H:%M", "%H%M")


def parse_flexible(value):
    """把值尽可能解析成 datetime（日期 / 时间 / 日期时间）。失败返回 None。"""
    s = str(value).strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_curl(text: str) -> dict:
    """解析 `Copy as cURL` 文本 → {method, url, headers, data, content_type}。

    支持 \\ 换行续行（含 Windows 的 ^）、单/双引号、--url / -H / -b /
    --data-raw / --data / --data-urlencode / -d / -X。cookie 一律丢弃。
    """
    cleaned = re.sub(r"[\\^][ \t]*\r?\n", " ", text.strip())
    try:
        tokens = shlex.split(cleaned)
    except ValueError as e:
        raise ValueError(f"cURL 文本无法分词（引号不配对？）: {e}") from e

    method = None
    url = None
    headers = {}
    datas = []

    i = 1 if tokens and tokens[0].endswith("curl") else 0
    while i < len(tokens):
        t = tokens[i]
        if t in _CURL_VALUE_FLAGS:
            val = tokens[i + 1] if i + 1 < len(tokens) else ""
            if t in ("-H", "--header"):
                if ":" in val:
                    k, v = val.split(":", 1)
                    headers[k.strip()] = v.strip()
            elif t in ("-d", "--data", "--data-raw", "--data-binary", "--data-urlencode"):
                datas.append(val)
            elif t in ("-X", "--request"):
                method = val.upper()
            elif t == "--url":
                url = val
            elif t in ("-A", "--user-agent"):
                headers.setdefault("User-Agent", val)
            elif t in ("-e", "--referer"):
                headers.setdefault("Referer", val)
            elif t in ("-b", "--cookie"):
                # 保留以便报告，但实际发送前会被 DROP_HEADERS 过滤掉
                headers.setdefault("Cookie", val)
            i += 2
            continue
        if t in _CURL_BARE_FLAGS:
            if t == "--compressed":
                headers.setdefault("Accept-Encoding", "gzip, deflate")
            i += 1
            continue
        if not t.startswith("-") and url is None:
            url = t
        i += 1

    if not url:
        raise ValueError("cURL 文本里找不到 URL（请确认整条命令都复制全了）")
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    decoded = [_decode_ansic_loose(_decode_ansic(d)) for d in datas]
    if decoded and "multipart/form-data" in content_type.lower():
        data = "".join(decoded)
    else:
        data = "&".join(decoded)
    if method is None:
        method = "POST" if data else "GET"
    return {
        "method": method,
        "url": url,
        "headers": headers,
        "data": data,
        "content_type": content_type,
    }


_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][\w-]*)(?::([^}]*))?\}")


def render_template(text: str, ctx: dict) -> str:
    """替换占位符。

    {key}              → 原值
    {key:%d/%m/%Y}     → 值按日期/时间解析后重新格式化
    {key:url}          → 值做 URL 编码（空格→+，冒号→%3A）
    {key:url:%d %b %Y} → 先格式化再 URL 编码
    未知 key 原样保留 —— 让错误在 dry-book 里显形，而不是静默发错请求。
    """
    def _repl(m):
        key, spec = m.group(1), m.group(2)
        if key not in ctx:
            return m.group(0)
        val = ctx[key]
        val = "" if val is None else str(val)
        if not spec:
            return val
        need_url = False
        fmt = spec
        if spec == "url" or spec.startswith("url:"):
            need_url = True
            fmt = spec[3:].lstrip(":")
        if fmt:
            dt = parse_flexible(val)
            if dt is not None:
                try:
                    val = dt.strftime(fmt)
                except ValueError:
                    pass
        return quote_plus(val) if need_url else val

    return _PLACEHOLDER_RE.sub(_repl, text)


def _render_body(body: str, ctx: dict) -> str:
    """渲染请求体。

    有意不做「解码→替换→重编码」：抓包里的 searchFormString 之类字段，
    其值内部本身含 & 和 =，解码后重新解析会把字段切碎、请求结构就变了。
    这里只做纯文本替换，浏览器原来的编码字节原样保留；
    需要编码的占位符用 {key:url} 显式声明。
    """
    return render_template(body, ctx)


def find_unresolved(text: str) -> list:
    """找出渲染后仍残留的占位符 —— 它们会被原样发给服务器，必须拦下来。"""
    return sorted({m.group(0) for m in _PLACEHOLDER_RE.finditer(text or "")})


def to_curl(method: str, url: str, headers: dict, data: str = "") -> str:
    """把请求要素还原成一段可读的 cURL（用于落盘 / 打印审查）。"""

    def q(s: str) -> str:
        return "'" + str(s).replace("'", "'\\''") + "'"

    parts = ["curl --url " + q(url)]
    if method and method.upper() != ("POST" if data else "GET"):
        parts.append(f"-X {method.upper()}")
    for k, v in headers.items():
        parts.append("-H " + q(f"{k}: {v}"))
    if data:
        # multipart 请求体含换行和 boundary，普通单引号包不住，写成 $'...'
        if "\n" in data or "\r" in data or "'" in data or any(ord(c) < 32 for c in data):
            parts.append("--data-raw " + _ansic_escape(data))
        else:
            parts.append("--data-raw " + q(data))
    return " \\\n  ".join(parts)


def mask_secrets(text: str, ctx: dict) -> str:
    """打印前遮掉 token 类值，避免日志泄漏会话凭据。"""
    out = str(text)
    for key in ("csrf", "CSRFToken", "LtpaToken2", "JSESSIONID"):
        v = ctx.get(key)
        if v:
            out = out.replace(str(v), "***")
    return out


class LoginFailed(Exception):
    """账号密码被拒 / 触发额外验证。"""


class SessionExpired(Exception):
    """会话失效（302 / 登录页），需要重新登录。"""


class BookingNotCalibrated(Exception):
    """预订接口尚未校准（缺少真实抓包的 endpoint / params）。"""


class BookingFailed(Exception):
    """提交预订被服务端拒绝。"""


class BookingResult:
    def __init__(self, ok: bool, message: str, raw: str = "", inconclusive: bool = False,
                 confirmed_failure: bool = False):
        self.ok = ok
        self.message = message
        self.raw = raw
        # 结果不明（如 HTTP 302 且拿不到结果页）：既不能当成抢到，也绝不能再重试 ——
        # 防止重复扣费。此时只能推人去看一眼。
        self.inconclusive = inconclusive
        # 服务端明确说「这次没订上」（如时段已被别人抢走）：这不是赌博，是事实，
        # 所以可以放心去试下一个候选，不算重复扣费风险。
        self.confirmed_failure = confirmed_failure

    def __repr__(self):
        return f"BookingResult(ok={self.ok}, message={self.message!r})"


# ---- 结果页判定 ----
# POSS 提交后走 PRG（POST→Redirect→GET）模式：make_book_submit.do 处理完 → 302 到
# make_book_result.do → 结果（成功/失败）存在会话里由该页渲染出来。
# 所以「跟着 302 走一步」就能拿到权威结论，不用再让用户人工去查。

_TAG_RE = re.compile(r"(?s)<[^>]+>")
_DROP_BLOCK_RE = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")


def html_to_text(html: str) -> str:
    """粗剥 HTML，留下可比对的可见文本（不做完整 DOM 解析，够用即可）。"""
    import html as _html

    s = _DROP_BLOCK_RE.sub(" ", html or "")
    s = _TAG_RE.sub(" ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# 默认「明确失败」标志。刻意选不会误伤成功页的短语：
# 英文 "unsuccessful" 本身含 "success"，所以判定时必须先看失败词再看成功词。
DEFAULT_FAILURE_KEYWORDS = [
    "unsuccessful", "not successful", "booking failed", "cannot be booked",
    "unable to book", "no longer available", "not available", "already been booked",
    "already booked", "have been booked", "was booked", "duplicate",
    "不成功", "失败", "額滿", "额满", "名額已滿", "名额已满", "已被", "已經被", "已经被",
    "餘額不足", "余额不足", "押金不足",
]
DEFAULT_SUCCESS_KEYWORDS = ["success", "successful", "成功", "confirmed", "已確認", "已确认"]


def classify_result(text: str, success_keywords=None, failure_keywords=None):
    """判定结果页 → (verdict, matched)

    verdict ∈ {"ok", "fail", "unknown"}

    顺序很重要：**先判失败**。英文结果页里「unsuccessful」包含「success」子串，
    若先判成功，一次失败的预订会被读成抢到了 —— 我们就此收工，然后你要自己
    发现问题。反过来判错的代价只是多试一次，安全得多。

    只比对「去掉了 script/style 的内容」：既不漏掉藏在隐藏字段/属性里的结果标志，
    也不会因为某个 JS 库里恰巧有 "success" 三个字就把失败读成成功。
    """
    visible = html_to_text(text)
    without_js = _DROP_BLOCK_RE.sub(" ", text or "")
    low = (visible + "\n" + without_js).lower()
    fails = [k for k in (failure_keywords or DEFAULT_FAILURE_KEYWORDS) if k.lower() in low]
    if fails:
        return "fail", fails
    oks = [k for k in (success_keywords or DEFAULT_SUCCESS_KEYWORDS) if k.lower() in low]
    if oks:
        return "ok", oks
    return "unknown", []



class PossClient:
    def __init__(self, netid: str, password: str, fb_user_id: str = "657032"):
        if not netid or not password:
            raise LoginFailed("未配置 POSS_NETID / POSS_PASSWORD")
        self.netid = netid
        self.password = password
        self.fb_user_id = fb_user_id
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        })
        self.api_csrf = None
        self.debug = os.environ.get("POSS_DEBUG") == "1"

    # ---------- 登录 ----------

    def login(self):
        """①→③ 完整登录，成功后 api_csrf 可用。"""
        r = self.s.get(LOGIN_PAGE, timeout=30)
        r.raise_for_status()
        m = re.search(r'name="CSRFToken"\s+value="(' + UUID_RE + r')"', r.text)
        if not m:
            raise LoginFailed("登录页上找不到 CSRFToken（页面结构可能变了）")
        login_token = m.group(1)

        r = self.s.post(
            LOGIN_POST,
            data={
                "j_username": self.netid,
                "j_password": self.password,
                "otheruser": "N",
                "buttonAction": "loginButton",
                "CSRFToken": login_token,
            },
            headers={"Referer": LOGIN_PAGE, "Origin": BASE},
            allow_redirects=True,
            timeout=30,
        )
        # 登录失败会带回登录表单；成功则落在 home 页
        if "j_security_check" in r.text or "loginform" in r.text:
            raise LoginFailed(
                "登录被拒：回到登录页（账号密码错误，或该账号触发了额外验证）"
            )
        if self.debug:
            print(f"[login] 登录后落地: {r.url}")
            print(f"[login] cookies: {sorted(self.s.cookies.get_dict().keys())}")

        self.api_csrf = self._fetch_api_token()
        if not self.api_csrf:
            raise LoginFailed("make_book.do 页面上找不到接口 CSRFToken")
        return True

    def _fetch_api_token(self):
        """③ 打开预订页，提取 timetable.json 用的 CSRFToken。"""
        r = self.s.get(MAKE_BOOK, timeout=30)
        if "loginhome" in r.url or "loginform" in r.text:
            raise SessionExpired("make_book.do 仍被挡在登录页（LTPA 未生效？）")
        tokens = re.findall(r"CSRFToken[\"'=:\s]+(" + UUID_RE + r")", r.text)
        if self.debug:
            print(f"[login] make_book.do: {r.status_code}, 找到 {len(tokens)} 个 token")
        return tokens[-1] if tokens else None

    # ---------- 时间表 ----------

    def timetable(self, ctr_id: str, search_date: str = "", params: dict | None = None):
        """④ POST timetable.json，返回原始 JSON。会话失效抛 SessionExpired。

        ctr_id / search_date 必填逻辑在这；其余 body 参数可用 params 覆盖默认值
        （来自 config.json 的 request 段，见 DEFAULT_BODY / OVERRIDABLE）。
        """
        if not self.api_csrf:
            raise SessionExpired("尚未登录（无接口 CSRFToken）")
        body = dict(DEFAULT_BODY)
        for k in OVERRIDABLE:
            if params and k in params and params[k] is not None:
                body[k] = str(params[k])
        body.update({
            "CSRFToken": self.api_csrf,
            "fbUserId": self.fb_user_id,
            "searchDate": search_date,
            "ctrId": str(ctr_id),
        })
        r = self.s.post(
            f"{TIMETABLE}?CSRFToken={self.api_csrf}",
            data=body,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": MAKE_BOOK,
                "Origin": BASE,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            allow_redirects=False,  # 302 = 会话失效的明确信号
            timeout=30,
        )
        if r.status_code != 200:
            raise SessionExpired(f"timetable.json HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise SessionExpired(f"返回非 JSON（疑似登录页/错误页）: {str(e)[:100]}") from e

    # ---------- 提交预订 ----------

    def book(self, payload: dict, endpoint: str = "",
             success_keywords: list | None = None) -> BookingResult:
        """⑤ 提交预订。

        ⚠️ 端点与字段必须先按真实抓包校准（config.booking.endpoint / params）。
        凭猜测去 POST 一个会扣押金的接口是不负责任的，因此未校准时直接抛错。

        payload 由 monitor 按 config.booking.params 模板 + 时段上下文组装。
        """
        if not endpoint:
            raise BookingNotCalibrated(
                "预订接口尚未校准：请在 POSS 手动预订一次并抓取那条提交请求的 cURL，"
                "把 URL 路径填到 config.booking.endpoint，表单字段填到 config.booking.params。"
            )
        if not self.api_csrf:
            raise SessionExpired("尚未登录（无接口 CSRFToken）")
        body = dict(payload)
        body.setdefault("CSRFToken", self.api_csrf)
        url = endpoint if endpoint.startswith("http") else BASE + endpoint
        r = self.s.post(
            url,
            data=body,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": MAKE_BOOK,
                "Origin": BASE,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            allow_redirects=False,
            timeout=30,
        )
        if r.status_code == 302:
            loc = r.headers.get("Location", "?")
            if "login" in loc.lower() or "loginhome" in loc.lower():
                raise SessionExpired(f"提交预订被重定向到登录页（HTTP 302 → {loc}）")
            # 非登录跳转：可能已经下单成功。绝不能重试，交给人去确认。
            return BookingResult(False, f"HTTP 302 → {loc}（结果不明，请人工确认）",
                                 inconclusive=True)
        text = r.text or ""
        ok_http = r.status_code == 200
        kws = success_keywords or ["success", "successful", "成功"]
        hit = any(k.lower() in text.lower() for k in kws)
        ok = ok_http and hit
        msg = "预订成功" if ok else f"HTTP {r.status_code} 未匹配成功标志: {text[:200]}"
        return BookingResult(ok, msg, text[:2000])

    # ---------- 提交预订（cURL 模板路径，2026-09-14 起为主路径） ----------

    def build_booking_request(self, curl_text: str, ctx: dict) -> dict:
        """把 cURL 模板 + 上下文渲染成 {method,url,headers,body,content_type}。

        ctx 里未显式给出的 csrf / fbUserId 用当前会话值兜底。
        """
        parsed = parse_curl(curl_text)
        full_ctx = dict(ctx)
        full_ctx.setdefault("csrf", self.api_csrf or "")
        full_ctx.setdefault("fbUserId", self.fb_user_id)
        url = render_template(parsed["url"], full_ctx)
        body = _render_body(parsed["data"], full_ctx)
        headers = {k: v for k, v in parsed["headers"].items()
                   if k.lower() not in DROP_HEADERS}
        if not url.startswith("http"):
            url = BASE + url
        return {
            "method": parsed["method"],
            "url": url,
            "headers": headers,
            "body": body,
            "content_type": parsed["content_type"],
        }

    def book_via_curl(self, curl_text: str, ctx: dict,
                      success_keywords: list | None = None) -> BookingResult:
        """按抓包模板提交预订。body 原样转发（表单或 JSON 都不动）。"""
        if not self.api_csrf:
            raise SessionExpired("尚未登录（无接口 CSRFToken）")
        req = self.build_booking_request(curl_text, ctx)

        # 安全闸门：占位符没解析出来就绝不发出去 —— 宁可报错，也不往学校服务器
        # 发一条带着 {facilityId} 字面量的请求。
        leftover = find_unresolved(req["url"])
        if "json" not in (req["content_type"] or "").lower():
            leftover += find_unresolved(req["body"])
        if leftover:
            raise BookingNotCalibrated(
                "预订模板里有取不到值的占位符: " + ", ".join(leftover)
                + "。通常是 timetable 数据里没提供该字段（如 facilityId），"
                  "需要先跑 --dump 看清时间表结构再调整解析器。"
            )

        kwargs = {"headers": req["headers"], "allow_redirects": False, "timeout": 30}
        if req["method"].upper() == "GET":
            r = self.s.get(req["url"], **kwargs)
        else:
            # 传原始字符串：Content-Type 用抓包里的原值，不做重新编码
            kwargs["data"] = req["body"]
            r = self.s.post(req["url"], **kwargs)
        if r.status_code == 302:
            loc = r.headers.get("Location", "?")
            if "login" in loc.lower() or "loginhome" in loc.lower():
                raise SessionExpired(f"提交预订被重定向到登录页（HTTP 302 → {loc}）")
            return BookingResult(False, f"HTTP 302 → {loc}（结果不明，请人工确认）",
                                 inconclusive=True)
        text = r.text or ""
        kws = success_keywords or ["success", "successful", "成功", "已确认"]
        ok = r.status_code == 200 and any(k.lower() in text.lower() for k in kws)
        msg = "预订成功" if ok else f"HTTP {r.status_code} 未匹配成功标志: {text[:200]}"
        return BookingResult(ok, msg, text[:2000])

    # ---------- 预订结果页 ----------

    def open_result_page(self, url: str, referer: str = "", max_hops: int = 3):
        """跟着 302 打开预订结果页（PRG 的最后一步，等价于浏览器自动跳转）。

        返回最终响应对象；中途被踢到登录页抛 SessionExpired。
        """
        r = None
        for _ in range(max_hops):
            headers = {"Referer": referer} if referer else {}
            r = self.s.get(url, headers=headers, allow_redirects=False, timeout=30)
            if r.status_code not in (301, 302):
                return r
            loc = r.headers.get("Location", "")
            if not loc:
                return r
            if "login" in loc.lower() or "loginhome" in loc.lower():
                raise SessionExpired(f"打开结果页时被踢到登录页（302 → {loc}）")
            url = loc if loc.startswith("http") else BASE + loc
        return r

    # ---------- 会话持久化 ----------
    def save_session(self, path: str):
        """把 cookie 序列化到文件（保留 domain/path，跨运行复用）。"""
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self.s.cookies
        ]
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"t": time.time(), "cookies": cookies}, f)

    def load_session(self, path: str, max_age: int = 90 * 60) -> bool:
        """尝试恢复旧会话；成功且未超龄返回 True。"""
        if not os.path.exists(path):
            return False
        try:
            with open(path, encoding="utf-8") as f:
                blob = json.load(f)
            if time.time() - blob.get("t", 0) > max_age:
                return False
            self.s.cookies.clear()
            for c in blob["cookies"]:
                self.s.cookies.set(
                    c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/")
                )
            # 接口 CSRFToken 也在会话期内有效，一并恢复
            self.api_csrf = blob.get("api_csrf")
            return True
        except (OSError, KeyError, json.JSONDecodeError):
            return False
