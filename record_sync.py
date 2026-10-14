#!/usr/bin/env python3
"""Refresh the repository's booking list from each account's POSS My Record."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import requests

from poss_client import TZ, PossError, account_ids_from_env, client_from_env

ROOT = Path(__file__).resolve().parent
LIST_PATH = ROOT / "bookings.json"


def refresh(path: Path = LIST_PATH) -> bool:
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    accounts = dict(previous.get("accounts", {}))
    ids = account_ids_from_env(str(ROOT / ".env"))
    if not ids:
        raise PossError("未配置任何 POSS 账号")
    failed = False
    for account_id in ids:
        try:
            client = client_from_env(str(ROOT / ".env"), account_id)
            client.login()
            bookings = client.record_bookings()
            accounts[account_id] = {
                "status": "ok",
                "checked_at": datetime.now(TZ).isoformat(timespec="seconds"),
                "bookings": bookings,
            }
            print(f"账号 {account_id}: 从 My Record 核实 {len(bookings)} 条有效预约", flush=True)
        except (PossError, requests.RequestException, ValueError) as exc:
            failed = True
            old = accounts.get(account_id, {})
            accounts[account_id] = {
                **old, "status": "error",
                "error": type(exc).__name__,
                "error_at": datetime.now(TZ).isoformat(timespec="seconds"),
            }
            print(f"账号 {account_id}: My Record 同步失败 ({type(exc).__name__})，"
                  "保留上次可信列表", file=sys.stderr, flush=True)
    # Drop accounts no longer configured, so removed credentials cannot leave stale bookings.
    data = {"version": 1, "accounts": {
        account_id: accounts[account_id] for account_id in ids
    }}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return not failed


if __name__ == "__main__":
    try:
        success = refresh()
    except (PossError, OSError, ValueError) as exc:
        print(f"My Record 同步失败: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from exc
    raise SystemExit(0 if success else 1)
