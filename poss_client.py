#!/usr/bin/env python3
"""PolyU POSS client based on the site's real page data and JSON contracts.

The site exposes the selection tree in ``dependentJson`` on make_book.do and
returns availability as ``facilityIds - occupiedFacilityIds`` in timetable.json.
This module keeps those contracts explicit instead of guessing from arbitrary
JSON keys or empty HTML selects.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlparse

import requests

BASE = "https://www40.polyu.edu.hk"
LOGIN_PAGE = f"{BASE}/poss/secure/login/loginhome.do"
LOGIN_POST = f"{BASE}/poss/j_security_check"
MAKE_BOOK = f"{BASE}/starspossfbstud/secure/ui_make_book/make_book.do"
TIMETABLE = f"{BASE}/starspossfbstud/secure/ui_make_book/timetable.json"
RESULT_PAGE = f"{BASE}/starspossfbstud/secure/ui_make_book/make_book_result.do"
MY_RECORD = f"{BASE}/starspossfbstud/secure/ui_my_record/my_record.do"
MAKE_DEPOSIT = f"{BASE}/starspossfbstud/secure/ui_make_dep/make_dep.do"
VIEW_BOOK_DAYS = f"{BASE}/starspossfbstud/secure/ui_make_book/view_book_days.json"
TZ = timezone(timedelta(hours=8))
SESSION_HEARTBEAT_SECONDS = 5 * 60
PAGE_TIMEOUT = (3.05, 10)
TIMETABLE_TIMEOUT = (3.05, 8)
CONFIRMATION_TIMEOUT = (3.05, 45)
FINAL_TIMEOUT = (3.05, 30)
RECORD_TIMEOUT = (10, 30)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class PossError(RuntimeError):
    """Base error for a user-actionable POSS failure."""


class LoginError(PossError):
    pass


class SessionExpired(PossError):
    pass


class TimetableError(PossError):
    def __init__(self, message: str, http_status: int | None = None,
                 retry_after: float | None = None,
                 response_info: dict[str, Any] | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.retry_after = retry_after
        self.response_info = response_info or {}


class BookingError(PossError):
    pass


class BookingAttemptError(BookingError):
    def __init__(self, message: str, stage: str, reason_code: str,
                 events: list[dict[str, Any]], uncertain: bool = False,
                 retry_after: float | None = None):
        super().__init__(message)
        self.stage = stage
        self.reason_code = reason_code
        self.events = list(events)
        self.uncertain = uncertain
        self.retry_after = retry_after


def transport_reason(exc: requests.RequestException) -> str:
    """Classify transport failures without logging URLs, tokens or response bodies."""
    if isinstance(exc, requests.exceptions.ProxyError):
        return "proxy_error"
    if isinstance(exc, requests.exceptions.SSLError):
        detail = str(exc).lower()
        if "proxy" in detail:
            return "proxy_error"
        if "certificate verify failed" in detail or "hostname mismatch" in detail:
            return "tls_certificate_error"
        if "eof" in detail or "connection reset" in detail:
            return "tls_connection_closed"
        if "handshake" in detail:
            return "tls_handshake_error"
        return "tls_error"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    return "network_error"


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            delay = parsedate_to_datetime(value) - datetime.now(timezone.utc)
            return max(0.0, delay.total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


@dataclass(frozen=True)
class Selection:
    dataset_id: str
    activity_id: str
    center_id: str
    facility_id: str
    date: str
    start: str
    end: str
    activity_name: str = ""
    center_name: str = ""
    facility_name: str = ""
    book_type: str = "INDV"


@dataclass
class BookingOutcome:
    status: str
    message: str
    stage: str = ""
    reason_code: str = ""
    center: str = ""
    facility: str = ""
    date: str = ""
    start: str = ""
    end: str = ""
    charge: Any = None
    refundable_fee: Any = None
    http_status: int | None = None
    response_url: str = ""
    result_html: str = ""
    verified: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)


class _FormParser(HTMLParser):
    """Small form parser for the server-rendered confirmation page."""

    def __init__(self):
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._form: dict[str, Any] | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "form":
            self._form = {"attrs": attrs, "fields": []}
            self.forms.append(self._form)
        elif tag.lower() == "input" and self._form is not None:
            name = attrs.get("name")
            if not name:
                return
            kind = (attrs.get("type") or "text").lower()
            if kind in {"submit", "button", "image", "reset", "file"}:
                return
            if kind in {"checkbox", "radio"} and "checked" not in attrs:
                return
            self._form["fields"].append((name, attrs.get("value", "")))

    def handle_endtag(self, tag):
        if tag.lower() == "form":
            self._form = None


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", value or "")
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html_lib.unescape(value)).strip()


def response_diagnostics(response) -> dict[str, Any]:
    """Keep response evidence without recording private page text or URLs."""
    text = response.text or ""
    body = text.encode("utf-8", errors="replace")
    visible = html_to_text(text).lower()
    hints = (
        ("rate_limit", ("too many requests", "rate limit", "request limit")),
        ("access_denied", ("access denied", "you have been blocked", "forbidden")),
        ("server_busy", ("service unavailable", "server is busy", "internal server error")),
        ("session_expired", ("session expired", "please log in", "please login")),
        ("booking_limit", ("maximum number of bookings", "booking quota",
                           "booking limit", "too many bookings", "預訂次數")),
        ("insufficient_balance", ("insufficient balance", "insufficient deposit",
                                  "not enough deposit", "餘額不足", "余额不足")),
        ("slot_taken", ("already been booked", "not available", "名額已滿", "額滿")),
    )
    hint = next((name for name, phrases in hints
                 if any(phrase in visible for phrase in phrases)), "unclassified")
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    info = {
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest()[:16] if body else None,
        "content_type": content_type if content_type in (
            "text/html", "text/plain", "application/json",
        ) else "other",
        "response_hint": hint,
        "confirmation_form": "boMakeBookFacilities[0].facilityId" in text,
    }
    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get("Location")
        if not location:
            target = "missing"
        else:
            parsed = urlparse(urljoin(BASE, location))
            path = parsed.path.lower()
            target = ("external" if parsed.hostname != urlparse(BASE).hostname
                      else "login" if "login" in path
                      else "booking_result" if "result" in path
                      else "booking" if "make_book" in path else "other")
        info["redirect_target"] = target
    return info


def parse_record_page(page: str) -> tuple[list[dict[str, str]], bool]:
    """Parse one My Record page; reject unknown layouts instead of publishing partial data."""
    headers = re.findall(r"(?is)<th\b[^>]*>.*?</th>", page)
    names = [html_to_text(header).lower() for header in headers]
    if not {"transaction date", "booking", "transaction type"}.issubset(names):
        raise PossError("My Record 表格结构已变化")
    next_button = re.search(
        r"""(?is)<button\b(?=[^>]*\bname\s*=\s*["']next["'])[^>]*>""", page
    )
    if not next_button:
        raise PossError("My Record 缺少分页按钮")
    has_next = not bool(re.search(r"\bdisabled\b", next_button.group(), re.I))
    records = []
    for row in re.findall(r"(?is)<tr\b[^>]*>.*?</tr>", page):
        cells = re.findall(r"(?is)<td\b[^>]*>.*?</td>", row)
        if len(cells) < 3:
            continue
        kind = html_to_text(cells[2])
        if not kind.startswith("Individual Booking"):
            continue
        spans = re.findall(r"(?is)<span\b[^>]*>.*?</span>", cells[1])
        if len(spans) < 2:
            raise PossError("My Record 预约详情缺失")
        venue = html_to_text(spans[0])
        slot = html_to_text(spans[1])
        match = re.fullmatch(
            r"(\d{1,2} [A-Za-z]{3} \d{4}) \([A-Za-z]{3}\) "
            r"(\d{1,2}:\d{2} [AP]M) - (\d{1,2}:\d{2} [AP]M)", slot
        )
        if not venue or not match or not re.fullmatch(
            r"Individual Booking(?: Transfer (?:To|From)| Refund"
            r"(?: \(transferred booking\))?)? "
            r"\((?:Confirmed|Cancelled|Transfered|Transferred)\)", kind
        ):
            raise PossError("My Record 预约日期或状态无法识别")
        try:
            day = datetime.strptime(match.group(1), "%d %b %Y").date().isoformat()
            start = datetime.strptime(match.group(2), "%I:%M %p").strftime("%H:%M")
            end = datetime.strptime(match.group(3), "%I:%M %p").strftime("%H:%M")
            transaction_at = datetime.strptime(
                html_to_text(cells[0]), "%d %b %Y %I:%M %p"
            ).isoformat()
        except ValueError as exc:
            raise PossError("My Record 交易日期无法识别") from exc
        parts = re.match(r"^(.*?)\s+[A-Z][A-Z0-9() ]{2,}\s+-\s+(.+)$", venue)
        center, facility = (parts.group(1), parts.group(2)) if parts else (venue, venue)
        records.append({
            "date": day, "start": start, "end": end,
            "venue": venue, "center": center, "facility": facility,
            "transaction_at": transaction_at,
            "status": "confirmed" if kind.endswith("(Confirmed)") else "inactive",
        })
    return records, has_next


def active_record_bookings(pages: list[str]) -> list[dict[str, str]]:
    """The newest transaction for a court and time determines its active state."""
    latest: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for page in pages:
        records, _ = parse_record_page(page)
        for row in records:
            key = (row["date"], row["start"], row["end"], row["venue"].casefold())
            if key not in latest or row["transaction_at"] > latest[key]["transaction_at"]:
                latest[key] = row
    return [
        {key: value for key, value in row.items() if key not in ("status", "transaction_at")}
        for row in sorted(latest.values(), key=lambda r: (
            r["date"], r["start"], r["venue"]
        ))
        if row["status"] == "confirmed" and row["date"] >= datetime.now(TZ).date().isoformat()
    ]


def record_page_before(page: str, cutoff: date) -> bool:
    """POSS lists transactions newest first; older transactions cannot book future slots."""
    if not {"transaction date", "booking", "transaction type"}.issubset({
        html_to_text(header).lower()
        for header in re.findall(r"(?is)<th\b[^>]*>.*?</th>", page)
    }) or not re.search(r"""(?is)<button\b(?=[^>]*\bname\s*=\s*["']next["'])[^>]*>""", page):
        raise PossError("My Record 表格或分页结构已变化")
    timestamps = []
    for row in re.findall(r"(?is)<tr\b[^>]*>.*?</tr>", page):
        cells = re.findall(r"(?is)<td\b[^>]*>.*?</td>", row)
        if len(cells) < 3:
            continue
        try:
            timestamps.append(datetime.strptime(
                html_to_text(cells[0]), "%d %b %Y %I:%M %p"
            ).date())
        except ValueError as exc:
            raise PossError("My Record 交易日期无法识别") from exc
    return bool(timestamps) and max(timestamps) < cutoff


def parse_deposit_balance(page: str) -> str | None:
    match = re.search(
        r"\b(?:Current\s+)?Deposit\s+Balance\s*:\s*(?:HK\s*)?\$\s*"
        r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)\b",
        html_to_text(page), re.I,
    )
    return f"{Decimal(match.group(1).replace(',', '')):.2f}" if match else None


def parse_hm(value: str) -> str | None:
    for fmt in ("%H:%M", "%H:%M:%S", "%H%M"):
        try:
            return datetime.strptime(str(value).strip(), fmt).strftime("%H:%M")
        except ValueError:
            pass
    return None


def parse_date(value: str | date | datetime) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def display_date(value: str | date | datetime) -> str:
    parsed = parse_date(value)
    if not parsed:
        raise ValueError(f"invalid date: {value}")
    return f"{parsed.day:02d} {parsed.strftime('%b')} {parsed.year}"


def iso_date(value: str | date | datetime) -> str:
    parsed = parse_date(value)
    if not parsed:
        raise ValueError(f"invalid date: {value}")
    return parsed.isoformat()


def booking_rule_from_message(message: str) -> dict[str, Any]:
    """Read POSS's authoritative booking horizon from its banner text."""
    match = re.search(
        r"up to\s+(\d+)\s+days?\s+in advance\s+from\s+(\d{1,2}:\d{2})",
        html_to_text(message),
        re.I,
    )
    if not match:
        return {"leadDays": 7, "openTime": "08:30"}
    return {"leadDays": int(match.group(1)), "openTime": parse_hm(match.group(2)) or "08:30"}


def booking_release_at(target_date: str | date | datetime,
                       rule: dict[str, Any] | None = None) -> datetime:
    """Return the exact Hong Kong time when a target date becomes bookable."""
    parsed = parse_date(target_date)
    if not parsed:
        raise BookingError(f"目标日期无效: {target_date}")
    rule = rule or {"leadDays": 7, "openTime": "08:30"}
    open_time = parse_hm(str(rule.get("openTime") or "08:30")) or "08:30"
    hour, minute = (int(part) for part in open_time.split(":"))
    return datetime.combine(
        parsed - timedelta(days=int(rule.get("leadDays", 7))),
        datetime.min.time(),
        tzinfo=TZ,
    ).replace(hour=hour, minute=minute)


def _extract_assignment(text: str, variable: str) -> Any:
    """Decode a JSON value assigned to a JS variable, including nested arrays."""
    marker = re.compile(
        rf"{re.escape(variable)}\s*=\s*", re.I
    ).search(text or "")
    if not marker:
        raise PossError(f"make_book.do 没找到 {variable}")
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(text[marker.end():].lstrip())
    except json.JSONDecodeError as exc:
        raise PossError(f"无法解析 POSS 的 {variable}: {exc}") from exc
    return value


def _normalise_tree(nodes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for node in nodes or []:
        value = str(node.get("value", ""))
        label = html_lib.unescape(str(node.get("label", value))).strip()
        children = _normalise_tree(node.get("boSelectOptionChildren"))
        out.append({"value": value, "label": label, "children": children})
    return out


def _find_node(nodes: list[dict[str, Any]], value: str) -> dict[str, Any] | None:
    return next((node for node in nodes if str(node["value"]) == str(value)), None)


def option_tree(html: str) -> list[dict[str, Any]]:
    return _normalise_tree(_extract_assignment(html, 'dependentJson["boDataSetOptionsJson"]'))


def tree_options(tree: list[dict[str, Any]], dataset_id: str) -> dict[str, Any]:
    dataset = _find_node(tree, dataset_id)
    if not dataset:
        raise PossError(f"POSS 没有找到 Facility / Category {dataset_id}")
    return dataset


def tree_context(tree: list[dict[str, Any]], dataset_id: str, activity_id: str,
                 center_id: str, facility_id: str = "") -> dict[str, Any]:
    dataset = tree_options(tree, dataset_id)
    activity = _find_node(dataset["children"], activity_id)
    if not activity:
        raise PossError(f"当前 Category 下没有 Activity {activity_id}")
    center = _find_node(activity["children"], center_id)
    if not center:
        raise PossError(f"当前 Activity 下没有 Center {center_id}")
    facility = _find_node(center["children"], facility_id) if facility_id else None
    return {"dataset": dataset, "activity": activity, "center": center, "facility": facility}


def _available_facilities(slot: dict[str, Any]) -> list[str]:
    occupied = {str(value) for value in slot.get("occupiedFacilityIds") or []}
    return list(dict.fromkeys(
        str(value) for value in slot.get("facilityIds") or []
        if str(value) not in occupied
    ))


def _timestamp_date(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value) / 1000, TZ).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def normalize_timetable(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert POSS's official timetable shape without inventing availability."""
    if payload.get("error"):
        message = ((payload.get("data") or {}).get("message")
                   if isinstance(payload.get("data"), dict) else None)
        raise TimetableError(message or "POSS 拒绝了时间表请求")
    data = payload.get("data") or {}
    columns = []
    for column in data.get("timeSlotColumns") or []:
        slots = []
        column_date = _timestamp_date(column.get("date"))
        for raw in column.get("timeSlots") or []:
            scheme = raw.get("chargingScheme") or {}
            available = _available_facilities(raw)
            statuses = []
            if raw.get("hasMaintenance"):
                statuses.append("维护");
            if raw.get("hasExam"):
                statuses.append("考试占用")
            if raw.get("hasReservation"):
                statuses.append("已有预订")
            if raw.get("hasConfirmation"):
                statuses.append("已有确认")
            slots.append({
                "date": column_date,
                "title": column.get("title", ""),
                "centerAlias": column.get("centerAlias"),
                "facilityAlias": column.get("facilityAlias"),
                "start": parse_hm(raw.get("fromTime", "")) or str(raw.get("fromTime", "")),
                "end": parse_hm(raw.get("toTime", "")) or str(raw.get("toTime", "")),
                "left": len(available),
                "facilityIds": [str(value) for value in raw.get("facilityIds") or []],
                "availableFacilityIds": available,
                "charge": scheme.get("charge"),
                "refundableFee": scheme.get("rfndRsvFee"),
                "color": scheme.get("colorCode"),
                "status": " / ".join(statuses),
                "hasMaintenance": bool(raw.get("hasMaintenance")),
                "hasExam": bool(raw.get("hasExam")),
                "raw": raw,
            })
        columns.append({
            "date": column_date,
            "title": column.get("title", ""),
            "centerAlias": column.get("centerAlias"),
            "facilityAlias": column.get("facilityAlias"),
            "slots": slots,
        })
    return {
        "message": html_to_text(data.get("advanceBookMsg", "")),
        "bookingRule": booking_rule_from_message(data.get("advanceBookMsg", "")),
        "advanceBookDate": data.get("advanceBookDate"),
        "advanceBookStartDate": data.get("advanceBookStartDate"),
        "duration": data.get("duration") or 1,
        "columns": columns,
        "charges": data.get("timeSlotCharges") or [],
        "flags": {
            key: bool(data.get(key))
            for key in ("hasMaintenance", "hasExam", "hasReservation", "hasConfirmation")
        },
        "raw": payload,
    }


def iter_slots(table: dict[str, Any]):
    for column in table.get("columns") or []:
        yield from column.get("slots") or []


def find_slot(table: dict[str, Any], target_date: str, start: str, end: str = "") -> dict[str, Any] | None:
    target = iso_date(target_date)
    for slot in iter_slots(table):
        if slot["date"] == target and slot["start"] == start and (not end or slot["end"] == end):
            return slot
    return None


class PossClient:
    def __init__(self, netid: str, password: str, fb_user_id: str = "657032"):
        if not netid or not password:
            raise LoginError("请先配置 POSS NetID 和 NetPassword")
        self.netid = netid
        self.password = password
        self.fb_user_id = str(fb_user_id or "657032")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        })
        self.csrf = ""
        self.page_html = ""
        self.tree: list[dict[str, Any]] = []
        self._last_session_check = 0.0

    @staticmethod
    def _is_login_page(response) -> bool:
        url = str(response.url).lower()
        text = str(response.text or "").lower()
        return (
            "loginhome" in url
            or "j_security_check" in text
            or bool(re.search(r'<input[^>]+name=["\']j_username["\']', text))
        )

    def login(self) -> dict[str, Any]:
        response = self.session.get(LOGIN_PAGE, timeout=PAGE_TIMEOUT)
        response.raise_for_status()
        match = re.search(r'name=["\']CSRFToken["\']\s+value=["\'](' + UUID_RE + r')', response.text)
        if not match:
            raise LoginError("登录页缺少 CSRFToken，POSS 页面结构可能已变化")
        response = self.session.post(
            LOGIN_POST,
            data={
                "j_username": self.netid,
                "j_password": self.password,
                "otheruser": "N",
                "buttonAction": "loginButton",
                "CSRFToken": match.group(1),
            },
            headers={"Referer": LOGIN_PAGE, "Origin": BASE},
            allow_redirects=True,
            timeout=PAGE_TIMEOUT,
        )
        if self._is_login_page(response):
            raise LoginError("登录失败：NetID 或 NetPassword 不正确，或账号需要额外验证")
        return self.load_booking_page()

    def load_booking_page(self) -> dict[str, Any]:
        response = self.session.get(MAKE_BOOK, timeout=PAGE_TIMEOUT)
        if self._is_login_page(response):
            raise SessionExpired("POSS 会话已失效，请重新登录")
        response.raise_for_status()
        self.page_html = response.text
        tokens = re.findall(r"CSRFToken[\"'=:\s]+(" + UUID_RE + r")", response.text)
        if not tokens:
            raise PossError("预订页缺少接口 CSRFToken")
        self.csrf = tokens[-1]
        self.tree = option_tree(response.text)
        match = re.search(
            r'<input\b[^>]*\bname=["\']fbUserId["\'][^>]*\bvalue=["\'](\d+)["\']',
            response.text, re.I,
        )
        if not match:
            raise PossError("预订页缺少当前账号的 fbUserId")
        self.fb_user_id = match.group(1)
        self._last_session_check = time.monotonic()
        return self.metadata()

    def heartbeat(self, force: bool = False) -> bool:
        """Touch the authenticated POSS session and refresh page-scoped data."""
        if not force and time.monotonic() - self._last_session_check < SESSION_HEARTBEAT_SECONDS:
            return False
        self.load_booking_page()
        return True

    def ensure_ready(self) -> dict[str, Any]:
        if not self.csrf or not self.tree:
            return self.login()
        try:
            self.heartbeat()
        except (SessionExpired, requests.RequestException):
            return self.login()
        return self.metadata()

    def metadata(self) -> dict[str, Any]:
        return {
            "csrf": self.csrf,
            "datasets": [
                {"value": node["value"], "label": node["label"]}
                for node in self.tree
            ],
            "tree": self.tree,
            "today": datetime.now(TZ).date().isoformat(),
            "windowDays": 15,
        }

    def deposit_balance(self) -> dict[str, str] | None:
        """Read the authenticated account's HKD deposit without changing it."""
        if not self.page_html:
            self.ensure_ready()
        amount = parse_deposit_balance(self.page_html)
        if amount is None:
            response = self.session.get(MAKE_DEPOSIT, timeout=PAGE_TIMEOUT)
            if self._is_login_page(response):
                raise SessionExpired("POSS 会话已失效，无法读取余额")
            response.raise_for_status()
            amount = parse_deposit_balance(response.text)
        return {"amount": amount, "currency": "HKD"} if amount is not None else None

    def record_bookings(self) -> list[dict[str, str]]:
        """Read pages covering every still-bookable date, failing closed on partial data."""
        pages = []
        lead_days = booking_rule_from_message(self.page_html)["leadDays"]
        cutoff = datetime.now(TZ).date() - timedelta(days=max(15, lead_days + 1))
        for offset in range(0, 10000, 10):
            response = self.session.get(
                MY_RECORD, params={"txFirstRecord": offset, "txMaxResults": 10},
                timeout=RECORD_TIMEOUT,
            )
            if self._is_login_page(response):
                raise SessionExpired("读取 My Record 时会话已失效")
            response.raise_for_status()
            if record_page_before(response.text, cutoff):
                return active_record_bookings(pages)
            _, has_next = parse_record_page(response.text)
            pages.append(response.text)
            if not has_next:
                return active_record_bookings(pages)
        raise PossError("My Record 超过 1000 页，拒绝发布不完整列表")

    def _post_timetable(self, values: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            f"{TIMETABLE}?CSRFToken={self.csrf}",
            data=values,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": MAKE_BOOK,
                "Origin": BASE,
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
            allow_redirects=False,
            timeout=TIMETABLE_TIMEOUT,
        )
        if response.status_code in (301, 302, 303, 307, 308) or self._is_login_page(response):
            raise SessionExpired("时间表请求的会话已失效，请重新登录")
        if response.status_code != 200:
            raise TimetableError(
                f"时间表接口 HTTP {response.status_code}",
                http_status=response.status_code,
                retry_after=retry_after_seconds(response.headers.get("Retry-After"))
                if response.status_code in (429, 503) else None,
                response_info=response_diagnostics(response),
            )
        try:
            return response.json()
        except ValueError as exc:
            raise TimetableError("时间表接口返回的不是 JSON") from exc

    def timetable(self, dataset_id: str, activity_id: str, center_id: str,
                  facility_id: str = "", book_type: str = "INDV") -> dict[str, Any]:
        """Fetch the site's complete official window.

        The real page sends an empty searchDate for the full window. A concrete
        searchDate changes the response into a single column, so the UI filters
        the returned official window locally instead of sending a fake date.
        """
        self.ensure_ready()
        tree_context(self.tree, dataset_id, activity_id, center_id, facility_id)
        request_data = {
            "CSRFToken": self.csrf,
            "fbUserId": self.fb_user_id,
            "bookType": book_type,
            "dataSetId": str(dataset_id),
            "actvId": str(activity_id),
            "searchDate": "",
            "ctrId": str(center_id),
            "facilityId": str(facility_id or ""),
            "showCourtAreaDetails": "true",
        }
        try:
            payload = self._post_timetable(request_data)
        except SessionExpired:
            self.login()
            request_data["CSRFToken"] = self.csrf
            payload = self._post_timetable(request_data)
        return normalize_timetable(payload)

    def _selection_context(self, selection: Selection) -> dict[str, Any]:
        self.ensure_ready()
        return tree_context(
            self.tree, selection.dataset_id, selection.activity_id,
            selection.center_id, selection.facility_id,
        )

    def _search_form_string(self, selection: Selection) -> str:
        # This is the exact form serialization used by ui_book_control_indv.js.
        values = [
            ("fbUserId", self.fb_user_id),
            ("bookType", selection.book_type),
            ("dataSetId", selection.dataset_id),
            ("actvId", selection.activity_id),
            ("searchDate", ""),
            ("ctrId", selection.center_id),
            ("facilityId", ""),
        ]
        return urlencode(values)

    @staticmethod
    def _date_time(value: str, hm: str) -> str:
        parsed = parse_date(value)
        if not parsed or not parse_hm(hm):
            raise BookingError(f"目标日期或时间无效: {value} {hm}")
        return f"{parsed.day:02d} {parsed.strftime('%b')} {parsed.year} {parse_hm(hm)}"

    def _first_step_data(self, selection: Selection, facility_id: str) -> dict[str, Any]:
        return {
            "brcdNo": "",
            "phone": "",
            "extlPtyDclrId": "",
            "dataSetId": selection.dataset_id,
            "actvId": selection.activity_id,
            "onBehalfOfFbUserId": "",
            "byPassQuota": "false",
            "byPassChrgSchm": "false",
            "byPassBookingDaysLimit": "false",
            "repeatOccurrence": "false",
            "grpFacilityIds": "",
            "searchFormString": self._search_form_string(selection),
            "boMakeBookFacilities[0].ctrId": selection.center_id,
            "boMakeBookFacilities[0].facilityId": facility_id,
            "boMakeBookFacilities[0].startDateTime": self._date_time(selection.date, selection.start),
            "boMakeBookFacilities[0].endDateTime": self._date_time(selection.date, selection.end),
            "CSRFToken": self.csrf,
        }

    @staticmethod
    def _booking_facility_name(context: dict[str, Any]) -> str:
        """Use the short Area/Court label shown on POSS's confirmation page."""
        label = str((context.get("facility") or {}).get("label") or "")
        return label.rsplit(" - ", 1)[-1].strip()

    def _confirmation_data(self, selection: Selection, facility_id: str,
                           context: dict[str, Any], csrf: str) -> dict[str, str]:
        """Build the exact multipart fields emitted by POSS's browser script."""
        return {
            "dataSetId": selection.dataset_id,
            "boBookingType.id": "1",
            "boBookingType.value": selection.book_type,
            "boBookingMode.value": "SPORT",
            "boBookingMode.id": "1",
            "userRefNum": "",
            "fbUserId": self.fb_user_id,
            "grpFacilityIds": "",
            "repeatOccurrence": "false",
            "startDate": "",
            "startTime": "",
            "endDate": "",
            "endTime": "",
            "dayOfWeeks": "",
            "functionsAvailable": "false",
            "brcdNo": "",
            "phone": "",
            "onBehalfOfFbUserId": "",
            "byPassQuota": "false",
            "byPassChrgSchm": "false",
            "byPassBookingDaysLimit": "false",
            "searchFormString": self._search_form_string(selection),
            "extlPtyDclrId": "",
            "boMakeBookFacilities[0].ctrId": selection.center_id,
            "boMakeBookFacilities[0].centerName": context["center"]["label"],
            "boMakeBookFacilities[0].facilityId": facility_id,
            "boMakeBookFacilities[0].facilityName": self._booking_facility_name(context),
            "boMakeBookFacilities[0].startDateTime": self._date_time(selection.date, selection.start),
            "boMakeBookFacilities[0].endDateTime": self._date_time(selection.date, selection.end),
            "declare": "on",
            "CSRFToken": csrf,
        }

    def preview_booking(self, selection: Selection, table: dict[str, Any] | None = None,
                        excluded_facility_ids: set[str] | frozenset[str] = frozenset()
                        ) -> dict[str, Any]:
        """Validate a target and return the exact request without posting it."""
        context = self._selection_context(selection)
        if table is None:
            table = self.timetable(selection.dataset_id, selection.activity_id, selection.center_id,
                                   selection.facility_id, selection.book_type)
        slot = find_slot(table, selection.date, selection.start, selection.end)
        if not slot:
            raise BookingError("目标时段不在 POSS 当前返回的官方窗口内")
        available = [court for court in slot["availableFacilityIds"]
                     if court not in excluded_facility_ids]
        facility_id = selection.facility_id or (available[0] if available else "")
        if not facility_id:
            raise BookingError("目标时段当前没有可用场地；可以保存目标，但不能立即下单")
        if facility_id not in slot["facilityIds"]:
            raise BookingError("目标 Area 不属于这个时段")
        if context["facility"] is None:
            context = tree_context(
                self.tree, selection.dataset_id, selection.activity_id,
                selection.center_id, facility_id,
            )
        rule = table.get("bookingRule") or {"leadDays": 7, "openTime": "08:30"}
        release_at = booking_release_at(selection.date, rule)
        now = datetime.now(TZ)
        if now < release_at:
            raise BookingError(
                f"POSS 尚未开放该日期：{selection.date} 要到 "
                f"{release_at:%Y-%m-%d %H:%M} 才能抢（POSS 规则：提前 "
                f"{rule['leadDays']} 天，{rule['openTime']}）"
            )
        return {
            "selection": selection.__dict__,
            "facilityId": facility_id,
            "slot": slot,
            "context": {
                "activity": context["activity"]["label"],
                "center": context["center"]["label"],
                "facility": context["facility"]["label"] if context["facility"] else "",
            },
            "bookingRule": rule,
            "releaseAt": release_at.isoformat(),
            "request": {
                "method": "POST",
                "url": MAKE_BOOK,
                "fields": self._first_step_data(selection, facility_id),
            },
        }

    def _parse_confirmation(self, html: str) -> tuple[str, str, dict[str, str], str]:
        parser = _FormParser()
        parser.feed(html or "")
        candidates = []
        for form in parser.forms:
            attrs = form["attrs"]
            action = attrs.get("action", "")
            fields = dict(form["fields"])
            if "boMakeBookFacilities[0].facilityId" in fields or "declare" in fields:
                candidates.append((action, attrs.get("method", "post").upper(), fields,
                                   attrs.get("enctype", "application/x-www-form-urlencoded")))
        if not candidates:
            raise BookingError("第一步没有返回确认表单，未执行最终提交")
        return candidates[-1]

    def _record_contains_booking(self, selection: Selection, center_label: str,
                                 facility_label: str = "") -> bool:
        """Verify a success page against My Record before calling it booked."""
        response = self.session.get(MY_RECORD, timeout=PAGE_TIMEOUT)
        if self._is_login_page(response):
            raise SessionExpired("验证预订记录时会话已失效")
        response.raise_for_status()
        parsed = parse_date(selection.date)
        start = datetime.strptime(selection.start, "%H:%M").strftime("%I:%M %p")
        end = datetime.strptime(selection.end, "%H:%M").strftime("%I:%M %p")
        date_text = f"{parsed.day:02d} {parsed.strftime('%b')} {parsed.year}"
        for match in re.finditer(r"(?is)<tr\b[^>]*>.*?</tr>", response.text):
            row = html_to_text(match.group())
            if all(value.lower() in row.lower() for value in (
                center_label, date_text, f"{start} - {end}",
            )) and (not facility_label or re.search(
                r"(?<![A-Za-z0-9])" + re.escape(facility_label) + r"(?![A-Za-z0-9])",
                row, re.IGNORECASE,
            )):
                # My Record is newest first; an older confirmed row must not
                # override a later cancellation of the same target.
                return "Individual Booking" in row and "(Confirmed)" in row
        return False

    def _reconcile_record(self, selection: Selection, events: list[dict[str, Any]]) -> bool:
        center = selection.center_name
        if not center and self.tree:
            center = tree_context(self.tree, selection.dataset_id, selection.activity_id,
                                  selection.center_id)["center"]["label"]
        for attempt in range(3):
            started = time.monotonic()
            try:
                confirmed = self._record_contains_booking(
                    selection, center, self._attempt_facility_label
                )
                events.append({"stage": "reconcile_record", "confirmed": confirmed,
                               "duration_ms": round((time.monotonic() - started) * 1000)})
                if confirmed:
                    return True
            except (PossError, requests.RequestException) as exc:
                events.append({"stage": "reconcile_record", "error": type(exc).__name__,
                               "duration_ms": round((time.monotonic() - started) * 1000)})
            if attempt < 2:
                time.sleep(attempt + 1)
        return False

    def book(self, selection: Selection, dry_run: bool = False,
             on_final_submit: Callable[[], None] | None = None,
             prepared_facility_id: str = "",
             on_first_step: Callable[[], None] | None = None,
             on_court_selected: Callable[[str], None] | None = None,
             excluded_facility_ids: set[str] | frozenset[str] = frozenset()
             ) -> BookingOutcome:
        """Perform exactly one two-step booking, with no automatic retry after submit."""
        events: list[dict[str, Any]] = []
        self.last_attempt_events = events
        self._attempt_facility_label = ""
        stage = "timetable"

        def record(name: str, start: float, **details: Any):
            events.append({
                "at": datetime.now(TZ).isoformat(timespec="milliseconds"),
                "stage": name,
                "duration_ms": round((time.monotonic() - start) * 1000),
                **details,
            })

        try:
            outcome = self._book_once(
                selection, dry_run, events, record, on_final_submit, prepared_facility_id,
                on_first_step, on_court_selected, excluded_facility_ids,
            )
            if outcome.status == "unknown" and any(
                event.get("stage") == "final_submit" for event in events
            ):
                if outcome.reason_code == "unknown_result":
                    outcome.status = "failed"
                    outcome.message = "结果页无法识别；按失败继续其他目标，未在抢位路径等待 My Record"
                    events.append({"stage": "reconcile_record", "state": "skipped",
                                   "reason": "unknown_result"})
                elif self._reconcile_record(selection, events):
                    outcome.status, outcome.reason_code = "booked", "confirmed"
                    outcome.message, outcome.verified = "My Record 已确认这笔订单", True
            return outcome
        except BookingAttemptError:
            raise
        except (PossError, requests.RequestException) as exc:
            if events:
                stage = events[-1]["stage"]
            reason = (
                "no_vacancy" if isinstance(exc, BookingError) and "没有可用场地" in str(exc)
                else "not_open" if isinstance(exc, BookingError) and "尚未开放" in str(exc)
                else "session_expired" if isinstance(exc, SessionExpired)
                else "login_failed" if isinstance(exc, LoginError)
                else ("rate_limited" if exc.http_status == 429 else "transient_http"
                      if exc.http_status in (408, 500, 502, 503, 504) else "request_rejected")
                if isinstance(exc, TimetableError) else
                transport_reason(exc) if isinstance(exc, requests.RequestException)
                else "request_rejected"
            )
            if stage in ("final_submit", "verify_record"):
                if self._reconcile_record(selection, events):
                    return BookingOutcome(
                        "booked", "My Record 已确认这笔订单", stage=stage,
                        reason_code="confirmed", verified=True, events=events,
                        center=selection.center_name, date=selection.date,
                        start=selection.start, end=selection.end,
                    )
            raise BookingAttemptError(
                (str(exc) if isinstance(exc, PossError)
                 else f"{type(exc).__name__}: POSS 网络请求失败"),
                stage, reason, events,
                uncertain=stage in ("first_step", "final_submit", "verify_record"),
                retry_after=getattr(exc, "retry_after", None),
            ) from exc

    def _book_once(self, selection: Selection, dry_run: bool,
                   events: list[dict[str, Any]], record,
                   on_final_submit: Callable[[], None] | None,
                   prepared_facility_id: str,
                   on_first_step: Callable[[], None] | None,
                   on_court_selected: Callable[[str], None] | None,
                   excluded_facility_ids: set[str] | frozenset[str]) -> BookingOutcome:
        if prepared_facility_id:
            started = time.monotonic()
            try:
                self.ensure_ready()
                context = tree_context(
                    self.tree, selection.dataset_id, selection.activity_id,
                    selection.center_id, prepared_facility_id,
                )
                if context["facility"] is None:
                    raise BookingError("预备球场编号不属于目标场馆")
                preview = {
                    "facilityId": prepared_facility_id,
                    "slot": {"charge": None, "refundableFee": None},
                    "context": {
                        "center": context["center"]["label"],
                        "facility": context["facility"]["label"],
                    },
                    "request": {"fields": self._first_step_data(
                        selection, prepared_facility_id,
                    )},
                }
            except (PossError, requests.RequestException) as exc:
                record("prepared", started, error=type(exc).__name__)
                raise
            record("prepared", started, state="ready")
        else:
            started = time.monotonic()
            try:
                table = self.timetable(selection.dataset_id, selection.activity_id,
                                       selection.center_id, selection.facility_id, selection.book_type)
            except (PossError, requests.RequestException) as exc:
                record("timetable", started, error=type(exc).__name__,
                       http_status=getattr(exc, "http_status", None),
                       retry_after_s=getattr(exc, "retry_after", None),
                       **getattr(exc, "response_info", {}))
                raise
            record("timetable", started, columns=len(table["columns"]))
            started = time.monotonic()
            slot = find_slot(table, selection.date, selection.start, selection.end)
            if slot is None:
                record("availability", started, state="slot_missing")
                raise BookingAttemptError(
                    "目标时段不在 POSS 返回的时间表中，可能日期/中心/活动不匹配",
                    "availability", "slot_missing", events,
                )
            record("availability", started, available=slot["left"],
                   facility_available=selection.facility_id in slot["availableFacilityIds"]
                   if selection.facility_id else bool(slot["availableFacilityIds"]))
            if selection.facility_id and selection.facility_id not in slot["availableFacilityIds"]:
                raise BookingAttemptError(
                    "目标 Area 已被占用或不可用；可能被他人抢先，但无法仅凭时间表断定是速度原因",
                    "availability", "no_vacancy", events,
                )
            available = [court for court in slot["availableFacilityIds"]
                         if court not in excluded_facility_ids]
            if not available:
                raise BookingAttemptError(
                    "该时段没有可用 Area；可能被他人抢先，但无法仅凭时间表断定是速度原因",
                    "availability", "no_vacancy", events,
                )
            started = time.monotonic()
            events.append({"at": datetime.now(TZ).isoformat(timespec="milliseconds"),
                           "stage": "validation", "state": "checking"})
            if excluded_facility_ids:
                preview = self.preview_booking(
                    selection, table, excluded_facility_ids=excluded_facility_ids
                )
            else:
                preview = self.preview_booking(selection, table)
            record("validation", started, state="ok")
        slot = preview["slot"]
        facility_id = preview["facilityId"]
        context = tree_context(
            self.tree, selection.dataset_id, selection.activity_id,
            selection.center_id, facility_id,
        )
        self._attempt_facility_label = self._booking_facility_name(context)
        outcome = BookingOutcome(
            status="preview" if dry_run else "started",
            message="已生成第一步请求，未提交" if dry_run else "正在进入确认页",
            stage="preview" if dry_run else "first_step",
            center=preview["context"]["center"], facility=preview["context"]["facility"],
            date=selection.date, start=selection.start, end=selection.end,
            charge=slot["charge"], refundable_fee=slot["refundableFee"],
            events=events,
        )
        if dry_run:
            outcome.message = "dry-run：第一步请求参数已验证，未发送预订请求"
            return outcome

        if on_court_selected:
            on_court_selected(facility_id)
        started = time.monotonic()
        events.append({"at": datetime.now(TZ).isoformat(timespec="milliseconds"),
                       "stage": "first_step", "state": "sending"})
        if on_first_step:
            on_first_step()
        try:
            first = self.session.post(
                MAKE_BOOK,
                data=preview["request"]["fields"],
                headers={"Referer": MAKE_BOOK, "Origin": BASE},
                allow_redirects=False,
                timeout=CONFIRMATION_TIMEOUT,
            )
        except requests.RequestException as exc:
            record("first_step", started, error=type(exc).__name__)
            raise
        record("first_step", started, http_status=first.status_code,
               redirected=first.status_code in (301, 302, 303, 307, 308),
               **response_diagnostics(first))
        if self._is_login_page(first):
            raise SessionExpired("第一步预订被重定向到登录页")
        if first.status_code in (301, 302, 303, 307, 308):
            location = first.headers.get("Location")
            if not location:
                raise BookingError("第一步预订返回了没有 Location 的重定向")
            first = self.session.get(urljoin(BASE, location),
                                     headers={"Referer": MAKE_BOOK}, timeout=CONFIRMATION_TIMEOUT)
        if first.status_code != 200:
            raise BookingError(f"第一步预订 HTTP {first.status_code}，未执行最终提交")

        started = time.monotonic()
        events.append({"at": datetime.now(TZ).isoformat(timespec="milliseconds"),
                       "stage": "confirmation_page", "state": "parsing"})
        try:
            action, method, confirmation_fields, enctype = self._parse_confirmation(first.text)
        except BookingError as exc:
            record("confirmation_page", started, error=type(exc).__name__)
            if any(word in html_to_text(first.text).lower() for word in (
                "already been booked", "not available", "cannot be booked",
                "名額已滿", "額滿", "已被",
            )):
                raise BookingAttemptError(
                    "POSS 明确拒绝了这块球场，未发送最终提交",
                    "confirmation_page", "slot_taken", events,
                ) from exc
            raise
        record("confirmation_page", started, state="ok",
               csrf_present=bool(confirmation_fields.get("CSRFToken")))
        outcome.stage = "confirmation_page"
        confirmation_context = tree_context(
            self.tree, selection.dataset_id, selection.activity_id,
            selection.center_id, facility_id,
        )
        csrf = confirmation_fields.get("CSRFToken") or self.csrf
        fields = self._confirmation_data(
            selection, facility_id, confirmation_context, csrf
        )
        submit_url = urljoin(MAKE_BOOK, action or "make_book_submit.do")
        if method != "POST":
            raise BookingError(f"确认表单方法异常: {method}")
        if on_final_submit:
            on_final_submit()
        started = time.monotonic()
        events.append({"at": datetime.now(TZ).isoformat(timespec="milliseconds"),
                       "stage": "final_submit", "state": "sending"})
        try:
            if "multipart/form-data" in enctype:
                files = {key: (None, value) for key, value in fields.items()}
                final = self.session.post(submit_url, files=files,
                                          headers={"Referer": first.url, "Origin": BASE},
                                          allow_redirects=False, timeout=FINAL_TIMEOUT)
            else:
                final = self.session.post(submit_url, data=fields,
                                          headers={"Referer": first.url, "Origin": BASE},
                                          allow_redirects=False, timeout=FINAL_TIMEOUT)
        except requests.RequestException as exc:
            record("final_submit", started, error=type(exc).__name__)
            raise
        record("final_submit", started, http_status=final.status_code,
               redirected=final.status_code in (301, 302, 303, 307, 308),
               **response_diagnostics(final))
        if self._is_login_page(final):
            raise SessionExpired("最终提交被重定向到登录页")
        outcome.stage = "final_submit"
        result = final
        if final.status_code in (301, 302, 303, 307, 308):
            location = final.headers.get("Location")
            if not location:
                return BookingOutcome(
                    status="unknown",
                    message="最终提交返回无目标地址的重定向，请人工确认",
                    stage=outcome.stage,
                    center=outcome.center,
                    facility=outcome.facility,
                    date=outcome.date,
                    start=outcome.start,
                    end=outcome.end,
                    charge=outcome.charge,
                    refundable_fee=outcome.refundable_fee,
                    reason_code="missing_result_redirect",
                    events=events,
                )
            result = self.session.get(urljoin(submit_url, location),
                                      headers={"Referer": submit_url}, timeout=PAGE_TIMEOUT)
        if result is not final:
            events.append({"at": datetime.now(TZ).isoformat(timespec="milliseconds"),
                           "stage": "result_page", "http_status": result.status_code,
                           **response_diagnostics(result)})
        text = result.text or ""
        visible = html_to_text(text)
        lower = visible.lower()
        failure_words = ("unsuccessful", "not successful", "not available", "already been",
                         "cannot be booked", "失败", "額滿", "名額已滿", "已被")
        success_words = ("successful", "booking successful", "confirmed", "成功", "已確認")
        if result.status_code != 200:
            status = "unknown"
            message = f"最终提交结果页 HTTP {result.status_code}，需核对 My Record"
            reason_code = "result_http_error"
        elif any(word.lower() in lower for word in failure_words):
            status = "failed"
            matched = [word for word in failure_words if word.lower() in lower]
            message = f"POSS 明确拒绝了这次预订（匹配：{', '.join(matched[:2])}）"
            reason_code = ("slot_taken" if any(word in lower for word in (
                "already been", "not available", "cannot be booked",
                "額滿", "名額已滿", "已被",
            )) else "server_rejected")
        elif ("successfully" in lower and "booking" in lower) or any(
            word.lower() in lower for word in success_words
        ):
            # POSS can render a generic success shell with an empty transaction
            # when required booking fields are missing. Verify My Record before
            # reporting a paid booking to the caller.
            started = time.monotonic()
            events.append({"at": datetime.now(TZ).isoformat(timespec="milliseconds"),
                           "stage": "verify_record", "state": "checking"})
            try:
                verified = self._record_contains_booking(
                    selection, outcome.center, self._attempt_facility_label
                )
            except (PossError, requests.RequestException) as exc:
                record("verify_record", started, error=type(exc).__name__)
                raise
            record("verify_record", started, confirmed=verified)
            status = "booked" if verified else "unknown"
            message = ("预订成功，且已在 POSS My Record 中核实"
                       if verified else
                       "POSS 返回成功提示，但 My Record 没有这笔订单；不认为已下单")
            reason_code = "confirmed" if verified else "unverified_result"
        else:
            status = "unknown"
            message = "最终结果无法自动判定，请立即打开 POSS 确认，程序不会重试"
            reason_code = "unknown_result"
        return BookingOutcome(
            status=status,
            message=message,
            stage=outcome.stage,
            reason_code=reason_code,
            center=outcome.center,
            facility=outcome.facility,
            date=outcome.date,
            start=outcome.start,
            end=outcome.end,
            charge=outcome.charge,
            refundable_fee=outcome.refundable_fee,
            http_status=result.status_code,
            response_url=result.url,
            result_html=text[:10000],
            verified=verified if "verified" in locals() else False,
            events=events,
        )


def load_env(path: str = ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def account_ids_from_env(path: str = ".env") -> list[str]:
    values = {**os.environ, **load_env(path)}
    ids = ["1"] if values.get("POSS_NETID") and values.get("POSS_PASSWORD") else []
    for key in values:
        match = re.fullmatch(r"POSS_NETID_(\d+)", key)
        if match and int(match.group(1)) >= 2 and values.get(key) and values.get(f"POSS_PASSWORD_{match.group(1)}"):
            ids.append(match.group(1))
    ids = sorted(set(ids), key=int)
    netids = [values["POSS_NETID" if slot == "1" else f"POSS_NETID_{slot}"] for slot in ids]
    if len(netids) != len(set(netids)):
        raise LoginError("多个账号不能使用相同的 NetID")
    return ids


def client_from_env(path: str = ".env", account_id: str = "1") -> PossClient:
    values = {**os.environ, **load_env(path)}
    suffix = "" if account_id == "1" else f"_{account_id}"
    netid = values.get(f"POSS_NETID{suffix}", "")
    password = values.get(f"POSS_PASSWORD{suffix}", "")
    fb_user_id = values.get(f"POSS_FB_USER_ID{suffix}", "657032")
    return PossClient(netid, password, fb_user_id)
