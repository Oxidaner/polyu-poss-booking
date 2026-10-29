#!/usr/bin/env python3
"""POSS monitor and booking runner for the rebuilt client."""
from __future__ import annotations

import argparse
import json
import os
import random
import smtplib
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText

import requests

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from poss_client import (
    BookingAttemptError, PossError, Selection, SessionExpired, booking_release_at,
    account_ids_from_env, client_from_env, find_slot, load_env, transport_reason, tree_context,
)

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("POSS_CONFIG_FILE", os.path.join(ROOT, "config.json"))
STATE_FILE = os.environ.get("POSS_STATE_FILE", os.path.join(ROOT, "state.json"))
LOG_FILE = os.environ.get("POSS_LOG_FILE", os.path.join(ROOT, "dumps", "booking_attempts.jsonl"))
TZ = timezone(timedelta(hours=8))
REPORT_LOCK = threading.Lock()


def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as handle:
        return json.load(handle)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    return {"processed": {}}


def save_state(state):
    directory = os.path.dirname(os.path.abspath(STATE_FILE))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix=".poss-state-", delete=False) as handle:
            temporary = handle.name
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STATE_FILE)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def report_attempt(report):
    """Write only structured, non-secret diagnostics to disk and Actions."""
    with REPORT_LOCK:
        os.makedirs(os.path.dirname(os.path.abspath(LOG_FILE)), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, ensure_ascii=False) + "\n")
        brief = (f"{report['target']} | {report['status']} | {report['stage']} | "
                 f"{report['reason_code']} | {report['message']} | "
                 f"放位后 {report['release_lag_ms']} ms | 尝试 {report['attempts']} 次")
        print(f"[booking] {brief}", flush=True)
        for event in report["events"]:
            print(f"  [{event['stage']}] "
                  + " ".join(f"{key}={value}" for key, value in event.items()
                             if key not in ("stage", "at")), flush=True)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(f"- {brief.replace('|', ' / ')}\n")


def notify(title, body):
    """Use configured channels; never print tokens or passwords in failures."""
    env = {**load_env(os.path.join(ROOT, ".env")), **os.environ}
    token = env.get("PUSHPLUS_TOKEN", "")
    key = env.get("SERVERCHAN_KEY", "")
    delivered = False
    if token:
        try:
            response = requests.post(
                "https://www.pushplus.plus/send",
                json={"token": token, "title": title, "content": body, "template": "txt"},
                timeout=15,
            )
            response.raise_for_status()
            delivered |= response.json().get("code") == 200
        except (requests.RequestException, ValueError) as exc:
            print(f"[notify] PushPlus 失败: {type(exc).__name__}")
    if key:
        try:
            response = requests.post(f"https://sctapi.ftqq.com/{key}.send",
                                     data={"title": title, "desp": body}, timeout=15)
            response.raise_for_status()
            delivered |= response.json().get("code") == 0
        except (requests.RequestException, ValueError) as exc:
            print(f"[notify] Server酱 失败: {type(exc).__name__}")
    if all(env.get(name) for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO")):
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = Header(title, "utf-8")
            msg["From"] = env["SMTP_USER"]
            msg["To"] = env["MAIL_TO"]
            with smtplib.SMTP_SSL(env["SMTP_HOST"], int(env.get("SMTP_PORT") or 465),
                                  timeout=20) as server:
                server.login(env["SMTP_USER"], env["SMTP_PASS"])
                server.sendmail(env["SMTP_USER"], [env["MAIL_TO"]], msg.as_string())
            delivered = True
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            print(f"[notify] 邮件失败: {type(exc).__name__}")
    return delivered


def target_selection(target):
    return Selection(
        dataset_id=str(target.get("datasetId") or target.get("dataSetId") or "18"),
        activity_id=str(target.get("activityId") or target.get("actvId") or ""),
        center_id=str(target.get("centerId") or target.get("ctrId") or ""),
        facility_id=str(target.get("facilityId") or ""),
        date=str(target["date"]),
        start=str(target.get("from") or target.get("start")),
        end=str(target.get("to") or target.get("end")),
        activity_name=str(target.get("activity") or target.get("activityName") or ""),
        center_name=str(target.get("center") or target.get("centerName") or target.get("name") or ""),
        facility_name=str(target.get("facility") or target.get("facilityName") or ""),
        book_type=str(target.get("bookType") or "INDV"),
    )


def _target_key(target):
    item = target_selection(target)
    return "|".join((item.dataset_id, item.activity_id, item.center_id,
                     item.facility_id, item.date, item.start, item.end, item.book_type))


def _court_key(target, facility_id):
    return f"{_target_key({**target, 'facilityId': ''})}|court:{facility_id}"


def _account_activity(account_id, selection):
    return f"{account_id}|{selection.date}|{selection.dataset_id}|{selection.activity_id}"


def _eligible_accounts(target, accounts):
    assigned = str(target.get("accountId") if target.get("accountId") is not None else "")
    if assigned and assigned not in accounts:
        raise PossError(f"目标指定的账号 {assigned} 尚未配置")
    return [assigned] if assigned else accounts


def _readiness_key(account_id, target):
    key = _target_key(target)
    return key if account_id == "1" else f"account:{account_id}|{key}"


def _claimed_court(claims, selection, facility_id):
    return any(
        row.get("center_id") == selection.center_id
        and row.get("facility_id") == facility_id
        and row.get("date") == selection.date
        and row.get("start", "") < selection.end
        and selection.start < row.get("end", "")
        for row in claims.values()
    )


def release_at(target, config):
    rule = {
        "leadDays": int(config.get("booking", {}).get("lead_days", 7)),
        "openTime": config.get("booking", {}).get("trigger_time", "08:30"),
    }
    return booking_release_at(target["date"], rule)


def wait_for_nearby_release(client, targets, config, now, force=False, wait_long=False,
                            on_ready=None, on_status=None):
    """Login before a nearby release and keep the session warm while waiting."""
    if force:
        return now
    future = [release_at(target, config) for target in targets
              if release_at(target, config) > now]
    if not future:
        return now
    release = min(future)
    wait_seconds = (release - now).total_seconds()
    if wait_seconds > 10 * 60 and not wait_long:
        return now
    logged_in = False
    next_check = time.monotonic()
    failed_check = ""
    final_refresh_due = False
    final_refresh_done = False
    connection_refresh_done = False
    prepared = False
    print(f"等待放位时间 {release:%Y-%m-%d %H:%M}，约 {wait_seconds:.0f} 秒")
    while True:
        remaining = (release - datetime.now(TZ)).total_seconds()
        if remaining <= 0:
            return datetime.now(TZ)
        if remaining <= 3 * 60 and not final_refresh_due:
            next_check = min(next_check, time.monotonic())
            final_refresh_due = True
        if remaining <= 60 and not connection_refresh_done:
            next_check = min(next_check, time.monotonic())
            connection_refresh_done = True
        if time.monotonic() >= next_check:
            check = "heartbeat" if logged_in else "login"
            try:
                if logged_in:
                    try:
                        client.heartbeat(force=True)
                        print("[warmup] POSS 会话保活成功", flush=True)
                    except SessionExpired:
                        logged_in = False
                        check = "login"
                        client.login()
                        print("[warmup] POSS 会话已过期，重新登录成功", flush=True)
                else:
                    client.login()
                    print("[warmup] POSS 登录成功", flush=True)
                logged_in = True
                if on_ready and remaining <= 10 * 60 and (
                    not prepared or (final_refresh_due and not final_refresh_done)
                ):
                    check = "prepare"
                    on_ready(client)
                    prepared = True
                next_check = time.monotonic() + 4 * 60
                if final_refresh_due and not final_refresh_done:
                    print("[warmup] 放位前 3 分钟已确认 POSS 登录态", flush=True)
                    final_refresh_done = True
                if failed_check:
                    report_attempt({
                        "target": "POSS 预热", "status": "recovered",
                        "stage": check, "reason_code": "session_ready",
                        "message": "预热连接已恢复", "release_lag_ms": round(-remaining * 1000),
                        "attempts": 0, "events": [],
                    })
                    failed_check = ""
                if on_status:
                    on_status("ready", check, "session_ready")
            except (PossError, requests.RequestException) as exc:
                if isinstance(exc, SessionExpired):
                    logged_in = False
                reason = (transport_reason(exc) if isinstance(exc, requests.RequestException)
                          else type(exc).__name__)
                failed_check = check
                report_attempt({
                    "target": "POSS 预热", "status": "retrying", "stage": check,
                    "reason_code": reason, "message": f"{type(exc).__name__}: 预热请求失败",
                    "release_lag_ms": round(-remaining * 1000), "attempts": 0,
                    "events": [{"stage": check, "error": type(exc).__name__,
                                "retry_in_seconds": 30}],
                })
                print(f"[warmup] {'预备球场' if check == 'prepare' else '保活' if logged_in else '登录'}失败："
                      f"{reason}；30 秒后重试，不退出抢位任务", flush=True)
                if on_status:
                    on_status("retrying", check, reason)
                next_check = time.monotonic() + 30
        time.sleep(min(1, remaining))


def run_parallel_group(targets, config, state, args):
    """Race accounts independently, stopping after one booking per account/activity/day."""
    release = release_at(targets[0], config)
    group_key = f"{targets[0]['date']}|{release.isoformat()}"
    campaigns = state.setdefault("campaigns", {})
    processed = state.setdefault("processed", {})
    accounts = account_ids_from_env(os.path.join(ROOT, ".env"))
    if not accounts:
        raise PossError("没有配置完整的 POSS 账号")
    account_activities = state.setdefault("account_activities", {})
    claims = state.setdefault("court_claims", {})
    migrated = False
    for court_key, row in [*campaigns.items(), *processed.items()]:
        if (row.get("status") in ("booked", "final_sent")
                or row.get("status") == "unknown"
                and row.get("stage") in ("final_submit", "verify_record")):
            parts = (row.get("target") or court_key).split("|")
            if len(parts) > 4:
                activity_key = f"{row.get('account_id', '1')}|{parts[4]}|{parts[0]}|{parts[1]}"
                if row["status"] == "unknown" and row.get("reason_code") == "unknown_result":
                    row["status"] = "failed"
                    previous = account_activities.get(activity_key, {})
                    if previous.get("status") == "unknown" and previous.get("court_key") == court_key:
                        account_activities.pop(activity_key)
                    migrated = True
                else:
                    record = {
                        "status": "booked" if row["status"] == "booked" else "unknown",
                        "court_key": court_key,
                    }
                    if row["status"] == "booked":
                        account_activities[activity_key] = record
                    else:
                        account_activities.setdefault(activity_key, record)
                if len(parts) == 9 and parts[8].startswith("court:"):
                    facility_id = parts[8].removeprefix("court:")
                    claim_key = f"{parts[4]}|{parts[2]}|{facility_id}|{parts[5]}|{parts[6]}"
                    claims.setdefault(claim_key, {
                        "date": parts[4], "center_id": parts[2],
                        "facility_id": facility_id, "start": parts[5], "end": parts[6],
                        "account_id": row.get("account_id", "1"),
                    })
    if migrated and not args.dry_run:
        save_state(state)
    pending = [target for target in targets
               if any(account_activities.get(_account_activity(account, target_selection(target)), {}).get(
                   "status") not in ("booked", "unknown")
                   for account in _eligible_accounts(target, accounts))]
    if not pending:
        print(f"{targets[0]['date']} 的目标已处理，跳过")
        return
    now = datetime.now(TZ)
    if not args.force and now > release + timedelta(
        minutes=max(0, int(config["booking"].get("grace_minutes", 30)))
    ):
        for raw in pending:
            selection = target_selection(raw)
            report_attempt({
                "target": f"{selection.activity_name} {selection.center_name} "
                          f"{selection.date} {selection.start}-{selection.end}",
                "status": "failed", "stage": "schedule",
                "reason_code": "missed_release_window",
                "message": "超过放位宽限时间，未发送预订请求",
                "release_lag_ms": round((now - release).total_seconds() * 1000),
                "attempts": 0, "events": [],
            })
            if not args.dry_run:
                processed[_target_key(raw)] = {
                    "at": int(time.time()), "status": "failed",
                    "reason_code": "missed_release_window", "stage": "schedule",
                }
        if not args.dry_run:
            save_state(state)
        return
    if not args.force and not args.wait and (release - now).total_seconds() > 10 * 60:
        print(f"等待下次放位：{release:%Y-%m-%d %H:%M}")
        return

    stops = {_account_activity(account, target_selection(target)): threading.Event()
             for target in pending for account in _eligible_accounts(target, accounts)}
    state_lock = threading.Lock()
    run_id = f"{release.isoformat()}:{os.getpid()}:{time.time_ns()}"
    state["active_run"] = {"id": run_id, "release_at": release.isoformat(),
                           "log_offset": os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0,
                           "targets": [_target_key(target) for target in pending],
                           "accounts": accounts}
    state["readiness"] = {}
    if not args.dry_run:
        save_state(state)

    def emit(report, key, account_id):
        report_attempt({
            **report, "run_id": run_id, "target_key": key, "account_id": account_id,
            "at": report.get("at") or datetime.now(TZ).isoformat(timespec="milliseconds"),
        })

    active_courts = set()
    explicit_courts = {}
    for target in pending:
        selection = target_selection(target)
        if selection.facility_id:
            wildcard = {**target, "facilityId": ""}
            explicit_courts.setdefault(_target_key(wildcard), set()).add(selection.facility_id)
    burst = max(0, int(config["booking"].get("burst_seconds", 120)))
    retry_interval = max(1, int(config["booking"].get("retry_interval", 3)))
    preferred = {}
    load = {}
    for target in pending:
        if target.get("accountId"):
            selection = target_selection(target)
            group = (selection.date, selection.dataset_id, selection.activity_id)
            load[(group, target["accountId"])] = load.get((group, target["accountId"]), 0) + 1
    for target in pending:
        if target.get("accountId"):
            continue
        selection = target_selection(target)
        group = (selection.date, selection.dataset_id, selection.activity_id)
        candidates = [account for account in accounts
                      if account_activities.get(_account_activity(
                          account, selection), {}).get("status")
                      not in ("booked", "unknown")]
        if candidates:
            owner = min(candidates, key=lambda account: (load.get((group, account), 0),
                                                          accounts.index(account)))
            preferred[_target_key(target)] = owner
            load[(group, owner)] = load.get((group, owner), 0) + 1

    candidates_by_target = {}
    cached_by_target = {}
    lane_counts = {}
    for target in pending:
        key = _target_key(target)
        selection = target_selection(target)
        candidates = [account for account in _eligible_accounts(target, accounts)
                      if account_activities.get(_account_activity(account, selection), {}).get(
                          "status") not in ("booked", "unknown")]
        if not target.get("accountId") and preferred.get(key) in candidates:
            candidates.remove(preferred[key])
            candidates.insert(0, preferred[key])
        candidates_by_target[key] = candidates
        cached = state.get("prepared_courts", {}).get(key, {})
        ids = cached.get("facility_ids", []) if isinstance(cached, dict) else []
        excluded = explicit_courts.get(key, set()) if not selection.facility_id else set()
        cached_by_target[key] = list(dict.fromkeys(
            str(item) for item in ids if item and str(item) not in excluded
        ))
        lane_counts[key] = max(1, (len(cached_by_target[key]) + len(candidates) - 1)
                               // len(candidates)) if candidates else 1

    schedule_lock = threading.Lock()
    futures = []
    extra_pools = []

    def expand_lanes(raw, ids):
        key = _target_key(raw)
        candidates = candidates_by_target[key]
        if not candidates:
            return 1
        desired = max(1, (len(ids) + len(candidates) - 1) // len(candidates))
        with schedule_lock:
            previous = lane_counts[key]
            if desired > previous:
                lane_counts[key] = desired
                extra = [(raw, account, lane, desired)
                         for lane in range(previous, desired)
                         for index, account in enumerate(candidates)
                         if index + lane * len(candidates) < len(ids)]
                if extra:
                    pool = ThreadPoolExecutor(max_workers=len(extra))
                    extra_pools.append(pool)
                    futures.extend(pool.submit(worker, *job) for job in extra)
            return lane_counts[key]

    def worker(raw, account_id, lane=0, lanes=1):
        selection = target_selection(raw)
        key = _target_key(raw)
        base_key = _target_key({**raw, "facilityId": ""})
        activity_key = _account_activity(account_id, selection)
        stop = stops[activity_key]
        cached = state.setdefault("prepared_courts", {}).get(key, [])
        cached_ids = cached.get("facility_ids", []) if isinstance(cached, dict) else []
        if not isinstance(cached_ids, list):
            cached_ids = []
        cached_ids = list(dict.fromkeys(str(item) for item in cached_ids if item))
        excluded_explicit = explicit_courts.get(base_key, set()) if not selection.facility_id else set()
        candidates = candidates_by_target[key]

        def distribute(ids):
            ids = list(dict.fromkeys(
                str(item) for item in ids if item and str(item) not in excluded_explicit
            ))
            offset = candidates.index(account_id) + lane * len(candidates)
            return ids[offset:offset + 1]

        prepared_ids = distribute(cached_ids)
        reports = []
        entry = {
            "target": f"{selection.activity_name} {selection.center_name} "
                      f"{selection.date} {selection.start}-{selection.end}"
                      + (f" Area lane {lane + 1}" if lanes > 1 else ""),
            "target_key": key,
            "account_id": account_id,
            "attempts": 0, "events": [], "attempt_failures": [],
            "prepared_source": "cache" if prepared_ids else "timetable",
        }

        def next_court():
            entry["events"] = []
            entry["attempt_failures"] = []
            for field in ("first_step_started_at", "first_step_response_at",
                          "first_step_error_at", "first_step_timeout",
                          "final_submit_started_at"):
                entry.pop(field, None)

        def readiness(status, stage, reason="", court_ids=None):
            with state_lock:
                record = state["readiness"].setdefault(
                    _readiness_key(account_id, raw), {"run_id": run_id, "lanes": {}})
                previous = record["lanes"].get(str(lane), {})
                courts = len(court_ids) if court_ids is not None else previous.get("courts", 0)
                next_value = {
                    "status": status, "stage": stage, "reason_code": reason,
                    "courts": courts,
                    "at": datetime.now(TZ).isoformat(timespec="milliseconds"),
                }
                if any(previous.get(field) != next_value[field] for field in (
                    "status", "stage", "reason_code", "courts"
                )):
                    record["lanes"][str(lane)] = next_value
                    if not args.dry_run:
                        save_state(state)

        def valid_courts(client, ids):
            if not hasattr(client, "tree"):
                return ids
            return [court_id for court_id in ids if tree_context(
                client.tree, selection.dataset_id, selection.activity_id,
                selection.center_id, court_id,
            )["facility"] is not None]

        def prepare(client):
            nonlocal lanes
            table = client.timetable(
                selection.dataset_id, selection.activity_id, selection.center_id,
                selection.facility_id, selection.book_type,
            )
            slot = find_slot(table, selection.date, selection.start, selection.end)
            if slot is None:
                raise PossError("预备时目标时段不在官方时间表中")
            ids = list(dict.fromkeys(str(item) for item in slot["facilityIds"]
                                     if item and str(item) not in excluded_explicit))
            if selection.facility_id:
                ids = [selection.facility_id] if selection.facility_id in ids else []
            ids = valid_courts(client, ids)
            if not ids:
                raise PossError("目标时段没有可预备的球场编号")
            with state_lock:
                state["prepared_courts"][key] = {
                    "facility_ids": ids, "at": int(time.time()),
                }
                save_state(state)
            lanes = expand_lanes(raw, ids)
            prepared_ids[:] = distribute(ids)
            if lanes > 1:
                entry["target"] = (f"{selection.activity_name} {selection.center_name} "
                                   f"{selection.date} {selection.start}-{selection.end} "
                                   f"Area lane {lane + 1}")
            entry["prepared_source"] = "live"
            readiness("ready", "prepare", "courts_validated", prepared_ids)
            print(f"[warmup] {selection.center_name} {selection.start}-{selection.end} "
                  f"已预备 {len(ids)} 块球场（通道 {lane + 1}/{lanes}）", flush=True)

        def warmup_status(status, stage, reason):
            if status == "ready":
                with state_lock:
                    current = dict(state["readiness"].get(
                        _readiness_key(account_id, raw), {}).get("lanes", {}).get(str(lane), {}))
                if current.get("status") == "ready":
                    readiness("ready", stage, reason)
                elif current.get("status") == "cached" or current.get("courts"):
                    readiness("cached", stage, reason)
                else:
                    readiness("waiting", stage, reason)
                return
            try:
                valid = valid_courts(client, prepared_ids)
            except PossError:
                valid = []
            prepared_ids[:] = valid
            readiness("cached" if valid else "retrying", stage, reason, valid)

        readiness("checking", "login")
        try:
            client = (client_from_env(os.path.join(ROOT, ".env"))
                      if account_id == "1" else
                      client_from_env(os.path.join(ROOT, ".env"), account_id=account_id))
            started = wait_for_nearby_release(
                client, [raw], config, datetime.now(TZ), args.force, args.wait,
                on_ready=prepare, on_status=warmup_status,
            )
        except (PossError, requests.RequestException) as exc:
            entry.update(status="failed", stage="warmup_login",
                         reason_code=transport_reason(exc) if isinstance(
                             exc, requests.RequestException) else type(exc).__name__,
                         message=f"{type(exc).__name__}: 登录或保活失败")
            entry["attempt_failures"].append({
                "stage": "warmup_login", "reason_code": entry["reason_code"],
            })
            entry["release_lag_ms"] = round((datetime.now(TZ) - release).total_seconds() * 1000)
            readiness("failed", "warmup_login", entry["reason_code"])
            return [entry]
        if not args.force and started < release:
            return []
        with state_lock:
            known = state["prepared_courts"].get(key, {})
            known_ids = known.get("facility_ids", []) if isinstance(known, dict) else []
        if not prepared_ids and known_ids and not distribute(known_ids):
            entry.update(status="skipped", stage="prepared",
                         reason_code="no_assigned_court",
                         message="该通道没有分配到球场",
                         release_lag_ms=round((datetime.now(TZ) - release).total_seconds() * 1000))
            return [entry]
        deadline = time.monotonic() + (
            0 if args.dry_run else burst if args.force else max(
                0, (release + timedelta(seconds=burst) - started).total_seconds()
            )
        )
        while (entry["attempts"] == 0 or not stop.is_set()) and (
            entry["attempts"] == 0 or time.monotonic() < deadline
        ):
            with state_lock:
                done_ids = {
                    court_id for court_id in cached_ids
                    if state["campaigns"].get(_court_key(raw, court_id), {}).get("status")
                    in ("final_sent", "booked", "unknown")
                    or state["processed"].get(_court_key(raw, court_id), {}).get("status")
                    in ("booked", "unknown")
                }
                done_ids.update(
                    court_key.removeprefix(f"{base_key}|court:") for court_key, record
                    in campaigns.items() if court_key.startswith(f"{base_key}|court:")
                    and record.get("status") in ("final_sent", "booked", "unknown")
                )
                done_ids.update(excluded_explicit)
                if campaigns.get(key, {}).get("status") in ("final_sent", "booked", "unknown"):
                    done_ids.update(cached_ids)
                done_ids.update(
                    court_id for court_id in cached_ids
                    if _claimed_court(claims, selection, court_id)
                )
                excluded_ids = done_ids | {
                    court_key.removeprefix(f"{base_key}|court:")
                    for court_key in active_courts if court_key.startswith(f"{base_key}|court:")
                }
            prepared_ids[:] = [court_id for court_id in prepared_ids
                               if court_id not in excluded_ids]
            if lane > 0 and not prepared_ids:
                break
            entry["attempts"] += 1
            lag_ms = round((datetime.now(TZ) - release).total_seconds() * 1000)
            entry.setdefault("first_attempt_lag_ms", lag_ms)
            entry["release_lag_ms"] = lag_ms
            claimed = False
            prepared_id = prepared_ids.pop(0) if prepared_ids else ""
            current_id = prepared_id or selection.facility_id
            current_key = _court_key(raw, current_id) if current_id else key
            active_key = ""
            claim_key = ""
            if not prepared_id:
                entry["prepared_source"] = "timetable"

            def select_court(facility_id):
                nonlocal current_id, current_key, active_key, claim_key
                current_id = str(facility_id)
                current_key = _court_key(raw, current_id) if current_id else key
                with state_lock:
                    if (current_id in excluded_explicit
                            or current_key in active_courts
                            or _claimed_court(claims, selection, current_id)
                            or campaigns.get(current_key, {}).get("status")
                            in ("final_sent", "booked", "unknown")
                            or processed.get(current_key, {}).get("status")
                            in ("booked", "unknown")):
                        raise BookingAttemptError(
                            "该球场已有请求或预约，跳过重复请求",
                            "prepared", "court_already_attempted", [])
                    if not args.dry_run:
                        claim_key = (f"{selection.date}|{selection.center_id}|{current_id}|"
                                     f"{selection.start}|{selection.end}")
                        claims[claim_key] = {
                            "date": selection.date, "center_id": selection.center_id,
                            "facility_id": current_id, "start": selection.start,
                            "end": selection.end, "account_id": account_id,
                            "at": int(time.time()), "run_id": run_id,
                        }
                        save_state(state)
                    active_courts.add(current_key)
                    active_key = current_key

            def mark_first_step():
                emit({
                    "target": entry["target"], "status": "sending", "stage": "first_step",
                    "reason_code": "request_start",
                    "message": "第一步请求即将发送",
                    "release_lag_ms": lag_ms, "attempts": entry["attempts"],
                    "at": datetime.now(TZ).isoformat(timespec="milliseconds"),
                    "events": [{"stage": "first_step", "court_id": current_id,
                                "prepared_source": entry["prepared_source"]}],
                }, key, account_id)

            def mark_final_sent():
                nonlocal claimed
                with state_lock:
                    already_done = (
                        campaigns.get(current_key, {}).get("status")
                        in ("final_sent", "booked", "unknown")
                        or processed.get(current_key, {}).get("status")
                        in ("booked", "unknown")
                    )
                    if already_done or (entry["attempts"] > 1 and stop.is_set()):
                        raise BookingAttemptError(
                            "该球场已提交或结果待确认，停止重复提交",
                            "final_submit", "group_stopped", [],
                        )
                    campaigns[current_key] = {
                        "at": int(time.time()), "status": "final_sent",
                        "target": current_key, "stage": "final_submit",
                        "run_id": run_id, "account_id": account_id,
                    }
                    save_state(state)
                    claimed = True

            retry_after = None
            status = "unknown"
            try:
                outcome = client.book(
                    selection, dry_run=args.dry_run,
                    on_final_submit=None if args.dry_run else mark_final_sent,
                    prepared_facility_id=prepared_id,
                    on_first_step=None if args.dry_run else mark_first_step,
                    on_court_selected=None if args.dry_run else select_court,
                    excluded_facility_ids=excluded_ids,
                )
                status, stage, reason, message = (
                    outcome.status, outcome.stage,
                    outcome.reason_code or outcome.status, outcome.message,
                )
                entry["events"].extend(outcome.events)
            except BookingAttemptError as exc:
                status, stage, reason, message = (
                    "unknown" if exc.uncertain else "failed",
                    exc.stage, exc.reason_code, str(exc),
                )
                retry_after = exc.retry_after
                entry["events"].extend(exc.events)
            except (PossError, requests.RequestException) as exc:
                status, stage, reason, message = (
                    "unknown", "final_submit" if claimed else "runner",
                    "unexpected_error",
                    str(exc) if isinstance(exc, PossError)
                    else f"{type(exc).__name__}: POSS 网络请求失败",
                )
                entry["events"].extend(getattr(client, "last_attempt_events", []))
            except Exception as exc:  # noqa: BLE001
                status, stage, reason, message = (
                    "unknown", "final_submit" if claimed else "runner",
                    "internal_error", f"{type(exc).__name__}: 抢位流程异常",
                )
            except BaseException:
                if claimed:
                    stop.set()
                raise
            finally:
                if active_key:
                    with state_lock:
                        active_courts.discard(active_key)
                        if status == "failed" and claim_key and not claimed:
                            claims.pop(claim_key, None)
                            if not args.dry_run:
                                save_state(state)
            entry.update(status=status, stage=stage, reason_code=reason, message=message)
            entry["court_id"] = current_id
            booking = None
            if status == "booked":
                booking = {
                    "account_id": account_id,
                    "activity": selection.activity_name,
                    "center": outcome.center or selection.center_name,
                    "facility": outcome.facility or selection.facility_name or current_id,
                    "facility_id": current_id,
                    "date": selection.date,
                    "start": selection.start,
                    "end": selection.end,
                }
            if status == "failed":
                entry["attempt_failures"].append({
                    "stage": stage, "reason_code": reason, "court_id": current_id,
                })
            for event in entry["events"]:
                if event.get("stage") == "first_step" and event.get("state") == "sending":
                    entry.setdefault("first_step_started_at", event.get("at"))
                if event.get("stage") == "first_step" and "duration_ms" in event:
                    field = ("first_step_response_at" if "http_status" in event
                             else "first_step_error_at")
                    entry[field] = event.get("at")
                    entry["first_step_timeout"] = event.get("error") in ("ReadTimeout", "Timeout")
                if event.get("stage") == "final_submit" and event.get("state") == "sending":
                    entry.setdefault("final_submit_started_at", event.get("at"))
            final_uncertain = status == "unknown" and (
                claimed or stage == "final_submit"
            )
            if claimed:
                try:
                    with state_lock:
                        if status in ("booked", "unknown") or (
                            status == "failed" and reason == "unknown_result"
                        ):
                            campaigns[current_key] = {
                                "at": int(time.time()), "status": status,
                                "target": current_key, "reason_code": reason,
                                "stage": stage, "run_id": run_id, "account_id": account_id,
                                **({"booking": booking} if booking else {}),
                            }
                            if reason == "unknown_result":
                                processed[current_key] = {
                                    "at": int(time.time()), "status": "failed",
                                    "reason_code": reason, "stage": stage,
                                    "run_id": run_id, "account_id": account_id,
                                }
                        else:
                            campaigns.pop(current_key, None)
                            if claim_key:
                                claims.pop(claim_key, None)
                        if status == "booked" or final_uncertain:
                            if (status == "booked" or account_activities.get(
                                    activity_key, {}).get("status") != "booked"):
                                account_activities[activity_key] = {
                                    "at": int(time.time()),
                                    "status": "booked" if status == "booked" else "unknown",
                                    "court_key": current_key, "run_id": run_id,
                                }
                            stop.set()
                        save_state(state)
                except OSError:
                    stop.set()
                    raise
            if status == "booked" or final_uncertain:
                if status == "booked" or final_uncertain:
                    stop.set()
                if not args.dry_run:
                    with state_lock:
                        if not claimed:
                            campaigns[current_key] = {
                                "at": int(time.time()), "status": status,
                                "target": current_key, "reason_code": reason,
                                "stage": stage, "run_id": run_id, "account_id": account_id,
                                **({"booking": booking} if booking else {}),
                            }
                        if status == "booked" or final_uncertain:
                            if (status == "booked" or account_activities.get(
                                    activity_key, {}).get("status") != "booked"):
                                account_activities[activity_key] = {
                                    "at": int(time.time()),
                                    "status": "booked" if status == "booked" else "unknown",
                                    "court_key": current_key, "run_id": run_id,
                                }
                        processed[current_key] = {
                            "at": int(time.time()), "status": status,
                            "reason_code": reason, "stage": stage, "run_id": run_id,
                            "account_id": account_id,
                            **({"booking": booking} if booking else {}),
                        }
                        save_state(state)
                completed = dict(entry, events=list(entry["events"]))
                emit(completed, key, account_id)
                reports.append(completed)
                next_court()
                if stop.is_set() or (not prepared_ids and (
                    lane > 0 or not current_id or time.monotonic() >= deadline
                )):
                    break
                continue
            if status == "unknown":
                with state_lock:
                    if processed.get(current_key, {}).get("status") != "booked":
                        processed[current_key] = {
                            "at": int(time.time()), "status": "unknown",
                            "reason_code": reason, "stage": stage, "run_id": run_id,
                            "account_id": account_id,
                        }
                        save_state(state)
                completed = dict(entry, events=list(entry["events"]))
                emit(completed, key, account_id)
                reports.append(completed)
                next_court()
                if not current_id or selection.facility_id or (not prepared_ids and (
                    lane > 0 or time.monotonic() >= deadline
                )):
                    break
                continue
            if args.dry_run or reason == "group_stopped":
                break
            retryable = (
                (stage == "availability" and reason in ("no_vacancy", "slot_missing"))
                or (stage == "prepared" and reason == "request_rejected" and bool(prepared_id))
                or (stage == "prepared" and reason == "court_already_attempted")
                or (stage in ("timetable", "prepared") and reason in (
                    "network_error", "connect_timeout", "read_timeout", "timeout",
                    "tls_error", "tls_connection_closed", "tls_handshake_error",
                    "proxy_error", "rate_limited", "transient_http", "session_expired",
                ))
                or reason in ("slot_taken", "not_open")
            )
            if not retryable:
                break
            if stage == "prepared" and reason == "request_rejected":
                if lanes > 1 and lane > 0 and not prepared_ids:
                    break
                delay = 0 if prepared_ids else 0.3
            elif reason == "court_already_attempted":
                if lanes > 1 and lane > 0 and not prepared_ids:
                    break
                delay = 0 if prepared_ids else 0.5
            elif reason == "slot_taken":
                if lanes > 1 and lane > 0 and not prepared_ids:
                    break
                delay = 0 if prepared_ids else 0.5
            elif reason == "rate_limited" or retry_after is not None:
                delay = max(retry_interval, retry_after or 0)
            elif stage == "timetable":
                delay = random.uniform(0.3, min(3.0, 0.8 * 2 ** min(entry["attempts"] - 1, 3)))
            else:
                delay = retry_interval
            if time.monotonic() + delay >= deadline:
                break
            time.sleep(delay)
        if not entry["attempts"]:
            entry.update(status="skipped", stage="coordination",
                         reason_code="other_target_finished",
                         message="同一目标的另一球场已成功或结果待确认",
                         release_lag_ms=round((datetime.now(TZ) - release).total_seconds() * 1000))
        if not reports or entry.get("status") not in ("booked", "unknown"):
            reports.append(entry)
        return reports

    jobs = []
    for account_id in accounts:
        for target in pending:
            key = _target_key(target)
            candidates = candidates_by_target[key]
            if account_id not in candidates:
                continue
            lanes = lane_counts[key]
            jobs.append((target, account_id, 0, lanes))
            jobs.extend((target, account_id, lane, lanes)
                        for lane in range(1, lanes)
                        if candidates.index(account_id) + lane * len(candidates)
                        < len(cached_by_target[key]))
    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        with schedule_lock:
            futures.extend(executor.submit(worker, *job) for job in jobs)
        results = []
        seen = set()
        try:
            while True:
                with schedule_lock:
                    batch = [future for future in futures if future not in seen]
                    seen.update(batch)
                if not batch:
                    break
                results.extend(future.result() for future in batch)
        finally:
            for pool in extra_pools:
                pool.shutdown(wait=True)
    reports = [report for result in results for report in result]
    for report in reports:
        if report["status"] not in ("booked", "unknown"):
            emit(report, report["target_key"], report["account_id"])
    if not args.dry_run and reports:
        notified = state.setdefault("failure_notified", {})
        target_by_key = {_target_key(target): target_selection(target) for target in pending}
        for account_id in accounts:
            for activity_key in {_account_activity(account_id, target_selection(target))
                                 for target in pending
                                 if account_id in _eligible_accounts(target, accounts)}:
                own = [report for report in reports if report["account_id"] == account_id
                       and _account_activity(account_id, target_by_key[report["target_key"]])
                       == activity_key]
                failure_key = f"{group_key}|{activity_key}"
                if (not own or account_activities.get(activity_key, {}).get("status") == "booked"
                        or failure_key in notified):
                    continue
                reasons = "; ".join(
                    f"{report['target']}: {report['reason_code']}" for report in own
                )
                activity_name = target_by_key[own[0]["target_key"]].activity_name
                if notify(f"POSS 账号 {account_id} {activity_name} 抢位待确认/未抢到", reasons):
                    notified[failure_key] = int(time.time())
                    save_state(state)
    if not args.dry_run:
        state["active_run"]["finished_at"] = int(time.time())
        save_state(state)


def run(args):
    config = load_config()
    if not config.get("booking", {}).get("enabled", True):
        print("booking.enabled=false，跳过抢位")
        return
    targets = config.get("booking", {}).get("targets") or []
    if getattr(args, "today_only", False):
        today = datetime.now(TZ).date()
        targets = [target for target in targets
                   if release_at(target, config).date() == today]
    if not targets:
        print("今天没有放位目标" if getattr(args, "today_only", False)
              else "没有配置 booking.targets；请先在 poss_ui.py 选择目标")
        return
    state = load_state()
    groups = {}
    for target in targets:
        group = groups.setdefault(release_at(target, config), {})
        group[_target_key(target)] = target
    for _, group in sorted(groups.items()):
        run_parallel_group(list(group.values()), config, state, args)


def main():
    parser = argparse.ArgumentParser(description="POSS monitor")
    parser.add_argument("--dry-run", action="store_true", help="只验证目标，不提交预订")
    parser.add_argument("--force", action="store_true", help="忽略放位时间和已处理状态")
    parser.add_argument("--wait", action="store_true",
                        help="本地测试用：提前登录并保活，等待目标放位时间")
    parser.add_argument("--today-only", action="store_true",
                        help="只处理今天放位的目标，供每日云端运行使用")
    args = parser.parse_args()
    os.makedirs(os.path.join(ROOT, "dumps"), exist_ok=True)
    if os.name == "nt":
        lock_path = os.path.join(ROOT, "dumps", "booking-runner.lock")
        with open(lock_path, "a+b") as lock:
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                print("本地抢位程序已在运行，跳过重复实例", flush=True)
                return
            pid_path = os.path.join(ROOT, "dumps", "booking-local.pid")
            with open(pid_path, "w", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}\n")
            try:
                run(args)
            finally:
                try:
                    os.unlink(pid_path)
                except FileNotFoundError:
                    pass
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        return
    with open(os.path.join(ROOT, "dumps", "booking-runner.lock"), "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("本地抢位程序已在运行，跳过重复实例", flush=True)
            return
        run(args)


if __name__ == "__main__":
    main()
