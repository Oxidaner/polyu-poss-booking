#!/usr/bin/env python3
"""Fresh local POSS console.

The UI deliberately mirrors the real POSS data model:
  Facility/Category -> Activity -> Center -> Area
and renders the official ``timeSlotColumns`` response directly.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import monitor
import requests
from poss_client import (
    BookingError,
    LoginError,
    PossError,
    PossClient,
    Selection,
    SessionExpired,
    client_from_env,
    iso_date,
    load_env,
    transport_reason,
)

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("POSS_CONFIG_FILE", os.path.join(ROOT, "config.json"))
TZ = timezone(timedelta(hours=8))

_lock = threading.RLock()
_runner_lock = threading.Lock()
_account_verify_lock = threading.Lock()
_account_check_lock = threading.Lock()
_account_checks: dict[str, dict] = {}
_client: PossClient | None = None
RUNNER_PID_FILE = os.path.join(ROOT, "dumps", "booking-local.pid")
RUNNER_LOG_FILE = os.path.join(ROOT, "dumps", "booking-local.log")

DEFAULT_CONFIG = {
    "booking": {
        "enabled": True,
        "lead_days": 7,
        "trigger_time": "08:30",
        "burst_seconds": 120,
        "retry_interval": 3,
        "targets": [],
    },
    "monitor": {"enabled": True},
}


def deep_merge(base, patch):
    result = dict(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict:
    raw = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as handle:
            raw = json.load(handle)
    return deep_merge(DEFAULT_CONFIG, raw)


def save_config(config: dict):
    temp = CONFIG_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp, CONFIG_FILE)


def _git(args: list[str], timeout: int = 30) -> str:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise PossError(f"Git {' '.join(args)} 失败：{detail or '未知错误'}")
    return result.stdout.strip()


def _masked_remote(url: str) -> str:
    value = str(url or "").strip()
    value = re.sub(r"(?<=://)[^/@]+@", "", value)
    value = re.sub(r"//[^/]+:[^/@]+@", "//", value)
    return value


def github_status() -> dict:
    try:
        remote = _git(["remote", "get-url", "origin"])
    except PossError:
        remote = ""
    branch = _git(["branch", "--show-current"])
    config_dirty = bool(_git(["status", "--porcelain", "--", "config.json"]))
    other_dirty = bool(_git(["status", "--porcelain", "--", ".", ":!config.json"]))
    return {
        "remote": _masked_remote(remote),
        "remoteConfigured": bool(remote),
        "branch": branch,
        "configDirty": config_dirty,
        "otherDirty": other_dirty,
        "ready": bool(remote and branch),
    }


def set_github_remote(url: str) -> dict:
    value = str(url or "").strip()
    if not value or not (
        value.startswith("https://") or value.startswith("ssh://") or value.startswith("git@")
    ):
        raise PossError("GitHub 地址应是 HTTPS 或 SSH remote，例如 git@github.com:user/repo.git")
    try:
        _git(["remote", "get-url", "origin"])
    except PossError:
        _git(["remote", "add", "origin", value])
    else:
        _git(["remote", "set-url", "origin", value])
    return github_status()


def push_config_to_github() -> dict:
    status = github_status()
    if not status["remoteConfigured"]:
        raise PossError("还没有配置 origin，请先填写 GitHub 仓库地址")
    if not status["branch"]:
        raise PossError("当前处于 detached HEAD，无法自动推送")
    if not status["configDirty"]:
        return {**status, "pushed": False, "message": "config.json 没有未上传的修改"}

    _git(["add", "--", "config.json"])
    _git(["commit", "--only", "config.json", "-m", "chore(config): update POSS targets"])
    _git(["push", "origin", status["branch"]], timeout=90)
    return {
        **github_status(),
        "pushed": True,
        "message": f"已推送 config.json 到 origin/{status['branch']}",
    }


def get_client(force_login=False) -> PossClient:
    global _client
    with _lock:
        if force_login or _client is None:
            _client = client_from_env(os.path.join(ROOT, ".env"))
            _client.login()
        else:
            _client.ensure_ready()
        return _client


def account_status():
    values = load_env(os.path.join(ROOT, ".env"))
    ids = ["1", *sorted(
        (key.removeprefix("POSS_NETID_") for key in values
         if re.fullmatch(r"POSS_NETID_[2-9]\d*", key)), key=int,
    )]
    with _account_check_lock:
        checks = {slot: dict(_account_checks.get(slot, {"status": "unchecked"})) for slot in ids}
    accounts = [
        {"id": slot, "netid": values.get("POSS_NETID" if slot == "1" else f"POSS_NETID_{slot}", ""),
         "passwordSet": bool(values.get("POSS_PASSWORD" if slot == "1"
                                        else f"POSS_PASSWORD_{slot}")),
         "check": checks[slot]}
        for slot in ids
    ]
    return {
        "ready": all(row["netid"] and row["passwordSet"] for row in accounts),
        "netid": accounts[0]["netid"],
        "passwordSet": accounts[0]["passwordSet"],
        "accounts": accounts,
        "envFile": os.path.join(ROOT, ".env"),
    }


def verify_accounts() -> dict:
    """Check every configured login and read its deposit in a separate session."""
    accounts = account_status()["accounts"]

    def checked_at():
        return datetime.now(TZ).isoformat(timespec="seconds")

    def verify(row):
        if not row["netid"] or not row["passwordSet"]:
            return {"status": "not_configured", "checkedAt": checked_at()}
        try:
            client = client_from_env(os.path.join(ROOT, ".env"), account_id=row["id"])
            client.login()
        except (PossError, requests.RequestException) as exc:
            reason = (transport_reason(exc) if isinstance(exc, requests.RequestException)
                      else "login_rejected" if isinstance(exc, LoginError)
                      else "session_expired" if isinstance(exc, SessionExpired)
                      else "booking_page_error")
            return {"status": "login_failed", "reasonCode": reason, "checkedAt": checked_at()}
        except Exception:
            return {"status": "login_failed", "reasonCode": "verification_error",
                    "checkedAt": checked_at()}
        try:
            balance = client.deposit_balance()
        except (PossError, requests.RequestException) as exc:
            reason = (transport_reason(exc) if isinstance(exc, requests.RequestException)
                      else type(exc).__name__)
            return {"status": "balance_error", "reasonCode": reason, "checkedAt": checked_at()}
        except Exception:
            return {"status": "balance_error", "reasonCode": "verification_error",
                    "checkedAt": checked_at()}
        if balance is None:
            return {"status": "balance_unknown", "reasonCode": "balance_not_found",
                    "checkedAt": checked_at()}
        return {"status": "verified", "balance": balance, "checkedAt": checked_at()}

    with ThreadPoolExecutor(max_workers=min(4, len(accounts))) as executor:
        checks = list(executor.map(verify, accounts))
    with _account_check_lock:
        _account_checks.clear()
        _account_checks.update(zip((row["id"] for row in accounts), checks))
    return account_status()


def client_for_target(target: dict) -> PossClient:
    account_id = str(target.get("accountId") or "1")
    configured = {row["id"] for row in account_status()["accounts"]
                  if row["netid"] and row["passwordSet"]}
    if account_id not in configured:
        raise LoginError(f"目标指定的账号 {account_id} 尚未配置")
    if account_id == "1":
        return get_client()
    client = client_from_env(os.path.join(ROOT, ".env"), account_id=account_id)
    client.login()
    return client


def write_env(payload: dict):
    path = os.path.join(ROOT, ".env")
    current = load_env(path)
    if "accounts" in payload:
        rows = payload["accounts"]
        if not isinstance(rows, list) or not rows:
            raise LoginError("请至少配置一个 POSS 账号")
        updated = dict(current)
        for key in list(updated):
            if key in ("POSS_NETID", "POSS_PASSWORD") or re.fullmatch(
                r"POSS_(NETID|PASSWORD|FB_USER_ID)_[2-9]\d*", key
            ):
                updated.pop(key)
        ids, netids = set(), set()
        for row in rows:
            slot = str(row.get("id", ""))
            if not re.fullmatch(r"[1-9]\d*", slot) or slot in ids:
                raise LoginError("账号编号无效或重复")
            ids.add(slot)
            suffix = "" if slot == "1" else f"_{slot}"
            netid = str(row.get("netid") or "").strip()
            password = str(row.get("password") or "")
            if not password and current.get(f"POSS_NETID{suffix}") == netid:
                password = current.get(f"POSS_PASSWORD{suffix}", "")
            if not netid or not password or netid in netids or "\n" in netid + password:
                raise LoginError("每个账号需要唯一的 NetID 和密码；更改 NetID 时须填写新密码")
            netids.add(netid)
            updated[f"POSS_NETID{suffix}"] = netid
            updated[f"POSS_PASSWORD{suffix}"] = password
        if "1" not in ids:
            raise LoginError("请保留账号 1")
        current = updated
    else:
        if payload.get("netid") is not None:
            current["POSS_NETID"] = str(payload["netid"]).strip()
        if payload.get("password"):
            current["POSS_PASSWORD"] = str(payload["password"])
    if payload.get("fbUserId"):
        current["POSS_FB_USER_ID"] = str(payload["fbUserId"]).strip()
    if not current.get("POSS_NETID") or not current.get("POSS_PASSWORD"):
        raise LoginError("NetID 和 NetPassword 不能为空")
    account_ids_from_env_path = {**os.environ, **current}
    ids = [key for key in account_ids_from_env_path
           if re.fullmatch(r"POSS_NETID_[2-9]\d*", key)]
    if any(not account_ids_from_env_path.get(key.replace("NETID", "PASSWORD")) for key in ids):
        raise LoginError("所有账号都需要密码")
    directory = os.path.dirname(path)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                     prefix=".poss-env-", delete=False) as handle:
        os.chmod(handle.name, 0o600)
        for key, value in current.items():
            handle.write(f"{key}={value}\n")
        temporary = handle.name
    try:
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def selection_from_payload(payload: dict) -> Selection:
    return Selection(
        dataset_id=str(payload.get("datasetId") or payload.get("dataSetId") or ""),
        activity_id=str(payload.get("activityId") or payload.get("actvId") or ""),
        center_id=str(payload.get("centerId") or payload.get("ctrId") or ""),
        facility_id=str(payload.get("facilityId") or ""),
        date=iso_date(payload.get("date")),
        start=str(payload.get("start") or payload.get("from") or ""),
        end=str(payload.get("end") or payload.get("to") or ""),
        activity_name=str(payload.get("activityName") or payload.get("activity") or ""),
        center_name=str(payload.get("centerName") or payload.get("center") or payload.get("name") or ""),
        facility_name=str(payload.get("facilityName") or ""),
        book_type=str(payload.get("bookType") or "INDV"),
    )


def target_payload(selection: Selection) -> dict:
    return {
        "datasetId": selection.dataset_id,
        "activityId": selection.activity_id,
        "centerId": selection.center_id,
        "facilityId": selection.facility_id,
        "date": selection.date,
        "from": selection.start,
        "to": selection.end,
        "activity": selection.activity_name,
        "center": selection.center_name,
        "facility": selection.facility_name,
        "bookType": selection.book_type,
    }


def validated_targets(values) -> list[dict]:
    if not isinstance(values, list):
        raise PossError("抢位目标必须是列表")
    seen = set()
    targets = []
    for raw in values:
        if not isinstance(raw, dict):
            raise PossError("每个抢位目标都需要日期、时段和场馆")
        try:
            selection = selection_from_payload(raw)
        except ValueError as exc:
            raise PossError("抢位目标日期无效") from exc
        if not all((selection.dataset_id, selection.activity_id, selection.center_id,
                    selection.start, selection.end)):
            raise PossError("每个抢位目标都需要活动、场馆、日期和完整时段")
        try:
            start_time = datetime.strptime(selection.start, "%H:%M").time()
            end_time = datetime.strptime(selection.end, "%H:%M").time()
        except ValueError as exc:
            raise PossError("抢位目标时段无效") from exc
        if start_time >= end_time:
            raise PossError("抢位目标时段无效")
        identity = (selection.dataset_id, selection.activity_id, selection.center_id,
                    selection.facility_id, selection.date, selection.start,
                    selection.end, selection.book_type)
        account_id = str(raw.get("accountId") if raw.get("accountId") is not None else "")
        if account_id and not re.fullmatch(r"[1-9]\d*", account_id):
            raise PossError("指定账号编号无效")
        if identity not in seen:
            target = target_payload(selection)
            if account_id:
                target["accountId"] = account_id
            targets.append(target)
            seen.add(identity)
    return targets


def release_at(target: dict, config: dict) -> str:
    day = datetime.strptime(target["date"], "%Y-%m-%d") - timedelta(
        days=int(config["booking"].get("lead_days", 7))
    )
    return f"{day:%Y-%m-%d} {config['booking'].get('trigger_time', '08:30')}"


def runner_status() -> dict:
    try:
        with open(RUNNER_PID_FILE, encoding="utf-8") as handle:
            pid = int(handle.read().strip())
        os.kill(pid, 0)
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=3,
        )
        running = command.returncode == 0 and os.path.join(ROOT, "monitor.py") in command.stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        pid, running = 0, False
    return {"running": running, "pid": pid if running else None,
            "logFile": RUNNER_LOG_FILE}


def runner_progress(running: bool = False) -> dict:
    """Show only diagnostics for the active runner campaign."""
    config = load_config()
    targets = validated_targets(config["booking"].get("targets", []))
    try:
        state = monitor.load_state()
    except (OSError, ValueError):
        state = {}
    active = state.get("active_run") or {}
    run_id = active.get("id", "")
    accounts = active.get("accounts") or [
        row["id"] for row in account_status()["accounts"] if row["netid"] and row["passwordSet"]
    ] or ["1"]
    by_key = {
        (account, monitor._target_key(target)): {
            "accountId": account,
            "label": f"账号 {account} · {target['activity']} · {target['center']} "
                     f"{target['date']} {target['from']}-{target['to']}"
                     + (f" Area {target['facilityId']}" if target.get("facilityId") else ""),
            "releaseAt": release_at(target, config),
            "readiness": "not_started", "courts": 0,
            "confirmed": 0, "unknown": 0, "sending": 0,
            "failures": {}, "latency": {}, "last": "",
        } for account in accounts for target in targets
        if not target.get("accountId") or target["accountId"] == account
    }
    court_statuses = {key: {} for key in by_key}
    failures = {key: Counter() for key in by_key}
    durations = {key: {} for key in by_key}
    for (account, key), row in by_key.items():
        if key not in active.get("targets", []) or account not in active.get("accounts", ["1"]):
            continue
        readiness = (state.get("readiness") or {}).get(
            key if account == "1" else f"account:{account}|{key}", {})
        if readiness.get("run_id") != run_id:
            row["readiness"] = "checking"
            continue
        lanes = list(readiness.get("lanes", {}).values())
        statuses = {lane["status"] for lane in lanes}
        row["courts"] = sum(lane.get("courts", 0) for lane in lanes)
        if statuses <= {"ready"} and lanes:
            row["readiness"] = "ready"
        elif "ready" in statuses or "cached" in statuses:
            row["readiness"] = "cached" if statuses <= {"cached", "ready"} else "partial"
        elif "retrying" in statuses or "failed" in statuses:
            row["readiness"] = "retrying"
        elif "waiting" in statuses:
            row["readiness"] = "waiting"
        else:
            row["readiness"] = "checking"
        failed_lane = next((lane for lane in lanes if lane["status"] in ("failed", "retrying", "cached")), None)
        if failed_lane:
            row["last"] = f"{failed_lane['stage']}: {failed_lane['reason_code']}"

    if run_id and os.path.exists(monitor.LOG_FILE):
        with open(monitor.LOG_FILE, encoding="utf-8") as handle:
            offset = active.get("log_offset", 0)
            if isinstance(offset, int) and 0 <= offset <= os.path.getsize(monitor.LOG_FILE):
                handle.seek(offset)
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                key = (event.get("account_id", "1"), event.get("target_key"))
                if event.get("run_id") != run_id or key not in by_key:
                    continue
                row = by_key[key]
                court = event.get("court_id") or next(
                    (item.get("court_id") for item in event.get("events", [])
                     if item.get("court_id")), key,
                )
                status = event.get("status")
                if status in ("sending", "booked", "unknown", "failed"):
                    court_statuses[key][court] = status
                for item in event.get("events", []):
                    if isinstance(item.get("duration_ms"), (int, float)):
                        durations[key].setdefault(item["stage"], []).append(item["duration_ms"])
                if "attempt_failures" in event:
                    failures[key].update(item["stage"] for item in event["attempt_failures"])
                else:
                    for item in event.get("events", []):
                        if item.get("error") or (item.get("stage") == "availability"
                                                 and item.get("available") == 0):
                            failures[key][item["stage"]] += 1
                    if status in ("failed", "unknown") and not any(
                        item.get("stage") == event.get("stage")
                        and (item.get("error") or item.get("available") == 0)
                        for item in event.get("events", [])
                    ):
                        failures[key][event.get("stage") or "runner"] += 1
                if status in ("booked", "unknown", "failed"):
                    row["last"] = f"{event.get('stage')}: {event.get('reason_code')}"
    if run_id:
        explicit = {}
        for target in targets:
            if target.get("facilityId"):
                base = monitor._target_key({**target, "facilityId": ""})
                explicit.setdefault(base, set()).add(target["facilityId"])
        for account in accounts:
            for target in targets:
                key = monitor._target_key(target)
                pair = (account, key)
                if pair not in by_key:
                    continue
                if key not in active.get("targets", []):
                    continue
                base = monitor._target_key({**target, "facilityId": ""})
                prefix = f"{base}|court:"
                for storage in ("campaigns", "processed"):
                    for court_key, item in (state.get(storage) or {}).items():
                        if (not court_key.startswith(prefix) or item.get("run_id") != run_id
                                or item.get("account_id", "1") != account):
                            continue
                        court = court_key[len(prefix):]
                        if target.get("facilityId") and target["facilityId"] != court:
                            continue
                        if not target.get("facilityId") and court in explicit.get(base, set()):
                            continue
                        if court_statuses[pair].get(court) in ("booked", "unknown", "failed"):
                            continue
                        if item.get("status") in ("booked", "unknown", "final_sent"):
                            court_statuses[pair][court] = (
                                ("sending" if running else "unknown")
                                if item["status"] == "final_sent" else item["status"]
                            )
                            by_key[pair]["last"] = (
                                ("final_submit: 等待 POSS 回复" if running else
                                 "final_submit: 已发出但进程中断，需核对")
                                if item["status"] == "final_sent" else
                                f"{item.get('stage', 'runner')}: {item.get('reason_code', item['status'])}"
                            )
    for key, row in by_key.items():
        counts = Counter(court_statuses[key].values())
        row["confirmed"] = counts["booked"]
        row["unknown"] = counts["unknown"]
        row["sending"] = counts["sending"]
        row["failures"] = dict(failures[key])
        row["latency"] = {stage: {"lastMs": values[-1], "maxMs": max(values)}
                          for stage, values in durations[key].items()}
        parts = key[1].split("|")
        activity_key = f"{key[0]}|{parts[4]}|{parts[0]}|{parts[1]}"
        row["activityStatus"] = (state.get("account_activities", {}).get(
            activity_key, {}).get("status", ""))
        row["accountDone"] = row["activityStatus"] == "booked"
        if (not running and key[1] in active.get("targets", [])
                and not active.get("finished_at")
                and row["readiness"] in ("checking", "waiting", "retrying", "cached", "partial")):
            row["readiness"] = "interrupted"
            row["last"] = row["last"] or "预检阶段中断"
    return {"runId": run_id, "targets": list(by_key.values())}


def start_runner() -> dict:
    with _runner_lock:
        status = runner_status()
        if status["running"]:
            return status
        config = load_config()
        if not config["booking"].get("enabled", True):
            raise PossError("自动抢位未启用，请先在配置中开启")
        targets = validated_targets(config["booking"].get("targets", []))
        if not targets:
            raise PossError("先保存至少一个抢位目标")
        now = datetime.now(TZ)
        latest = max(datetime.strptime(release_at(target, config), "%Y-%m-%d %H:%M")
                     .replace(tzinfo=TZ) for target in targets)
        if now > latest + timedelta(minutes=config["booking"].get("grace_minutes", 30)):
            raise PossError("目标放位时间已过，请先在 UI 选择下一次日期")
        accounts = account_status()
        if not accounts["ready"]:
            raise PossError("先配置 POSS 账号")
        for target in targets:
            monitor._eligible_accounts(target, [row["id"] for row in accounts["accounts"]])
        os.makedirs(os.path.dirname(RUNNER_LOG_FILE), exist_ok=True)
        python = os.path.join(ROOT, ".venv", "bin", "python")
        if not os.path.exists(python):
            python = sys.executable
        with open(RUNNER_LOG_FILE, "a", encoding="utf-8") as log:
            log.write(f"[ui] {now.isoformat(timespec='seconds')} 启动本地并行抢位\n")
            log.flush()
            runner = subprocess.Popen(
                [python, "-u", os.path.join(ROOT, "monitor.py"), "--wait"],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
            if shutil.which("caffeinate"):
                subprocess.Popen(
                    ["caffeinate", "-i", "-w", str(runner.pid)],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True,
                )
        with open(RUNNER_PID_FILE, "w", encoding="utf-8") as handle:
            handle.write(f"{runner.pid}\n")
        return {"running": runner.poll() is None, "pid": runner.pid,
                "logFile": RUNNER_LOG_FILE}


class Handler(BaseHTTPRequestHandler):
    server_version = "PossConsole/2.0"

    def log_message(self, *_):
        pass

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def fail(self, exc, status=400):
        self.send_json({"ok": False, "error": str(exc), "type": type(exc).__name__}, status)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        try:
            if path in ("/", "/index.html"):
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/account":
                self.send_json({"ok": True, "account": account_status()})
            elif path == "/api/github/status":
                self.send_json({"ok": True, "github": github_status()})
            elif path == "/api/config":
                self.send_json({"ok": True, "config": load_config()})
            elif path == "/api/meta":
                self.send_json({"ok": True, "meta": get_client().metadata()})
            elif path == "/api/timetable":
                self.send_json(self.api_timetable(query))
            elif path == "/api/targets":
                self.send_json({"ok": True, "targets": load_config()["booking"].get("targets", [])})
            elif path == "/api/runner/status":
                runner = runner_status()
                self.send_json({"ok": True, "runner": runner,
                                "progress": runner_progress(runner["running"])})
            else:
                self.fail("not found", 404)
        except LoginError as exc:
            self.fail(exc, 401)
        except SessionExpired as exc:
            self.fail(exc, 401)
        except PossError as exc:
            self.fail(exc, 400)
        except Exception as exc:  # noqa: BLE001
            self.fail(exc, 500)

    def api_timetable(self, query):
        client = get_client()
        table = client.timetable(
            (query.get("datasetId") or [""])[0],
            (query.get("activityId") or [""])[0],
            (query.get("centerId") or [""])[0],
            "",
        )
        return {"ok": True, "table": table}

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self.read_json()
            if path == "/api/account":
                global _client
                with _account_verify_lock:
                    write_env(payload)
                    _client = None
                    result = verify_accounts()
                self.send_json({"ok": True, "account": result})
            elif path == "/api/accounts/verify":
                with _account_verify_lock:
                    result = verify_accounts()
                self.send_json({"ok": True, "account": result})
            elif path == "/api/github/remote":
                self.send_json({"ok": True, "github": set_github_remote(payload.get("remote", ""))})
            elif path == "/api/github/push":
                self.send_json({"ok": True, "github": push_config_to_github()})
            elif path == "/api/login":
                get_client(force_login=True)
                self.send_json({"ok": True, "meta": get_client().metadata()})
            elif path == "/api/config":
                config = deep_merge(load_config(), payload)
                save_config(config)
                self.send_json({"ok": True, "config": config})
            elif path == "/api/targets":
                config = load_config()
                targets = validated_targets(payload.get("targets"))
                configured = {row["id"] for row in account_status()["accounts"]
                              if row["netid"] and row["passwordSet"]}
                if any(target.get("accountId") not in configured
                       for target in targets if target.get("accountId")):
                    raise PossError("目标指定的账号尚未保存，请先保存账号")
                config["booking"]["targets"] = targets
                save_config(config)
                self.send_json({"ok": True, "targets": config["booking"]["targets"]})
            elif path == "/api/runner/start":
                self.send_json({"ok": True, "runner": start_runner()})
            elif path == "/api/booking/preview":
                selection = selection_from_payload(payload)
                preview = client_for_target(payload).preview_booking(selection)
                self.send_json({"ok": True, "preview": preview})
            elif path == "/api/booking/test":
                selection = selection_from_payload(payload)
                result = client_for_target(payload).book(selection, dry_run=False)
                self.send_json({"ok": True, "result": result.__dict__})
            else:
                self.fail("not found", 404)
        except LoginError as exc:
            self.fail(exc, 401)
        except SessionExpired as exc:
            self.fail(exc, 401)
        except PossError as exc:
            self.fail(exc, 400)
        except Exception as exc:  # noqa: BLE001
            self.fail(exc, 500)


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>POSS 抢课台</title>
<style>
:root{--ink:#18212b;--muted:#6d7885;--line:#dbe2e9;--paper:#fff;--bg:#edf2f4;--navy:#123247;--teal:#0f766e;--gold:#f0b429;--pink:#ef3d99;--blue:#2075e8;--gray:#c3cbd4;--pale:#f5f8fa;--shadow:0 14px 40px #17304212;--radius:18px}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 8% 4%,#fff 0 13%,transparent 32%),linear-gradient(135deg,#eef4f2,#e8eef4);color:var(--ink);font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}button,select,input{font:inherit}button{border:0;cursor:pointer}.shell{max-width:1500px;margin:auto;padding:26px 28px 70px}.top{display:flex;align-items:end;gap:18px;margin-bottom:22px}.mark{width:54px;height:54px;border-radius:16px;background:var(--navy);color:#f9c74f;display:grid;place-items:center;font:800 25px Georgia;box-shadow:0 10px 20px #12324728}.title h1{font:800 31px Georgia,serif;margin:0;letter-spacing:-.04em}.title p{margin:4px 0 0;color:var(--muted)}.status{margin-left:auto;border:1px solid var(--line);background:#ffffffbb;padding:8px 14px;border-radius:999px;color:var(--muted)}.status.ok{color:var(--teal);background:#e8f8f1}.status.err{color:#b42318;background:#fff0ef}.status.busy{color:#956b00;background:#fff8de}.card{background:#ffffffdb;border:1px solid #fff;border-radius:var(--radius);box-shadow:var(--shadow);padding:21px;margin:15px 0;backdrop-filter:blur(16px)}.card h2{margin:0;font:700 20px Georgia,serif}.head{display:flex;align-items:center;gap:12px;margin-bottom:17px}.grow{flex:1}.toolbar{display:grid;grid-template-columns:1.1fr 1.35fr 1.6fr 1.6fr auto;gap:12px;align-items:end}.field{display:grid;gap:6px}.field label{font-size:11px;font-weight:800;letter-spacing:.1em;color:var(--muted);text-transform:uppercase}.field select,.field input{height:43px;padding:0 13px;border:1px solid var(--line);border-radius:12px;background:var(--paper);color:var(--ink);outline:none}.field select:focus,.field input:focus{border-color:var(--teal);box-shadow:0 0 0 4px #0f766e18}.btn{padding:11px 16px;border-radius:12px;background:#eaf0f3;color:var(--ink);font-weight:700}.btn:hover{filter:brightness(.98);transform:translateY(-1px)}.btn.primary{background:var(--navy);color:#fff}.btn.gold{background:var(--gold);color:#34270b}.btn.danger{background:#fff0ef;color:#b42318}.message{margin:14px 0 0;padding:12px 15px;border-radius:12px;background:var(--pale);color:var(--muted)}.message.error{background:#fff0ef;color:#b42318}.gridbox{overflow:auto;border:1px solid var(--line);border-radius:14px;background:#fff;max-height:68vh}.grid{border-collapse:separate;border-spacing:3px;width:max(100%,1100px);min-width:1100px}.grid th{position:sticky;top:0;background:#fff;z-index:2;padding:10px 6px;white-space:nowrap;font-size:12px}.grid th:first-child{left:0;z-index:3;text-align:right;min-width:77px;color:var(--muted)}.grid td{min-width:76px}.grid .day.today{color:var(--teal);border-bottom:3px solid var(--teal)}.grid .dow{display:block;font-weight:500;color:#9aa5af}.slot{height:42px;border-radius:10px;display:grid;place-items:center;color:#fff;font-weight:800;cursor:pointer;position:relative;background:var(--gray);color:#52606c}.slot.free{background:linear-gradient(#f349a1,#df2d8a);color:#fff}.slot.paid{background:linear-gradient(#3285ee,#1e67d4);color:#fff}.slot.zero{background:#f1f4f6;color:#9ba7b0}.slot.selected{outline:3px solid var(--gold);outline-offset:1px}.slot .count{font-size:12px}.legend{display:flex;flex-wrap:wrap;gap:8px;margin-top:13px;color:var(--muted);font-size:12px}.legend span{display:inline-flex;align-items:center;gap:6px;background:#f5f8fa;padding:6px 10px;border-radius:999px}.dot{width:10px;height:10px;border-radius:4px;display:inline-block}.pill{padding:4px 8px;border-radius:999px;background:#fff3cf;color:#866000;font-size:11px;white-space:nowrap}.empty{padding:25px;text-align:center;color:#9aa5af;border:1px dashed var(--line);border-radius:13px}.settings{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.account{display:flex;gap:12px;align-items:center}.account small{color:var(--muted)}details summary{cursor:pointer;color:var(--muted);font-weight:700;margin-top:18px}.testbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center}.testbar strong{font-size:13px}@media(max-width:900px){.shell{padding:17px 13px}.top{align-items:start}.title h1{font-size:26px}.status{position:absolute;right:14px;top:17px}.toolbar{grid-template-columns:1fr 1fr}.toolbar .action{grid-column:1/-1}.settings{grid-template-columns:1fr 1fr}.account{align-items:start;flex-direction:column}}@media(max-width:560px){.toolbar,.settings{grid-template-columns:1fr}.title p{max-width:70%}.card{padding:15px}.grid{min-width:950px}}
.testbar select{min-width:240px;max-width:100%;height:42px;padding:0 12px;border:1px solid var(--line);border-radius:12px;background:var(--paper);color:var(--ink)}
.progressgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px;margin-top:16px}.progressitem{background:#f5f8fa;border:1px solid var(--line);border-radius:13px;padding:13px}.progressitem strong{display:block;margin-bottom:5px}.progressitem small{display:block;color:var(--muted);margin-top:4px}
.accountrow{display:grid;grid-template-columns:90px 1fr 1fr auto;gap:12px;align-items:end;margin:10px 0}.accountrow .pill{align-self:center;text-align:center}.accountrow .field{min-width:0}.accountrow .field input{width:100%;min-width:0}.accountrow .check{grid-column:2/-1;color:var(--teal);font-size:12px}.accountrow .check.err{color:#b42318}.accountrow .check.wait{color:var(--muted)}@media(max-width:560px){.accountrow{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.accountrow .pill,.accountrow .check{grid-column:1/-1}.accountrow .btn{grid-column:1/-1}}
.target-section{background:linear-gradient(145deg,#f9fcfb,#f1f7f6);border:1px solid #d5e5e1}
.target-head{display:flex;align-items:end;justify-content:space-between;gap:20px;padding:2px 2px 18px;border-bottom:1px solid #dce9e6}
.target-kicker{color:var(--teal);font-size:10px;font-weight:800;letter-spacing:.16em;margin-bottom:4px}
.target-head h2{font:700 25px Georgia,serif;line-height:1.2}
.target-head p{margin:5px 0 0;color:var(--muted);font-size:12px}
.target-actions{display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end}
.target-actions .btn{white-space:nowrap}
.targets{display:grid;gap:13px;margin-top:17px;min-width:0}
.target-group{min-width:0;overflow:hidden;background:#fff;border:1px solid #dce7e4;border-radius:16px;box-shadow:0 8px 24px #143d3810}
.target-group-head{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 17px;background:#e9f4f0;border-bottom:1px solid #dce7e4}
.target-group-title{display:flex;flex-wrap:wrap;align-items:baseline;gap:7px 13px;min-width:0}
.target-group-title strong{font-size:15px;color:var(--navy)}
.target-group-title span{font-size:12px;color:#42635e}
.target-release{font-size:11px;font-weight:700;color:var(--teal);text-align:right}
.target-release.expired{color:#a64b30}
.target-row{display:grid;grid-template-columns:132px minmax(0,1fr) 172px 34px;grid-template-areas:"when place account remove";gap:15px;align-items:center;padding:15px 17px;min-width:0}
.target-row+.target-row{border-top:1px solid #edf1f1}
.target-when{grid-area:when;display:flex;flex-direction:column;gap:2px}
.target-when strong{font:700 21px Georgia,serif;color:var(--navy);line-height:1.1;white-space:nowrap}
.target-when small{font-size:11px;color:var(--muted)}
.target-place{grid-area:place;min-width:0}
.target-place strong{display:block;font-size:14px;line-height:1.35;overflow-wrap:anywhere}
.target-meta{display:flex;flex-wrap:wrap;gap:4px 10px;margin-top:5px;color:var(--muted);font-size:11px}
.target-meta span+span:before{content:"·";margin-right:9px;color:#aab9b5}
.target-account{grid-area:account;display:grid;gap:4px;min-width:0}
.target-account span{color:var(--muted);font-size:10px;font-weight:800;letter-spacing:.06em}
.target-account select{width:100%;min-width:0;height:38px;padding:0 10px;border:1px solid var(--line);border-radius:10px;background:#fff;color:var(--ink)}
.target-account select:focus{outline:2px solid #0f766e45;border-color:var(--teal)}
.target-remove{grid-area:remove;width:34px;height:34px;border-radius:10px;background:#f8eded;color:#a33d38;font-size:21px;line-height:1}
.target-remove:hover{background:#f4dada}
.target-note{display:flex;gap:9px;margin-top:14px;color:#5a716d;font-size:12px;line-height:1.5}
.target-note strong{white-space:nowrap;color:var(--teal)}
.targets .empty{background:#fff}
@media(max-width:700px){.target-row{grid-template-columns:112px minmax(0,1fr) 34px;grid-template-areas:"when place remove" "account account account";gap:10px 12px}.target-account{max-width:240px}.target-head{align-items:start;flex-direction:column;gap:12px}.target-actions{justify-content:flex-start}}
@media(max-width:430px){.target-head h2{font-size:22px}.target-actions{width:100%;display:grid;grid-template-columns:1fr 1fr}.target-actions .btn{padding:9px 5px}.target-group-head{align-items:start;flex-direction:column;gap:2px}.target-release{text-align:left}.target-row{grid-template-columns:minmax(0,1fr) 34px;grid-template-areas:"when remove" "place place" "account account";gap:8px 10px;padding:14px}.target-when{flex-direction:row;align-items:baseline;gap:7px}.target-when strong{font-size:19px}.target-account{max-width:none}.target-meta span+span:before{display:none}.target-note{display:block}.target-note strong{margin-right:6px}}
.feedback{position:fixed;right:18px;bottom:18px;z-index:20;max-width:min(420px,calc(100vw - 28px));padding:12px 17px;border-radius:13px;background:#e5f5ee;border:1px solid #b9dfce;color:#145a43;box-shadow:0 16px 42px #143d3830;font-size:13px;font-weight:700;line-height:1.45}
.feedback.err{background:#fff0ed;border-color:#f3c5bc;color:#a33d38}.feedback.busy{background:#fff8e5;border-color:#efdb9f;color:#785a14}.feedback.info{background:#eaf0f3;border-color:#dbe2e9;color:var(--ink)}.feedback[hidden]{display:none}
@media(max-width:560px){.feedback{right:14px;bottom:14px}}
</style></head><body><main class="shell">
<header class="top"><div class="mark">P</div><div class="title"><h1>POSS 抢位台</h1><p>多账号、多场馆并行 · 每个账号按 Activity 抢一块</p></div><div id="status" class="status">正在初始化</div></header>
<section class="card"><div class="head"><h2>选择课程</h2><div class="grow"></div><button class="btn" id="reload">重新读取</button><button class="btn" id="login">重新登录</button></div><div class="toolbar">
<div class="field"><label>Facility / Category</label><select id="dataset"></select></div><div class="field"><label>Activity 活动</label><select id="activity"></select></div><div class="field"><label>Center 中心</label><select id="center"></select></div><div class="field"><label>Area 场地</label><select id="facility"></select></div><div class="action"><button class="btn primary" id="read">读取官方时间表</button></div></div><div class="message" id="notice">正在读取 POSS 的真实选项树…</div></section>
<section class="card"><div class="head"><h2>官方可订时间表</h2><div class="grow"></div><span id="caption" class="status">未读取</span></div><div class="gridbox"><div id="grid" class="empty">先选择 Activity 和 Center</div></div><div class="legend"><span><i class="dot" style="background:var(--gray)"></i>不可用</span><span><i class="dot" style="background:var(--pink)"></i>免费 / 可退押金</span><span><i class="dot" style="background:var(--blue)"></i>收费</span><span>数字 = POSS 真实可用 Area 数量</span><span>灰色目标也可以保存，等放位日再抢</span></div></section>
<section class="card target-section"><div class="target-head"><div><div class="target-kicker">BOOKING QUEUE</div><h2>并行抢位目标</h2><p id="targetSummary">0 个目标</p></div><div class="target-actions"><button class="btn danger" id="clear">清空目标</button><button class="btn gold" id="save">保存目标</button></div></div><div id="targets" class="targets"></div><div class="target-note"><strong>分配规则</strong><span>不指定账号时自动分配；同一账号在同一天的同一种 Activity 抢到一块即停止。</span></div></section>
<section class="card"><div class="head"><h2>本地并行抢位</h2><div class="grow"></div><span id="runnerStatus" class="status">检查中</span></div><div class="testbar"><button class="btn primary" id="startRunner">启动本地程序并等待放位</button><span id="runnerInfo">保存目标后启动；程序会在后台运行，关闭网页也会继续。</span></div><div id="runnerProgress" class="progressgrid"></div></section>
<section class="card"><div class="head"><h2>单笔下单验证</h2><div class="grow"></div></div><div class="testbar"><strong>手动测试只对下方选中的一笔目标执行。</strong><select id="manualTarget" aria-label="选择手动测试目标"></select><button class="btn" id="preview">预览请求（不下单）</button><button class="btn gold" id="test">真实下单测试</button></div><div class="message" id="result">自动并行抢位由本地 monitor.py 程序执行；此处只验证单个目标。</div></section>
<section class="card"><div class="head"><h2>账号与自动抢位</h2><div class="grow"></div><span id="account" class="status">检查中</span><button class="btn" id="addAccount">添加账号</button><button class="btn" id="verifyAccounts">验证账号并刷新余额</button></div><div id="accountRows"></div><div class="settings"><div class="field"><label>提前天数</label><input id="lead" type="number" min="0" max="30"></div><div class="field"><label>放位时刻</label><input id="trigger" type="time"></div></div><div style="margin-top:13px"><button class="btn primary" id="saveSettings">保存账号与设置</button></div><div class="message">保存后会逐个验证账号登录并读取 POSS 的 Deposit Balance。余额旁显示上次读取时间，可点击刷新；余额未知不会显示为 0。密码只存本机 `.env`，留空表示不改。先保存账号，再给目标指定账号；未指定的目标由程序自动分配。手动单笔测试使用目标指定账号；自动分配目标默认使用账号 1。</div></section>
<section class="card"><div class="head"><h2>GitHub 同步</h2><div class="grow"></div><span id="githubStatus" class="status">检查中</span></div><div class="settings"><div class="field" style="grid-column:span 3"><label>GitHub remote（只需首次配置）</label><input id="gitRemote" placeholder="git@github.com:user/repo.git"></div><div class="field"><label>当前分支</label><input id="gitBranch" readonly></div></div><div class="testbar" style="margin-top:13px"><button class="btn" id="saveGitRemote">保存 remote</button><button class="btn gold" id="pushGitConfig">一键上传 config.json</button></div><div class="message" id="githubMessage">只会提交和推送 config.json，不会上传 `.env`、密码或其他未提交代码。Git push 使用本机已有的 SSH key 或 Git credential。</div></section>
</main><div id="feedback" class="feedback" role="status" aria-live="polite" hidden></div><script>
const $=s=>document.querySelector(s);let META=null,TABLE=null,SELECTED=new Map(),CONFIG=null,ACCOUNTS=[];
const DOW=['日','一','二','三','四','五','六'];
function say(text,kind=''){const e=$('#status');e.textContent=text;e.className='status '+kind}
function note(text,error=false){$('#notice').textContent=text;$('#notice').className='message'+(error?' error':'')}
let feedbackTimer;
function feedback(text,kind='ok'){const e=$('#feedback');clearTimeout(feedbackTimer);e.textContent=({ok:'成功 · ',err:'失败 · ',busy:'进行中 · ',info:'已取消 · '}[kind]||'')+text;e.className='feedback '+kind;e.hidden=false;if(kind!=='busy')feedbackTimer=setTimeout(()=>{e.hidden=true},kind==='err'?8000:5000)}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function api(path,opt){const r=await fetch(path,opt);const j=await r.json().catch(()=>({ok:false,error:'服务器返回不是 JSON'}));if(!r.ok||j.ok===false)throw Error(j.error||`HTTP ${r.status}`);return j}
function post(path,data){return api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})}
function options(nodes){return (nodes||[]).map(n=>({value:n.value,label:`${n.label} (${n.value})`,raw:n}))}
function fill(id,items,placeholder){const e=$(id);e.innerHTML=items.length?'':'<option value="">'+placeholder+'</option>';for(const i of items){const o=document.createElement('option');o.value=i.value;o.textContent=i.label;o.raw=i.raw;e.appendChild(o)}if(items.length)e.value=items[0].value}
function selectedNode(id){return $(id).selectedOptions[0]?.raw||null}
function fillFacility(){fill('#facility',[{value:'',label:'任意 Area（所有可用球场）',raw:null},...options(selectedNode('#center')?.children)],'无 Area')}
function renderSelectors(){const tree=META.tree||[];fill('#dataset',options(tree),'无 Facility');const ds=selectedNode('#dataset');fill('#activity',options(ds?.children),'无 Activity');const act=selectedNode('#activity');fill('#center',options(act?.children),'无 Center');fillFacility()}
function release(t){const d=new Date(t.date+'T00:00:00Z');d.setUTCDate(d.getUTCDate()-(CONFIG?.booking?.lead_days??7));return d.toISOString().slice(0,10)+' '+(CONFIG?.booking?.trigger_time||'08:30')}
function currentSelection(date='',start='',end=''){const ds=selectedNode('#dataset'),act=selectedNode('#activity'),ctr=selectedNode('#center'),fac=selectedNode('#facility');return {datasetId:ds?.value||'',activityId:act?.value||'',centerId:ctr?.value||'',facilityId:fac?.value||'',date,start,end,activityName:act?.label||'',centerName:ctr?.label||'',facilityName:fac?.label||'',bookType:'INDV'} }
function key(t){return [t.datasetId,t.activityId,t.centerId,t.facilityId||'',t.date,t.from||t.start,t.to||t.end,t.bookType||'INDV'].join('|')}
function cls(slot){if(!slot||slot.left===0)return 'slot zero';if(slot.charge==null)return 'slot free';return Number(slot.charge)>0?'slot paid':'slot free'}
function renderGrid(){if(!TABLE){$('#grid').className='empty';$('#grid').textContent='选择完成后点击「读取官方时间表」';return}const dates=TABLE.columns.map(c=>c.date),times=[...new Set(TABLE.columns.flatMap(c=>c.slots.map(s=>s.start)))].sort();let html='<table class="grid"><thead><tr><th>时间</th>';for(const d of dates){const day=new Date(d+'T00:00:00');html+=`<th class="day ${d===META.today?'today':''}">${d.slice(5)}<span class="dow">周${DOW[day.getDay()]}</span></th>`}html+='</tr></thead><tbody>';for(const tm of times){html+=`<tr><th>${tm}</th>`;for(const d of dates){const col=TABLE.columns.find(c=>c.date===d);const slot=col?.slots.find(s=>s.start===tm);const id=slot?key({...currentSelection(d,slot.start,slot.end),from:slot.start}):'';const picked=slot&&SELECTED.has(id);const title=slot?`${d} ${slot.start}-${slot.end}\n可用 Area：${slot.left}\n费用：$${slot.charge??'?'} / 可退押金：$${slot.refundableFee??'?'}${slot.status?'\n'+slot.status:''}`:`${d} ${tm}\nPOSS 没有返回这个时段`;html+=`<td><div class="${cls(slot)} ${picked?'selected':''}" title="${esc(title)}" data-date="${d}" data-start="${tm}">${slot?`<span class="count">${slot.left}</span>`:''}</div></td>`}html+='</tr>'}$('#grid').className='';$('#grid').innerHTML=html;document.querySelectorAll('.slot').forEach(e=>e.onclick=()=>toggle(e.dataset.date,e.dataset.start));$('#caption').textContent=`${TABLE.columns.length} 天 · ${[...TABLE.columns.flatMap(c=>c.slots)].length} 个官方时段`}
function toggle(date,start){const col=TABLE.columns.find(c=>c.date===date),slot=col?.slots.find(s=>s.start===start);if(!slot){feedback(`${date} ${start} 没有官方时段数据`,'err');return}const t=currentSelection(date,slot.start,slot.end);const k=key({...t,from:t.start}),removed=SELECTED.has(k);if(removed)SELECTED.delete(k);else SELECTED.set(k,{...t,from:t.start,to:t.end,charge:slot.charge,refundableFee:slot.refundableFee});renderGrid();renderTargets();feedback(`${date} ${start} ${removed?'已移出':'已加入'}目标，点击「保存目标」生效`)}
function renderTargets(){
  const box=$('#targets'),arr=[...SELECTED.values()].sort((a,b)=>(a.date+a.activityName+a.from+a.centerName).localeCompare(b.date+b.activityName+b.from+b.centerName));
  const groups=new Map();
  for(const t of arr){const groupKey=t.date+'|'+t.activityId;if(!groups.has(groupKey))groups.set(groupKey,[]);groups.get(groupKey).push(t)}
  const summary=()=>`${arr.length} 个目标 · ${groups.size} 组放位 · ${arr.filter(t=>!t.accountId).length} 个自动分配`;
  $('#targetSummary').textContent=summary();
  const chosen=$('#manualTarget').value;
  $('#manualTarget').innerHTML=arr.length?arr.map(t=>`<option value="${esc(key(t))}">${esc(t.date)} ${esc(t.from)} ${esc(t.centerName)}</option>`).join(''):'<option value="">先添加目标</option>';
  if(SELECTED.has(chosen))$('#manualTarget').value=chosen;
  if(!arr.length){box.innerHTML='<div class="empty">还没有目标。点击上面的任意时段即可加入；灰色时段也可保存。</div>';return}
  box.innerHTML=[...groups.values()].map(items=>{
    const first=items[0],open=release(first);
    const weekday=DOW[new Date(first.date+'T12:00:00+08:00').getDay()];
    const expired=new Date(open.replace(' ','T')+':00+08:00').getTime()<Date.now();
    const rows=items.map(t=>{
      const missing=t.accountId&&!ACCOUNTS.some(a=>a.id===t.accountId);
      const choices=[`<option value="">自动分配</option>`,...ACCOUNTS.map(a=>`<option value="${esc(a.id)}">账号 ${esc(a.id)}</option>`)];
      if(missing)choices.push(`<option value="${esc(t.accountId)}">账号 ${esc(t.accountId)}（未配置）</option>`);
      const fee=t.charge==null?'费用待确认':`HK$${t.charge}${t.refundableFee?' · 可退押金 HK$'+t.refundableFee:''}`;
      return `<div class="target-row"><div class="target-when"><strong>${esc(t.from)}–${esc(t.to)}</strong><small>目标时段</small></div><div class="target-place"><strong>${esc(t.centerName)}</strong><div class="target-meta"><span>${esc(t.facilityName||'任意 Area')}</span><span>${esc(fee)}</span></div></div><label class="target-account"><span>抢位账号</span><select data-target-account="${esc(key(t))}" aria-label="${esc(t.centerName)} ${esc(t.from)} 抢位账号">${choices.join('')}</select></label><button class="target-remove" data-remove="${esc(key(t))}" aria-label="移除 ${esc(t.centerName)} ${esc(t.from)}" title="移除目标">×</button></div>`
    }).join('');
    return `<div class="target-group"><div class="target-group-head"><div class="target-group-title"><strong>${esc(first.date)} 周${weekday}</strong><span>${esc(first.activityName)} · ${items.length} 个目标</span></div><span class="target-release ${expired?'expired':''}">${expired?'放位已过':'放位'} · ${esc(open.slice(5))}</span></div>${rows}</div>`
  }).join('');
  box.querySelectorAll('[data-target-account]').forEach(e=>{e.value=SELECTED.get(e.dataset.targetAccount)?.accountId||'';e.onchange=()=>{SELECTED.get(e.dataset.targetAccount).accountId=e.value;$('#targetSummary').textContent=summary();feedback(`抢位账号已改为${e.value?'账号 '+e.value:'自动分配'}，点击「保存目标」生效`)}});
  box.querySelectorAll('[data-remove]').forEach(e=>e.onclick=()=>{SELECTED.delete(e.dataset.remove);renderGrid();renderTargets();feedback('目标已从列表移除，点击「保存目标」生效')})
}
async function loadMeta(announce=false){say('读取 POSS 选项树…','busy');if(announce)feedback('读取课程与时间表','busy');try{const r=await api('/api/meta');META=r.meta;renderSelectors();note('已读取真实 Facility → Activity → Center → Area 树，共 '+META.tree.length+' 个 Facility。');say('已登录','ok');const ready=await readTable(false);if(announce)feedback(ready?'课程与时间表已更新':'课程已更新，时间表读取失败',ready?'ok':'err');return ready}catch(e){say('读取失败','err');note(e.message,true);if(announce)feedback('读取课程失败：'+e.message,'err');return false}}
async function readTable(announce=false){const ds=$('#dataset').value,act=$('#activity').value,ctr=$('#center').value;if(!ds||!act||!ctr){note('请先选择 Activity 和 Center',true);if(announce)feedback('请先选择 Activity 和 Center','err');return false}say('读取官方时间表…','busy');if(announce)feedback('读取官方时间表','busy');note('正在请求 POSS timetable.json，名额按 facilityIds - occupiedFacilityIds 计算…');try{const r=await api(`/api/timetable?datasetId=${encodeURIComponent(ds)}&activityId=${encodeURIComponent(act)}&centerId=${encodeURIComponent(ctr)}`);TABLE=r.table;renderGrid();note((TABLE.message||'已读取官方时间表')+'  放位规则：目标日前 '+TABLE.bookingRule.leadDays+' 天 '+TABLE.bookingRule.openTime+' 开放。');say('时间表已更新','ok');if(announce)feedback('官方时间表已更新');return true}catch(e){TABLE=null;renderGrid();say('时间表失败','err');note(e.message,true);if(announce)feedback('时间表读取失败：'+e.message,'err');return false}}
async function loadConfig(){const r=await api('/api/config');CONFIG=r.config;$('#lead').value=CONFIG.booking.lead_days;$('#trigger').value=CONFIG.booking.trigger_time;SELECTED=new Map((CONFIG.booking.targets||[]).map(raw=>{const t={...raw,from:raw.from||raw.start,to:raw.to||raw.end,activityName:raw.activityName||raw.activity||'',centerName:raw.centerName||raw.center||'',facilityName:raw.facilityName||raw.facility||''};return [key(t),t]}));renderTargets()}
function accountCheck(check){
  const when=check?.checkedAt?` · ${check.checkedAt.slice(5,16).replace('T',' ')} 香港时间`:'';
  const reasons={login_rejected:'账号或密码被拒绝',session_expired:'登录会话失效',booking_page_error:'预订页读取失败',balance_not_found:'未找到余额字段',tls_error:'TLS 连接失败',tls_connection_closed:'TLS 连接中断',tls_certificate_error:'证书校验失败',tls_handshake_error:'TLS 握手失败',connect_timeout:'连接超时',read_timeout:'响应超时',proxy_error:'代理连接失败',network_error:'网络失败',verification_error:'验证过程异常'};
  if(check?.status==='verified')return {text:`登录可用 · 余额 HK$${check.balance.amount}${when}`,kind:''};
  if(check?.status==='balance_unknown'||check?.status==='balance_error')return {text:`登录可用 · 余额暂不可读（${reasons[check.reasonCode]||check.reasonCode}）${when}`,kind:'wait'};
  if(check?.status==='login_failed')return {text:`验证失败：${reasons[check.reasonCode]||check.reasonCode}${when}`,kind:'err'};
  if(check?.status==='not_configured')return {text:'账号尚未配置完整',kind:'err'};
  return {text:'尚未验证 · 保存账号后自动验证，或点击刷新余额',kind:'wait'}
}
function renderAccounts(){
  const box=$('#accountRows');
  box.innerHTML=ACCOUNTS.map(a=>{
    const check=accountCheck(a.check);
    return `<div class="accountrow" data-account="${esc(a.id)}"><span class="pill">账号 ${esc(a.id)}</span><div class="field"><label>NetID</label><input class="account-netid" value="${esc(a.netid||'')}" autocomplete="username"></div><div class="field"><label>NetPassword</label><input class="account-password" type="password" autocomplete="new-password" placeholder="${a.passwordSet?'留空 = 不修改':'请填写密码'}"></div>${a.id==='1'?'<span></span>':`<button class="btn danger" data-remove-account="${esc(a.id)}">移除</button>`}<small class="check ${check.kind}">${esc(check.text)}</small></div>`
  }).join('');
  box.querySelectorAll('[data-remove-account]').forEach(b=>b.onclick=()=>{collectAccounts();ACCOUNTS=ACCOUNTS.filter(a=>a.id!==b.dataset.removeAccount);renderAccounts();renderTargets();feedback(`账号 ${b.dataset.removeAccount} 已从表单移除，点击「保存账号与设置」生效`)})
}
function collectAccounts(){ACCOUNTS=[...document.querySelectorAll('.accountrow')].map(row=>({id:row.dataset.account,netid:row.querySelector('.account-netid').value,password:row.querySelector('.account-password').value,passwordSet:ACCOUNTS.find(a=>a.id===row.dataset.account)?.passwordSet||false}))}
async function loadAccount(){const r=await api('/api/account');const a=r.account;ACCOUNTS=a.accounts;renderAccounts();renderTargets();const loggedIn=ACCOUNTS.filter(row=>['verified','balance_unknown','balance_error'].includes(row.check?.status)).length;const balanced=ACCOUNTS.filter(row=>row.check?.status==='verified').length;const failed=ACCOUNTS.some(row=>row.check?.status==='login_failed');$('#account').textContent=a.ready?`已配置 ${ACCOUNTS.length} 个 · 可登录 ${loggedIn} 个 · 余额可读 ${balanced} 个`:'账号未配置完整';$('#account').className='status '+(!a.ready||failed?'err':balanced===ACCOUNTS.length?'ok':'busy')}
function accountNotice(){const failed=ACCOUNTS.filter(a=>a.check?.status==='login_failed');if(failed.length)note(`账号 ${failed.map(a=>a.id).join('、')} 验证失败，请查看各账号的原因`,true);else if(ACCOUNTS.some(a=>a.check?.status!=='verified'))note('登录状态已刷新，部分余额暂不可读',true);else note('已刷新各账号的登录状态与余额')}
async function loadGitHubStatus(){const r=await api('/api/github/status'),g=r.github;$('#gitRemote').value=g.remote||'';$('#gitBranch').value=g.branch||'detached HEAD';$('#githubStatus').textContent=g.remoteConfigured?(g.configDirty?'有待上传修改':'已连接'):'未配置 remote';$('#githubStatus').className='status '+(g.ready?(g.configDirty?'busy':'ok'):'err');if(g.otherDirty)$('#githubMessage').textContent='提示：其他代码文件也有本地修改，但一键上传只会提交 config.json。'+(g.configDirty?' 当前 config.json 有待上传修改。':'');else if(g.configDirty)$('#githubMessage').textContent='config.json 有待上传修改，点击「一键上传 config.json」后才会提交并推送。'}
function renderRunnerProgress(progress,running){const names={ready:'预检通过',cached:'编号已缓存，预检待恢复',partial:'部分通道待恢复',retrying:'预检重试中',checking:'检查中',waiting:'已登录，等待放位前预检',interrupted:'本地程序已退出，预检未完成',not_started:'待启动'};const rows=progress?.targets||[];$('#runnerProgress').innerHTML=rows.length?rows.map(t=>{const expired=!running&&new Date(t.releaseAt.replace(' ','T')+':00+08:00').getTime()<Date.now();const preflight=t.accountDone?'该账号此 Activity 已抢到，停止':t.activityStatus==='unknown'?'该账号此 Activity 提交待核对':expired&&t.readiness==='not_started'?'放位已过':names[t.readiness]||t.readiness;const errors=Object.entries(t.failures).map(([stage,count])=>`${stage} ${count} 次`).join(' · ');const latency=Object.entries(t.latency).filter(([stage])=>['timetable','first_step','final_submit'].includes(stage)).map(([stage,v])=>`${stage} 最近 ${(v.lastMs/1000).toFixed(1)} 秒 / 最慢 ${(v.maxMs/1000).toFixed(1)} 秒`).join(' · ');return `<div class="progressitem"><strong>${esc(t.label)}</strong><span>${esc(preflight)}${t.courts?' · 已预备 '+t.courts+' 块':''}</span><small>已确认 ${t.confirmed} 块 · 待确认 ${t.unknown} 块 · 请求中 ${t.sending} 块</small>${errors?`<small>失败阶段：${esc(errors)}</small>`:''}${latency?`<small>请求耗时：${esc(latency)}</small>`:''}${t.last?`<small>最近状态：${esc(t.last)}</small>`:''}</div>`}).join(''):'<div class="empty">尚未保存目标</div>'}
async function loadRunnerStatus(){try{const r=await api('/api/runner/status'),x=r.runner;$('#runnerStatus').textContent=x.running?'本地程序运行中':'未运行';$('#runnerStatus').className='status '+(x.running?'ok':'');$('#startRunner').disabled=x.running;$('#runnerInfo').textContent=x.running?`进程 ${x.pid} · 日志 ${x.logFile}`:'保存目标后启动；程序会在后台运行，关闭网页也会继续。';renderRunnerProgress(r.progress,x.running)}catch(e){$('#runnerStatus').textContent='状态读取失败';$('#runnerStatus').className='status err'}}
async function startLocalRunner(){const button=$('#startRunner');button.disabled=true;try{await saveTargets();const r=await post('/api/runner/start',{});$('#runnerStatus').textContent=r.runner.running?'本地程序运行中':'程序已退出';$('#runnerStatus').className='status '+(r.runner.running?'ok':'err');$('#runnerInfo').textContent=r.runner.running?`本地并行抢位已启动 · 进程 ${r.runner.pid} · 日志 ${r.runner.logFile}`:'程序已退出，请检查本地日志';await loadRunnerStatus()}catch(e){$('#runnerInfo').textContent=e.message;$('#runnerStatus').textContent='启动失败';$('#runnerStatus').className='status err'}finally{if($('#runnerStatus').textContent!=='本地程序运行中')button.disabled=false}}
async function saveTargets(){const r=await post('/api/targets',{targets:[...SELECTED.values()]});CONFIG.booking.targets=r.targets;note('已保存 '+r.targets.length+' 个并行目标');say('目标已保存','ok')}
function manualTarget(){return SELECTED.get($('#manualTarget').value)}
async function preview(){const t=manualTarget();if(!t){note('先选择一个目标',true);return}say('校验预订请求…','busy');try{const r=await post('/api/booking/preview',t);$('#result').textContent=`账号 ${t.accountId||"1"} 预览通过：${r.preview.context.center} / ${r.preview.context.facility||r.preview.facilityId} / ${t.date} ${t.from}-${t.to}，POSS 当前可用 Area ${r.preview.slot.left} 个；放位时间 ${r.preview.releaseAt}；没有下单。`;say('预览通过','ok')}catch(e){$('#result').textContent=e.message;say('预览失败','err')}}
async function realBook(){const t=manualTarget();if(!t){note('先选择一个目标',true);return}if(!confirm(`确认真实下单？\n账号 ${t.accountId||"1"}\n${t.activityName}\n${t.centerName}\n${t.date} ${t.from}-${t.to}\n\n只提交这一笔，两步流程不会自动重试。`))return;$('#test').disabled=true;say('真实下单中…','busy');try{const r=await post('/api/booking/test',t),x=r.result;$('#result').textContent=`阶段：${x.stage||'?'}\n结果：${x.status}\n${x.message}\n中心：${x.center}\nArea：${x.facility}\n日期：${x.date} ${x.start}-${x.end}\n费用：$${x.charge??'?'} / 可退押金：$${x.refundable_fee??'?'}`;say(x.status==='booked'?'已下单':'请人工确认',x.status==='booked'?'ok':'err')}catch(e){$('#result').textContent=e.message;say('下单失败','err')}finally{$('#test').disabled=false}}
$('#verifyAccounts').onclick=async()=>{const button=$('#verifyAccounts');button.disabled=true;$('#account').textContent='正在验证账号与读取余额…';try{await post('/api/accounts/verify',{});await loadAccount();accountNotice()}catch(e){note(e.message,true)}finally{button.disabled=false}};$('#startRunner').onclick=startLocalRunner;$('#saveGitRemote').onclick=async()=>{try{const r=await post('/api/github/remote',{remote:$('#gitRemote').value});$('#githubMessage').textContent='remote 已保存：'+r.github.remote;await loadGitHubStatus()}catch(e){$('#githubMessage').textContent=e.message;$('#githubMessage').className='message error'}};$('#pushGitConfig').onclick=async()=>{if(!confirm('只提交 config.json 并推送到当前分支，确认继续？'))return;$('#pushGitConfig').disabled=true;$('#githubMessage').textContent='正在提交并推送 config.json…';try{const r=await post('/api/github/push',{});$('#githubMessage').textContent=r.github.message;await loadGitHubStatus()}catch(e){$('#githubMessage').textContent=e.message;$('#githubMessage').className='message error'}finally{$('#pushGitConfig').disabled=false}};$('#dataset').onchange=()=>{renderSelectors();TABLE=null;renderGrid()};$('#activity').onchange=()=>{const act=selectedNode('#activity');fill('#center',options(act?.children),'无 Center');fillFacility();TABLE=null;renderGrid()};$('#center').onchange=()=>{fillFacility();TABLE=null;renderGrid()};$('#facility').onchange=()=>{};$('#read').onclick=readTable;$('#reload').onclick=loadMeta;$('#login').onclick=async()=>{try{await post('/api/login',{});await loadMeta()}catch(e){say('登录失败','err');note(e.message,true)}};$('#save').onclick=async()=>{try{await saveTargets()}catch(e){note(e.message,true)}};$('#clear').onclick=()=>{SELECTED.clear();renderGrid();renderTargets()};$('#preview').onclick=preview;$('#test').onclick=realBook;$('#addAccount').onclick=()=>{collectAccounts();const id=String(Math.max(0,...ACCOUNTS.map(a=>Number(a.id)))+1);ACCOUNTS.push({id,netid:'',passwordSet:false});renderAccounts();renderTargets()};$('#saveSettings').onclick=async()=>{try{collectAccounts();await post('/api/account',{accounts:ACCOUNTS});await post('/api/config',{booking:{lead_days:Number($('#lead').value),trigger_time:$('#trigger').value}});await loadAccount();await loadRunnerStatus();await loadMeta();accountNotice()}catch(e){note(e.message,true)}};
(async()=>{try{await loadConfig();await loadAccount();await loadGitHubStatus();await loadRunnerStatus();if(!$('#account').classList.contains('err'))await loadMeta();else note('请先填写账号和密码，再点击保存账号与设置')}catch(e){note(e.message,true)}})();
setInterval(loadRunnerStatus, 3000);
</script></body></html>'''


def main():
    parser = argparse.ArgumentParser(description="POSS 抢课台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"[poss-ui] {url}")
    if not args.no_browser:
        threading.Timer(.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
