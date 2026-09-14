#!/usr/bin/env python3
"""
cURL 导入器：把浏览器「Copy as cURL」的原始请求翻译成可复用的模板
================================================================
它做三件事：
  ① 解析 cURL（URL / 方法 / 请求头 / 请求体，表单或 JSON 都认）
  ② 自动把「每次都会变」的值换成占位符（日期时间、场馆、设施、CSRF、slotId）
  ③ 打印逐字段审查表，并写出 booking.curl 供 monitor.py 直接使用

设计要点（别改错，会扣押金）：
  · 请求体**不做解码→重编码**。像 searchFormString 这种字段，其值内部本身含
    & 和 = ，一旦解码再重新解析就会被切成多个字段，请求结构直接变形。
    这里只做纯文本替换，浏览器原来的编码字节原样保留。
  · 需要 URL 编码的占位符会自动写成 {key:url}（例如 "18 Sep 2026 14:30"
    在原文里是 18+Sep+2026+14%3A30）。
  · 判定日期/时间一律用严格正则，不用 strptime —— strptime 会把 "18" 当成
    01:08，把 facilityId=8891 这种编号误伤成时间。

用法：
  python3 calibrate.py --import booking_raw.txt
  pbpaste | python3 calibrate.py --import -        # macOS：直接读剪贴板
  python3 calibrate.py --show                      # 查看已导入模板的概要

支持的占位符（monitor 自动填充）：
  {csrf} {fbUserId} {ctrId} {facilityId} {gym} {date} {startTime} {endTime}
  {startDateTime} {endDateTime} {searchDate} {slotId}
  写法：{date} / {date:%d/%m/%Y} / {date:url} / {startDateTime:url:%d %b %Y %H:%M}
  另外 timetable 返回的原始字段也会按同名占位符注入（如 {courtId}）。
"""

import argparse
import json
import os
import re
import sys
from urllib.parse import parse_qsl, quote_plus, unquote, unquote_plus

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from poss_client import (  # noqa: E402
    DROP_HEADERS,
    parse_curl,
    to_curl,
)

DEFAULT_OUT = os.environ.get("BOOKING_CURL_FILE", "booking.curl")

# 字段名 → 占位符（按优先级从上到下匹配）
KEY_RULES = [
    (re.compile(r"csrf", re.I), "{csrf}", "会话令牌"),
    (re.compile(r"fbuser", re.I), "{fbUserId}", "用户 ID"),
    (re.compile(r"^ctr|ctrid|cent(er|re)id", re.I), "{ctrId}", "场馆编号"),
    (re.compile(r"facility(id)?$", re.I), "{facilityId}", "设施编号"),
    (re.compile(r"slot", re.I), "{slotId}", "时段编号"),
]
START_HINT = re.compile(r"start|begin|^from$|timefrom|bookfrom", re.I)
END_HINT = re.compile(r"end|finish|^to$|timeto|bookto", re.I)
DATE_HINT = re.compile(r"date|day", re.I)
# 字段名像编号 → 值再像时间/日期也不动它（facilityId=8891 不能被当成时间）
ID_HINT = re.compile(r"id$|^id|no$|num|code$", re.I)

# ⚠️ 严格正则判定，不用 strptime
_DATE_PATTERNS = [
    (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
    (r"\d{4}/\d{2}/\d{2}", "%Y/%m/%d"),
    (r"\d{2}/\d{2}/\d{4}", "%d/%m/%Y"),
    (r"\d{2}-\d{2}-\d{4}", "%d-%m-%Y"),
    (r"\d{8}", "%Y%m%d"),
    (r"\d{2}/\d{2}/\d{2}", "%d/%m/%y"),
]
_TIME_PATTERNS = [
    (r"\d{1,2}:\d{2}:\d{2}", "%H:%M:%S"),
    (r"\d{1,2}:\d{2}", "%H:%M"),
    (r"\d{4}", "%H%M"),
]
# POSS 的 startDateTime 形如「18 Sep 2026 14:30」——日期时间复合值
_DATETIME_PATTERNS = [
    (r"\d{1,2} [A-Za-z]{3} \d{4} \d{1,2}:\d{2}", "%d %b %Y %H:%M"),
    (r"\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}", "%Y-%m-%d %H:%M"),
    (r"\d{4}/\d{2}/\d{2} \d{1,2}:\d{2}", "%Y/%m/%d %H:%M"),
    (r"\d{2}/\d{2}/\d{4} \d{1,2}:\d{2}", "%d/%m/%Y %H:%M"),
]

SECRET_KEYS = re.compile(r"token|password|secret|sessionid|authorization", re.I)
WARN = "⚠️"


# ==================== 值 → 占位符 ====================


def _match_format(value: str, patterns):
    """严格正则匹配，返回 strftime 模板；不匹配返回 False。"""
    s = value.strip()
    for pattern, fmt in patterns:
        if re.fullmatch(pattern, s):
            return fmt
    return False


def _fmt_suffix(placeholder: str, fmt) -> str:
    return placeholder if fmt is None else "{%s:%s}" % (placeholder[1:-1], fmt)


def _add_url_spec(placeholder: str) -> str:
    """{x} → {x:url}；{x:%H%M} → {x:url:%H%M}"""
    name, _, fmt = placeholder.strip("{}").partition(":")
    return "{%s:url%s}" % (name, ":" + fmt if fmt else "")


def _looks_like_query(value: str) -> bool:
    """值本身是不是一串查询参数（如 searchFormString）。"""
    return "=" in value and "&" in value


def template_value(key: str, value: str):
    """把一个字段值换成占位符。返回 (新值, 说明)；说明以 ⚠️ 开头表示未识别。

    判定顺序很重要：**先看值像不像日期/时间，再看字段名**。
    POSS 的 boMakeBookFacilities[0].startDateTime 这种字段名里带 "Facilities"，
    若先按字段名匹配就会被误判成 {facilityId}，请求必然失败。
    """
    if not isinstance(value, str) or not value.strip():
        return value, ""

    looks_like_id = bool(ID_HINT.search(key))
    if not looks_like_id:
        dtfmt = _match_format(value, _DATETIME_PATTERNS)
        if dtfmt is not False:
            if START_HINT.search(key):
                return _fmt_suffix("{startDateTime}", dtfmt), "开始日期时间"
            if END_HINT.search(key):
                return _fmt_suffix("{endDateTime}", dtfmt), "结束日期时间"
            return value, f"{WARN} 日期时间值但字段名看不出起止，未识别（请人工确认）"

        dfmt = _match_format(value, _DATE_PATTERNS)
        if dfmt is not False:
            return _fmt_suffix("{date}", None if dfmt == "%Y-%m-%d" else dfmt), "预订日期"

        tfmt = _match_format(value, _TIME_PATTERNS)
        if tfmt is not False:
            if START_HINT.search(key):
                return _fmt_suffix("{startTime}", None if tfmt == "%H:%M" else tfmt), "开始时间"
            if END_HINT.search(key):
                return _fmt_suffix("{endTime}", None if tfmt == "%H:%M" else tfmt), "结束时间"
            # 4 位纯数字太像编号（如 slotId=1630），不硬猜
            if tfmt != "%H%M":
                return value, f"{WARN} 值像时间但字段名看不出起止，未识别（请人工确认）"

    for pat, ph, label in KEY_RULES:
        if pat.search(key):
            return ph, label
    return value, ""


# ==================== 请求体 / URL 处理 ====================


def _restore_braces(text: str) -> str:
    """quote_plus 会把 {} 编码成 %7B%7D，这里把它们还原成占位符字面量。

    必须用 unquote_plus：格式后缀里的空格会被编码成 '+'，用 unquote 会留下裸 +，
    占位符写成 {date:%d+%b+%Y} 就再也渲染不出来了。
    """
    return re.sub(r"%7B(.*?)%7D", lambda m: "{" + unquote_plus(m.group(1)) + "}",
                  text, flags=re.I)


def template_encoded_query(enc_query: str, src: str, notes: list | None = None):
    """处理「已编码」的查询串，返回 (新串, 记录表)。

    在编码文本上按 & 和 = 切分是安全的：编码后的值里不会出现裸的 & 或 =。
    未被识别的字段原字节保留，不会引入任何编码差异。
    """
    notes = [] if notes is None else notes
    out = []
    for pair in enc_query.split("&"):
        if "=" not in pair:
            out.append(pair)
            continue
        k_enc, _, v_enc = pair.partition("=")
        # 值要用 unquote_plus：form 编码里空格是 '+'，unquote 解不出来
        k, v = unquote_plus(k_enc), unquote_plus(v_enc)
        if not v.strip():
            out.append(pair)
            continue

        if _looks_like_query(v):  # 值里还嵌着一串参数 → 递归处理
            nested, _ = template_encoded_query(v, src, notes)
            out.append(f"{k_enc}={_restore_braces(quote_plus(nested))}")
            continue

        nv, note = template_value(k, v)
        if note.startswith(WARN):
            notes.append({"src": src, "k": k, "v": v, "nv": v, "note": note})
            out.append(pair)
            continue
        if note:
            if v_enc != v:  # 原值在文中是被编码过的 → 替换值也必须编码
                nv = _add_url_spec(nv)
            notes.append({"src": src, "k": k, "v": v, "nv": nv, "note": note})
            out.append(f"{k_enc}={nv}")
            continue
        out.append(pair)
    return "&".join(out), notes


def template_json(node, prefix="", notes=None):
    """递归处理 JSON body（JSON 不做 URL 编码，占位符直接替换）。"""
    if notes is None:
        notes = []
    if isinstance(node, dict):
        return {k: template_json(v, k, notes) for k, v in node.items()}, notes
    if isinstance(node, list):
        return [template_json(v, prefix, notes) for v in node], notes
    if isinstance(node, str):
        nv, note = template_value(prefix, node)
        if note:
            notes.append({"src": "body", "k": prefix, "v": node, "nv": nv, "note": note})
        return nv, notes
    return node, notes


def template_body(body: str, content_type: str):
    body = body or ""
    if not body:
        return body, [], "empty"
    if "json" in (content_type or "").lower() or body.lstrip()[:1] in ("{", "["):
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            return body, [], "raw"
        new, notes = template_json(obj)
        return json.dumps(new, ensure_ascii=False, separators=(",", ":")), notes, "json"
    new, notes = template_encoded_query(body, "body")
    return new, notes, "form"


def import_curl(curl_text: str) -> dict:
    parsed = parse_curl(curl_text)
    url = parsed["url"]
    base, _, query = url.partition("?")
    new_query, url_notes = template_encoded_query(query, "url")
    new_url = base + ("?" + new_query if new_query else "")

    body, body_notes, mode = template_body(parsed["data"], parsed["content_type"])

    headers = parsed["headers"]
    kept = {k: v for k, v in headers.items() if k.lower() not in DROP_HEADERS}
    dropped = {k: v for k, v in headers.items() if k.lower() in DROP_HEADERS}

    return {
        "method": parsed["method"],
        "url": new_url,
        "raw_url": url,
        "headers": kept,
        "dropped_headers": dropped,
        "data": body,
        "raw_data": parsed["data"],
        "mode": mode,
        "notes": url_notes + body_notes,
        "content_type": parsed["content_type"],
    }


# ==================== 展示 ====================


def _short(s, n=46):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _unknown_fields(raw_data: str, mode: str):
    """找出「疑似该模板化但没识别」的字段。"""
    if mode != "form" or not raw_data:
        return []
    out = []
    for k, v in parse_qsl(raw_data, keep_blank_values=True):
        if not v.strip():
            continue
        nv, note = template_value(k, v)
        if note:
            continue
        if SECRET_KEYS.search(k):
            out.append((k, v, "字段名像凭据，建议改成 {csrf} 或确认无需动态生成"))
    return out


def print_report(info: dict, out_path: str):
    W = 74
    print("=" * W)
    print("cURL 导入报告")
    print("=" * W)
    print(f"请求方法：{info['method']}")
    print(f"请求地址：{_short(info['url'], W - 6)}")
    print(f"请求体类型：{info['mode']}（{info['content_type'] or '未声明'}）")

    url_notes = [n for n in info["notes"] if n["src"] == "url"]
    body_notes = [n for n in info["notes"] if n["src"] == "body"]
    for title, notes in (("URL 查询参数", url_notes), ("请求体字段", body_notes)):
        if not notes:
            continue
        print(f"\n【{title}】")
        for n in notes:
            print(f"  {_short(n['k'], 38):<40}{_short(n['v'], 22):<24}→ {n['nv']}")
            if n["note"].startswith(WARN):
                print(f"  {'':<64}{n['note']}")
            else:
                print(f"  {'':<64}({n['note']})")

    if info["dropped_headers"]:
        print("\n【已丢弃的请求头】（脚本自己维护会话，不沿用你的登录凭据）")
        for k, v in info["dropped_headers"].items():
            extra = "  ← 含你的登录凭据，切勿外传" if k.lower() == "cookie" else ""
            print(f"  {k}: {_short(v, 30)}{extra}")

    unknown = _unknown_fields(info["raw_data"], info["mode"])
    if unknown:
        print(f"\n【{WARN} 需要人工确认的字段】")
        for k, v, why in unknown:
            print(f"  {k} = {_short(v, 24)}  → {why}")

    ph = sorted(set(re.findall(r"\{([A-Za-z_][\w-]*)[:}]", info["url"] + "&" + info["data"])))
    print(f"\n共 {len(info['notes'])} 个字段被模板化，占位符："
          f"{', '.join('{' + p + '}' for p in ph) or '(无 —— 静态请求)'}")
    print(f"已写入：{out_path}")
    print("下一步：python3 monitor.py --dry-book   # 核对脚本实际会提交什么")


def show_template(path: str) -> int:
    if not os.path.exists(path):
        print(f"尚未导入：{path} 不存在。先跑 `python3 calibrate.py --import <文件>`")
        return 1
    with open(path, encoding="utf-8") as f:
        text = f.read()
    info = import_curl(text)
    print("=" * 74)
    print(f"当前模板：{path}")
    print("=" * 74)
    print(f"方法：{info['method']}")
    print(f"地址：{info['url']}")
    print(f"头部：{', '.join(info['headers']) or '(无)'}")
    print("请求体：")
    for pair in (info["data"] or "(空)").split("&"):
        print(f"       {pair}")
    ph = sorted(set(re.findall(r"\{([A-Za-z_][\w-]*)[:}]", info["url"] + "&" + info["data"])))
    print(f"\n占位符：{', '.join('{' + p + '}' for p in ph) or '(无 —— 静态请求)'}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="cURL → booking.curl 模板导入器")
    parser.add_argument("--import", dest="src", help="cURL 文本文件；传 - 读 stdin")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"输出文件（默认 {DEFAULT_OUT}）")
    parser.add_argument("--show", action="store_true", help="显示已导入模板的概要")
    args = parser.parse_args()

    if args.show:
        return show_template(args.out)

    if not args.src:
        parser.print_help()
        return 1

    if args.src == "-":
        curl_text = sys.stdin.read()
    else:
        if not os.path.exists(args.src):
            print(f"找不到文件：{args.src}")
            return 1
        with open(args.src, encoding="utf-8") as f:
            curl_text = f.read()

    if not curl_text.strip():
        print("输入是空的 —— 是不是忘了复制 cURL？")
        return 1

    try:
        info = import_curl(curl_text)
    except ValueError as e:
        print(f"解析失败：{e}")
        return 1

    rendered = to_curl(info["method"], info["url"], info["headers"], info["data"])
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(rendered + "\n")

    print_report(info, args.out)
    print("\n--- booking.curl ---")
    print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
