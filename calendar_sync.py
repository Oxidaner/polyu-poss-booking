#!/usr/bin/env python3
"""Mirror confirmed POSS bookings into a dedicated macOS Calendar calendar."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
HONG_KONG = ZoneInfo("Asia/Hong_Kong")
CALENDAR_NAME = "POSS 预约"

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
            repeat with oldEvent in (every event whose start date is startDate)
                if (end date of oldEvent is endDate) and (description of oldEvent contains "账号：1") and (summary of oldEvent contains item 16 of argv) then
                    set description of oldEvent to (description of oldEvent) & return & eventMarker
                    return "adopted"
                end if
            end repeat
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

PRUNE_EVENTS = """
on run argv
    set calendarName to item 1 of argv
    set validMarkers to {}
    if (count of argv) > 1 then set validMarkers to items 2 thru -1 of argv
    set removed to 0
    tell application "Calendar"
        set matchingCalendars to every calendar whose name is calendarName
        if (count of matchingCalendars) is not 1 then error "需要一个名为 " & calendarName & " 的日历（且名称唯一）"
        tell item 1 of matchingCalendars
            repeat with oldEvent in (every event)
                set oldDescription to description of oldEvent
                if oldDescription contains "POSS-ID:" and (start date of oldEvent) >= (current date) then
                    set keepEvent to false
                    repeat with marker in validMarkers
                        if oldDescription contains (contents of marker) then set keepEvent to true
                    end repeat
                    if not keepEvent then
                        delete oldEvent
                        set removed to removed + 1
                    end if
                end if
            end repeat
        end tell
    end tell
    return removed
end run
"""


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=90,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def remote_data() -> dict:
    git("fetch", "--quiet", "origin", "main")
    revision = git("rev-parse", "refs/remotes/origin/main")
    return json.loads(git("show", f"{revision}:bookings.json"))


def confirmed_bookings(snapshot: dict) -> dict[str, dict]:
    bookings = {}
    account = snapshot.get("accounts", {}).get("1", {})
    if account.get("status") != "ok":
        raise ValueError("账号 1 的 My Record 尚未成功完成整页核实")
    for row in account["bookings"]:
        marker = "POSS-ID:" + hashlib.sha256(
            f"1|{row['date']}|{row['start']}|{row['end']}|{row['venue'].casefold()}".encode()
        ).hexdigest()[:24]
        bookings[marker] = {**row, "marker": marker, "account": "1"}
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
    description = f"POSS 已确认预约\n账号：1\n{booking['marker']}"
    return [
        calendar_name, f"POSS · {booking['venue']}",
        booking["center"], description,
        *[str(value) for value in (local_start.year, local_start.month, local_start.day,
                                   local_start.hour, local_start.minute)],
        *[str(value) for value in (local_end.year, local_end.month, local_end.day,
                                   local_end.hour, local_end.minute)],
        booking["marker"], booking["facility"],
    ]


def add_event(booking: dict, calendar_name: str) -> str:
    result = subprocess.run(
        ["osascript", "-", *event_args(booking, calendar_name)],
        input=ADD_EVENT, capture_output=True, text=True, timeout=90,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip().splitlines()[-1]


def prune_events(calendar_name: str, markers: list[str]) -> int:
    result = subprocess.run(
        ["osascript", "-", calendar_name, *markers],
        input=PRUNE_EVENTS, capture_output=True, text=True, timeout=90,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return int(result.stdout.strip().splitlines()[-1])


def sync_once(calendar_name: str, dry_run: bool = False) -> None:
    bookings = confirmed_bookings(remote_data())
    if not bookings:
        print("账号 1 当前没有待同步的有效预约", flush=True)
    for booking in bookings.values():
        if dry_run:
            print(f"待同步：{booking['date']} {booking['start']}-{booking['end']} "
                  f"{booking['venue']} · 账号 1", flush=True)
        else:
            result = add_event(booking, calendar_name)
            print(f"{result}: {booking['date']} {booking['venue']} · 账号 1", flush=True)
    if not dry_run:
        print(f"移除 {prune_events(calendar_name, list(bookings))} 条过期日历预约", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="将 POSS 已确认预约同步到 Mac 日历")
    parser.add_argument("--calendar", default=CALENDAR_NAME, help="已创建的 iCloud 日历名称")
    parser.add_argument("--dry-run", action="store_true", help="只显示待同步的预约")
    args = parser.parse_args()
    try:
        sync_once(args.calendar, args.dry_run)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"日历同步失败：{exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
