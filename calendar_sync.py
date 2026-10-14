#!/usr/bin/env python3
"""Mirror confirmed POSS bookings into a dedicated macOS Calendar calendar."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
HONG_KONG = ZoneInfo("Asia/Hong_Kong")
CALENDAR_NAME = "POSS 预约"

# The marker lives in Calendar, so restarts and local state resets cannot duplicate events.
ADD_EVENT = """
on run argv
    set calendarName to item 1 of argv
    set eventTitle to item 2 of argv
    set eventLocation to item 3 of argv
    set eventDescription to item 4 of argv
    set startDate to my makeDate(items 5 thru 9 of argv)
    set endDate to my makeDate(items 10 thru 14 of argv)
    set eventMarker to item 15 of argv
    tell application "Calendar"
        set matchingCalendars to every calendar whose name is calendarName
        if (count of matchingCalendars) is not 1 then error "需要一个名为 " & calendarName & " 的日历（且名称唯一）"
        set destination to item 1 of matchingCalendars
        tell destination
            if (count of (every event whose description contains eventMarker)) > 0 then
                return "exists"
            end if
            make new event with properties {summary:eventTitle, start date:startDate, end date:endDate, location:eventLocation, description:eventDescription}
        end tell
    end tell
    return "created"
end run

on makeDate(parts)
    set resultDate to current date
    set day of resultDate to 1
    set year of resultDate to (item 1 of parts as integer)
    set month of resultDate to (item 2 of parts as integer)
    set day of resultDate to (item 3 of parts as integer)
    set time of resultDate to ((item 4 of parts as integer) * hours + (item 5 of parts as integer) * minutes)
    return resultDate
end makeDate
"""


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=90,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def remote_data() -> tuple[dict, dict]:
    git("fetch", "--quiet", "origin", "main")
    revision = git("rev-parse", "refs/remotes/origin/main")
    config = json.loads(git("show", f"{revision}:config.json"))
    try:
        state = json.loads(git("show", f"{revision}:state.json"))
    except RuntimeError as exc:
        if "does not exist in" not in str(exc) and "exists on disk, but not in" not in str(exc):
            raise
        state = {}
    return state, config


def _target_details(parts: list[str], config: dict) -> tuple[str, str, str]:
    fallback = ("", "", "")
    for target in config.get("booking", {}).get("targets", []):
        identity = [
            str(target.get("datasetId") or "18"), str(target.get("activityId") or ""),
            str(target.get("centerId") or ""), str(target.get("facilityId") or ""),
            str(target.get("date") or ""), str(target.get("from") or target.get("start") or ""),
            str(target.get("to") or target.get("end") or ""), str(target.get("bookType") or "INDV"),
        ]
        labels = (str(target.get("activity") or target.get("activityName") or ""),
                  str(target.get("center") or target.get("centerName") or ""),
                  str(target.get("facility") or target.get("facilityName") or ""))
        if identity == parts[:8]:
            return labels
        if identity[:3] == parts[:3]:
            fallback = labels
    return fallback


def confirmed_bookings(state: dict, config: dict) -> dict[str, dict]:
    bookings = {}
    for section in ("processed", "campaigns"):
        for key, row in state.get(section, {}).items():
            if row.get("status") != "booked":
                continue
            target_key = row.get("target") or key
            parts = target_key.split("|")
            if len(parts) not in (8, 9):
                continue
            account = str(row.get("account_id") or row.get("booking", {}).get("account_id") or "1")
            marker = "POSS-ID:" + hashlib.sha256(
                f"{account}|{target_key}".encode("utf-8")
            ).hexdigest()[:24]
            activity, center, facility = _target_details(parts, config)
            snapshot = row.get("booking") or {}
            court = parts[8].removeprefix("court:") if len(parts) == 9 else parts[3]
            bookings[marker] = {
                "marker": marker,
                "account": account,
                "activity": snapshot.get("activity") or activity or f"Activity {parts[1]}",
                "center": snapshot.get("center") or center or f"Center {parts[2]}",
                "facility": snapshot.get("facility") or facility or court,
                "court_id": snapshot.get("facility_id") or court,
                "date": snapshot.get("date") or parts[4],
                "start": snapshot.get("start") or parts[5],
                "end": snapshot.get("end") or parts[6],
            }
    return bookings


def event_args(booking: dict, calendar_name: str) -> list[str]:
    start = datetime.fromisoformat(f"{booking['date']}T{booking['start']}").replace(
        tzinfo=HONG_KONG
    )
    end = datetime.fromisoformat(f"{booking['date']}T{booking['end']}").replace(
        tzinfo=HONG_KONG
    )
    if end <= start:
        end += timedelta(days=1)
    local_start = start.astimezone()
    local_end = end.astimezone()
    description = (f"POSS 已确认预约\n账号：{booking['account']}\n"
                   f"球场编号：{booking['court_id']}\n{booking['marker']}")
    return [
        calendar_name, f"{booking['activity']} · {booking['center']} · {booking['facility']}",
        booking["center"], description,
        *[str(value) for value in (local_start.year, local_start.month, local_start.day,
                                   local_start.hour, local_start.minute)],
        *[str(value) for value in (local_end.year, local_end.month, local_end.day,
                                   local_end.hour, local_end.minute)],
        booking["marker"],
    ]


def add_event(booking: dict, calendar_name: str) -> str:
    result = subprocess.run(
        ["osascript", "-", *event_args(booking, calendar_name)],
        input=ADD_EVENT, capture_output=True, text=True, timeout=90,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip().splitlines()[-1]


def sync_once(calendar_name: str, dry_run: bool = False) -> None:
    state, config = remote_data()
    bookings = {}
    local_state = ROOT / "state.json"
    if local_state.exists():
        with local_state.open(encoding="utf-8") as handle:
            bookings.update(confirmed_bookings(json.load(handle), config))
    bookings.update(confirmed_bookings(state, config))
    if not bookings:
        print("没有已确认预约", flush=True)
    for booking in bookings.values():
        if dry_run:
            print(f"待同步：{booking['date']} {booking['start']}-{booking['end']} "
                  f"{booking['activity']} {booking['center']} "
                  f"{booking['facility']} · 账号 {booking['account']}", flush=True)
        else:
            result = add_event(booking, calendar_name)
            print(f"{result}: {booking['date']} {booking['activity']} "
                  f"{booking['center']} · 账号 {booking['account']}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="将 POSS 已确认预约同步到 Mac 日历")
    parser.add_argument("--calendar", default=CALENDAR_NAME, help="已创建的 iCloud 日历名称")
    parser.add_argument("--watch", action="store_true", help="每三分钟同步一次")
    parser.add_argument("--dry-run", action="store_true", help="只显示待同步的预约")
    args = parser.parse_args()
    while True:
        try:
            sync_once(args.calendar, args.dry_run)
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            print(f"日历同步失败：{exc}", file=sys.stderr, flush=True)
            if not args.watch:
                raise SystemExit(1) from exc
        if not args.watch:
            break
        time.sleep(180)


if __name__ == "__main__":
    main()
