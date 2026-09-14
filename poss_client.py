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
import time

import requests

BASE = "https://www40.polyu.edu.hk"
LOGIN_PAGE = f"{BASE}/poss/secure/login/loginhome.do"
LOGIN_POST = f"{BASE}/poss/j_security_check"
MAKE_BOOK = f"{BASE}/starspossfbstud/secure/ui_make_book/make_book.do"
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


class LoginFailed(Exception):
    """账号密码被拒 / 触发额外验证。"""


class SessionExpired(Exception):
    """会话失效（302 / 登录页），需要重新登录。"""


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
