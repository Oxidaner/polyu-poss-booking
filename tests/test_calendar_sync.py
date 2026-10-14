import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

import calendar_sync
import record_sync
from poss_client import (PossClient, PossError, SessionExpired, active_record_bookings,
                         parse_record_page, record_page_before)


def page(rows, next_page=False):
    body = "".join(
        f'<tr><td>{at}</td><td><span>{venue}</span><br/>'
        f'<span>{slot}</span></td><td>Individual Booking {kind}</td>'
        '<td></td><td></td><td></td><td></td></tr>'
        for at, venue, slot, kind in rows
    )
    disabled = "" if next_page else ' disabled="disabled"'
    return ('<table><tr><th>Transaction Date</th><th>Booking</th>'
            '<th>Transaction type</th></tr>' + body + '</table>'
            f'<button name="next"{disabled}>Next</button>')


VENUE = "Shaw Sports Complex BMT05 - Badminton Court No. 5"
SLOT = "21 Oct 2026 (Wed) 01:30 PM - 02:30 PM"


class RecordsTest(unittest.TestCase):
    def test_pagination_and_latest_transaction_wins(self):
        first = page([
            ("14 Oct 2026 08:33 AM", VENUE, SLOT, "Refund (Cancelled)"),
            ("14 Oct 2026 08:32 AM", "Kwong Sports Centre BMT01 - Badminton Court No. 1",
             SLOT, "Transfer To (Confirmed)"),
        ], next_page=True)
        second = page([
            ("14 Oct 2026 08:30 AM", VENUE, SLOT, "(Confirmed)"),
            ("14 Oct 2026 08:29 AM", "Kwong Sports Centre BMT01 - Badminton Court No. 1",
             SLOT, "Transfer From (Transfered)"),
        ])
        with patch("poss_client.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 15)
            clock.strptime = datetime.strptime
            result = active_record_bookings([first, second])
        self.assertEqual(1, len(result))
        self.assertIn("Kwong", result[0]["center"])
        self.assertEqual("Badminton Court No. 1", result[0]["facility"])
        self.assertNotIn("status", result[0])

    def test_unknown_layout_and_partial_pages_fail_closed(self):
        with self.assertRaises(PossError):
            parse_record_page("<html>temporarily unavailable</html>")
        c = PossClient("demo", "demo")
        responses = [
            SimpleNamespace(text=page([("14 Oct 2026 08:30 AM", VENUE, SLOT,
                                        "(Confirmed)")], next_page=True),
                            url="https://www40.polyu.edu.hk/record",
                            raise_for_status=lambda: None),
            SimpleNamespace(text="<html>broken</html>",
                            url="https://www40.polyu.edu.hk/record",
                            raise_for_status=lambda: None),
        ]
        offsets = []
        class Session:
            def get(self, _url, **kwargs):
                offsets.append(kwargs["params"]["txFirstRecord"])
                return responses.pop(0)
        c.session = Session()
        with self.assertRaises(PossError):
            c.record_bookings()
        self.assertEqual([0, 10], offsets)

    def test_old_transaction_page_cannot_contain_a_future_booking(self):
        old = page([("1 Sep 2026 08:30 AM", VENUE,
                     "8 Sep 2026 (Tue) 01:30 PM - 02:30 PM",
                     "Unexpected Legacy Status")], next_page=True)
        self.assertTrue(record_page_before(old, date(2026, 9, 30)))
        self.assertFalse(record_page_before(old, date(2026, 8, 30)))

    def test_login_expiry_rejected(self):
        c = PossClient("demo", "demo")
        c.session.get = lambda *_args, **_kwargs: SimpleNamespace(
            text='<input name="j_username">', url="https://www40.polyu.edu.hk/loginhome",
        )
        with self.assertRaises(SessionExpired):
            c.record_bookings()

    def test_per_account_failure_preserves_only_its_prior_data(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bookings.json"
            path.write_text('{"accounts":{"1":{"status":"ok","bookings":[{"venue":"old"}]},'
                            '"2":{"status":"ok","bookings":[]}}}')
            class FakeClient:
                def __init__(self, account):
                    self.account = account
                def login(self):
                    if self.account == "1":
                        raise PossError("login failed")
                def record_bookings(self):
                    return [{"venue": "new"}]
            with patch.object(record_sync, "account_ids_from_env", return_value=["1", "2"]), \
                    patch.object(record_sync, "client_from_env",
                                 side_effect=lambda _path, account: FakeClient(account)):
                self.assertFalse(record_sync.refresh(path))
            data = json.loads(path.read_text())["accounts"]
            self.assertEqual([{"venue": "old"}], data["1"]["bookings"])
            self.assertEqual("error", data["1"]["status"])
            self.assertEqual([{"venue": "new"}], data["2"]["bookings"])
            self.assertEqual("ok", data["2"]["status"])


class CalendarTest(unittest.TestCase):
    def test_direct_read_uses_only_account_one(self):
        rows = [{
            "date": "2026-10-21", "start": "13:30", "end": "14:30",
            "venue": VENUE, "center": "Shaw Sports Complex",
            "facility": "Badminton Court No. 5",
        }]
        with patch.object(calendar_sync, "client_from_env") as client:
            client.return_value.record_bookings.return_value = rows
            self.assertEqual(rows, calendar_sync.local_bookings())
            client.assert_called_once_with(str(calendar_sync.ROOT / ".env"), "1")
            client.return_value.login.assert_called_once()
        events = calendar_sync.confirmed_bookings(rows)
        self.assertEqual(1, len(events))
        event = next(iter(events.values()))
        self.assertEqual("1", event["account"])
        args = calendar_sync.event_args(event, "POSS 预约")
        self.assertEqual(16, len(args))
        self.assertIn(event["marker"], args[3])
        with patch.object(calendar_sync, "local_bookings", return_value=rows), \
                patch.object(calendar_sync, "add_event") as add, \
                patch.object(calendar_sync, "prune_events", return_value=0) as prune:
            calendar_sync.sync_once("POSS 预约", dry_run=True)
            add.assert_not_called()
            prune.assert_not_called()
            calendar_sync.sync_once("POSS 预约")
            add.assert_called_once()
            prune.assert_called_once_with("POSS 预约", [event["marker"]])

    def test_incomplete_direct_read_retries_without_changing_calendar(self):
        with patch.object(calendar_sync, "client_from_env") as client, \
                patch.object(calendar_sync.time, "sleep") as sleep, \
                patch.object(calendar_sync, "add_event") as add, \
                patch.object(calendar_sync, "prune_events") as prune:
            client.return_value.record_bookings.side_effect = requests.ReadTimeout()
            with self.assertRaises(requests.ReadTimeout):
                calendar_sync.sync_once("POSS 预约")
            self.assertEqual(3, client.call_count)
            self.assertEqual(2, sleep.call_count)
            add.assert_not_called()
            prune.assert_not_called()


if __name__ == "__main__":
    unittest.main()
