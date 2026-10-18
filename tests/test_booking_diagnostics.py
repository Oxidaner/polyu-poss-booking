import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import requests

import monitor
import poss_ui
from poss_client import (
    BookingAttemptError, BookingOutcome, CONFIRMATION_TIMEOUT, FINAL_TIMEOUT,
    LoginError, PossClient, Selection, SessionExpired,
    TimetableError, parse_deposit_balance,
)


SELECTION = Selection(
    "18", "101", "152", "1601", "2026-10-14", "08:30", "09:30",
    "Fitness Training", "Block X - X202 Fitness Room", "X202 Fitness Room",
)

ALTERNATIVES = [
    {"datasetId": "18", "activityId": "2", "centerId": center_id,
     "facilityId": "", "date": "2026-10-16", "from": "15:30", "to": "16:30",
     "activity": "Badminton", "center": name}
    for center_id, name in (
        ("3", "Kwong"), ("1", "Shaw"), ("51", "Shaw Practice Hall"),
    )
]


def table(available=True):
    return {
        "columns": [{"date": SELECTION.date, "slots": [{
            "date": SELECTION.date, "start": SELECTION.start, "end": SELECTION.end,
            "left": int(available), "facilityIds": ["1601"],
            "availableFacilityIds": ["1601"] if available else [],
            "charge": 0, "refundableFee": 3,
        }]}],
    }


class Response:
    def __init__(self, status, text="", location="", retry_after=""):
        self.status_code = status
        self.text = text
        self.headers = {"Location": location} if location else {}
        if retry_after:
            self.headers["Retry-After"] = retry_after
        self.url = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = 0

    def post(self, *_args, **_kwargs):
        self.posts += 1
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def get(self, *_args, **_kwargs):
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def client(available=True, responses=()):
    c = PossClient("demo", "demo")
    c.csrf = "test-token"
    c.tree = [{"value": "18", "label": "Sports Facility", "children": [
        {"value": "101", "label": "Fitness Training", "children": [
            {"value": "152", "label": SELECTION.center_name, "children": [
                {"value": "1601", "label": "XFIT (X202) - X202 Fitness Room",
                 "children": []},
            ]},
        ]},
    ]}]
    c.timetable = lambda *_args: table(available)
    c._selection_context = lambda _selection: {
        "center": {"label": SELECTION.center_name},
        "facility": {"label": "XFIT (X202) - X202 Fitness Room"},
        "activity": {"label": SELECTION.activity_name},
    }
    c.preview_booking = lambda _selection, t: {
        "slot": t["columns"][0]["slots"][0],
        "facilityId": "1601",
        "context": {"center": SELECTION.center_name, "facility": SELECTION.facility_name},
        "request": {"fields": {"CSRFToken": "test-token"}},
    }
    c.session = Session(responses)
    return c


class BookingDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        account_ids = patch.object(monitor, "account_ids_from_env", return_value=["1"])
        account_ids.start()
        self.addCleanup(account_ids.stop)

    def test_deposit_balance_reads_booking_page_and_falls_back_to_deposit_page(self):
        self.assertEqual("104.00", parse_deposit_balance(
            '<div>Deposit Balance: <span>$104.00</span></div>'))
        self.assertEqual("1234.50", parse_deposit_balance(
            '<div>Current Deposit Balance: <span>HK$1,234.50</span></div>'))
        self.assertIsNone(parse_deposit_balance("<div>Deposit Balance: unavailable</div>"))

        c = PossClient("demo", "demo")
        c.page_html = "<div>Deposit Balance: $0.00</div>"
        c.session = Session([])
        self.assertEqual({"amount": "0.00", "currency": "HKD"}, c.deposit_balance())
        self.assertEqual([], c.session.responses)

        c.page_html = "<div>No balance here</div>"
        c.session = Session([Response(200, "<div>Current Deposit Balance: $42.50</div>")])
        self.assertEqual({"amount": "42.50", "currency": "HKD"}, c.deposit_balance())
        self.assertIsNone(parse_deposit_balance("<div>Make Deposit $500</div>"))

    def test_account_verification_is_independent_and_never_exposes_passwords(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(poss_ui, "ROOT", directory), \
                patch.dict(poss_ui._account_checks, clear=True):
            poss_ui.write_env({"accounts": [
                {"id": "1", "netid": "one", "password": "secret-one"},
                {"id": "2", "netid": "two", "password": "secret-two"},
                {"id": "3", "netid": "three", "password": "secret-three"},
                {"id": "4", "netid": "four", "password": "secret-four"},
            ]})

            class FakeClient:
                def __init__(self, slot):
                    self.slot = slot

                def login(self):
                    if self.slot == "2":
                        raise LoginError("bad credentials")

                def deposit_balance(self):
                    if self.slot == "3":
                        return None
                    if self.slot == "4":
                        raise ValueError("unexpected secret from page")
                    return {"amount": "104.00", "currency": "HKD"}

            with patch.object(poss_ui, "client_from_env",
                              side_effect=lambda _path, account_id: FakeClient(account_id)):
                rows = poss_ui.verify_accounts()["accounts"]
            self.assertEqual("verified", rows[0]["check"]["status"])
            self.assertEqual("104.00", rows[0]["check"]["balance"]["amount"])
            self.assertEqual("login_failed", rows[1]["check"]["status"])
            self.assertEqual("login_rejected", rows[1]["check"]["reasonCode"])
            self.assertEqual("balance_unknown", rows[2]["check"]["status"])
            self.assertNotIn("balance", rows[2]["check"])
            self.assertEqual("balance_error", rows[3]["check"]["status"])
            self.assertEqual("verification_error", rows[3]["check"]["reasonCode"])
            self.assertNotIn("secret-", json.dumps(rows))
            self.assertNotIn("bad credentials", json.dumps(rows))
            self.assertNotIn("unexpected secret", json.dumps(rows))

    def test_failed_state_replace_preserves_final_submit_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            original = {"processed": {"3|2026-10-16|15:30": {
                "status": "unknown", "reason_code": "final_sent",
            }}}
            path.write_text(json.dumps(original))
            with patch.object(monitor, "STATE_FILE", str(path)), \
                    patch.object(monitor.os, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    monitor.save_state({"processed": {}})
            self.assertEqual(original, json.loads(path.read_text()))
            self.assertEqual(["state.json"], [item.name for item in Path(directory).iterdir()])

    def test_parallel_targets_start_together_with_separate_clients(self):
        cfg = {"booking": {"enabled": True, "targets": ALTERNATIVES,
                           "burst_seconds": 0, "retry_interval": 3}}
        state, calls, reports, clients = {"processed": {}}, [], [], []
        barrier = threading.Barrier(3)

        class FakeClient:
            def __init__(self):
                clients.append(self)

            def book(self, selection, **_kwargs):
                barrier.wait(timeout=2)
                calls.append(selection.center_id)
                if selection.center_id == "3":
                    return BookingOutcome("booked", "My Record confirmed",
                                          reason_code="confirmed")
                raise BookingAttemptError("no courts", "availability", "no_vacancy", [])

        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt", side_effect=reports.append), \
                patch.object(monitor, "notify"), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))
        self.assertEqual({"3", "1", "51"}, set(calls))
        self.assertEqual(3, len(clients))
        self.assertEqual("booked", next(iter(state["campaigns"].values()))["status"])
        self.assertEqual(1, len([r for r in reports if r["status"] == "booked"]))

    def test_cached_courts_allocate_one_parallel_lane_per_court_across_accounts(self):
        target = ALTERNATIVES[0]
        courts = [f"court-{index}" for index in range(7)]
        cfg = {"booking": {"enabled": True, "targets": [target], "burst_seconds": 0}}
        release = monitor.release_at(target, cfg)
        state = {"processed": {}, "prepared_courts": {
            monitor._target_key(target): {"facility_ids": courts, "at": 1},
        }}
        barrier = threading.Barrier(len(courts))
        submitted = []

        class FakeClient:
            def __init__(self, account):
                self.account = account

            def book(self, selection, prepared_facility_id="", on_court_selected=None,
                     on_final_submit=None, **_kwargs):
                on_court_selected(prepared_facility_id)
                on_final_submit()
                submitted.append((self.account, prepared_facility_id))
                barrier.wait(timeout=2)
                return BookingOutcome("failed", "unrecognized", stage="final_submit",
                                      reason_code="unknown_result")

        with patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "wait_for_nearby_release",
                             side_effect=lambda _client, _targets, _cfg, now, *_a, **_kw: now), \
                patch.object(monitor, "account_ids_from_env", return_value=["1", "2"]), \
                patch.object(monitor, "client_from_env",
                             side_effect=lambda *_a, **kw: FakeClient(kw.get("account_id", "1"))), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"):
            monitor.run_parallel_group(
                [target], cfg, state, SimpleNamespace(force=False, dry_run=False, wait=True))
        self.assertEqual(set(courts), {court for _, court in submitted})
        self.assertEqual(7, len(submitted))
        self.assertEqual({4, 3}, {sum(account == slot for account, _ in submitted)
                                 for slot in ("1", "2")})

    def test_live_preflight_adds_lanes_before_release_without_cached_courts(self):
        target = ALTERNATIVES[0]
        courts = [f"live-{index}" for index in range(5)]
        cfg = {"booking": {"enabled": True, "targets": [target], "burst_seconds": 0}}
        release = monitor.release_at(target, cfg)
        state, submitted = {"processed": {}}, []
        barrier = threading.Barrier(len(courts))

        class FakeClient:
            def __init__(self, account):
                self.account = account

            def timetable(self, *_args):
                return {"columns": [{"slots": [{
                    "date": target["date"], "start": target["from"], "end": target["to"],
                    "facilityIds": courts,
                }]}]}

            def book(self, selection, prepared_facility_id="", on_court_selected=None,
                     on_final_submit=None, **_kwargs):
                on_court_selected(prepared_facility_id)
                on_final_submit()
                submitted.append((self.account, prepared_facility_id))
                barrier.wait(timeout=2)
                return BookingOutcome("failed", "unrecognized", stage="final_submit",
                                      reason_code="unknown_result")

        def ready(client, _targets, _cfg, now, *_args, on_ready=None, **_kwargs):
            on_ready(client)
            return now

        with patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "wait_for_nearby_release", side_effect=ready), \
                patch.object(monitor, "account_ids_from_env", return_value=["1", "2"]), \
                patch.object(monitor, "client_from_env",
                             side_effect=lambda *_a, **kw: FakeClient(kw.get("account_id", "1"))), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"):
            monitor.run_parallel_group(
                [target], cfg, state, SimpleNamespace(force=False, dry_run=False, wait=True))
        self.assertEqual(set(courts), {court for _, court in submitted})
        self.assertEqual(5, len(submitted))

    def test_unknown_final_submit_blocks_activity_but_not_other_activity(self):
        fitness = {**ALTERNATIVES[0], "activityId": "101", "activity": "Fitness",
                   "centerId": "152", "center": "X202"}
        cfg = {"booking": {"enabled": True, "targets": ALTERNATIVES,
                           "burst_seconds": 0, "retry_interval": 3}}
        cfg["booking"]["targets"] = [*ALTERNATIVES, fitness]
        state, posts, snapshots = {"processed": {}}, [], []
        barrier = threading.Barrier(4)

        class FakeClient:
            def book(self, selection, on_final_submit=None, **_kwargs):
                barrier.wait(timeout=2)
                on_final_submit()
                posts.append(selection.activity_id)
                if selection.activity_id == "101":
                    return BookingOutcome("booked", "confirmed", reason_code="confirmed")
                raise BookingAttemptError("timeout", "final_submit", "read_timeout",
                                          [{"stage": "final_submit"}], uncertain=True)

        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state", side_effect=lambda value: snapshots.append(
                    json.loads(json.dumps(value)))), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify", return_value=True), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))
            monitor.run(SimpleNamespace(force=False, dry_run=False, wait=False))
        self.assertEqual(["101", "2", "2", "2"], sorted(posts))
        self.assertTrue(any(
            entry["status"] == "final_sent"
            for snapshot in snapshots for entry in snapshot["campaigns"].values()
        ))
        self.assertEqual({"unknown", "booked"},
                         {item["status"] for item in state["campaigns"].values()})

    def test_unrecognized_result_keeps_other_badminton_targets_running(self):
        first = {**ALTERNATIVES[0], "facilityId": "court-a"}
        second = {**ALTERNATIVES[1], "facilityId": "court-b"}
        cfg = {"booking": {"enabled": True, "targets": [first, second],
                           "burst_seconds": 0, "retry_interval": 3}}
        state, reports, snapshots = {"processed": {}}, [], []
        first_started = threading.Event()

        class FakeClient:
            def book(self, selection, on_court_selected=None, on_final_submit=None,
                     **_kwargs):
                court = "court-a" if selection.center_id == "3" else "court-b"
                on_court_selected(court)
                if court == "court-b":
                    self_assert = first_started.wait(timeout=2)
                    if not self_assert:
                        raise AssertionError("first submit never started")
                on_final_submit()
                if court == "court-a":
                    first_started.set()
                    return BookingOutcome("failed", "record absent",
                                          stage="final_submit", reason_code="unknown_result")
                return BookingOutcome("booked", "confirmed",
                                      stage="final_submit", reason_code="confirmed")

        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state", side_effect=lambda value: snapshots.append(
                    json.loads(json.dumps(value)))), \
                patch.object(monitor, "report_attempt", side_effect=reports.append), \
                patch.object(monitor, "notify"), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))

        self.assertEqual({"failed", "booked"}, {r["status"] for r in reports})
        self.assertEqual("booked", state["account_activities"]["1|2026-10-16|18|2"]["status"])
        self.assertEqual("failed", state["campaigns"][monitor._court_key(first, "court-a")]["status"])
        self.assertEqual("booked", state["campaigns"][monitor._court_key(second, "court-b")]["status"])
        self.assertEqual(2, len(state["court_claims"]))
        self.assertTrue(any(
            row["status"] == "final_sent"
            for snapshot in snapshots for row in snapshot["campaigns"].values()
        ))

    def test_previous_unknown_result_does_not_block_other_target_on_restart(self):
        first = {**ALTERNATIVES[0], "facilityId": "court-a"}
        second = {**ALTERNATIVES[1], "facilityId": "court-b"}
        cfg = {"booking": {"enabled": True, "targets": [first, second],
                           "burst_seconds": 0}}
        first_key = monitor._court_key(first, "court-a")
        state = {
            "processed": {first_key: {"status": "unknown", "stage": "final_submit",
                                     "reason_code": "unknown_result", "account_id": "1"}},
            "account_activities": {"1|2026-10-16|18|2": {
                "status": "unknown", "court_key": first_key}},
        }
        attempted = []

        class FakeClient:
            def book(self, selection, on_court_selected=None, on_final_submit=None,
                     **_kwargs):
                on_court_selected(selection.facility_id)
                on_final_submit()
                attempted.append(selection.center_id)
                return BookingOutcome("booked", "confirmed", reason_code="confirmed")

        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))

        self.assertEqual(["1"], attempted)
        self.assertEqual("failed", state["processed"][first_key]["status"])
        self.assertEqual("booked", state["account_activities"]["1|2026-10-16|18|2"]["status"])

    def test_distinct_courts_can_submit_before_first_result_returns(self):
        target = ALTERNATIVES[0]
        cfg = {"booking": {"enabled": True, "targets": [target],
                           "burst_seconds": 0, "retry_interval": 3}}
        key = monitor._target_key(target)
        release = monitor.release_at(target, cfg)
        state = {"processed": {}, "prepared_courts": {
            key: {"facility_ids": ["court-a", "court-b"], "at": 1},
        }}
        posts = []
        barrier = threading.Barrier(2)
        first_callback = threading.Event()
        second_callback = threading.Event()
        second_posted = threading.Event()
        release_first = threading.Event()

        class FakeClient:
            def book(self, selection, on_final_submit=None,
                     prepared_facility_id="", **_kwargs):
                barrier.wait(timeout=2)
                if prepared_facility_id == "court-b":
                    first_callback.wait(timeout=2)
                    second_callback.set()
                    on_final_submit()
                    posts.append(prepared_facility_id)
                    second_posted.set()
                    return BookingOutcome("booked", "My Record confirmed",
                                          reason_code="confirmed")
                on_final_submit()
                first_callback.set()
                release_first.wait(timeout=2)
                posts.append(prepared_facility_id)
                return BookingOutcome("failed", "already booked",
                                      stage="final_submit", reason_code="slot_taken")

        with patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "wait_for_nearby_release",
                             side_effect=lambda _client, _targets, _cfg, now, *_args, **_kwargs: now), \
                patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()):
            runner = threading.Thread(
                target=monitor.run,
                args=(SimpleNamespace(force=False, dry_run=False, wait=True),),
            )
            runner.start()
            try:
                self.assertTrue(first_callback.wait(timeout=2))
                self.assertTrue(second_callback.wait(timeout=2))
                self.assertTrue(second_posted.wait(timeout=1))
                self.assertEqual(["court-b"], posts)
            finally:
                release_first.set()
                runner.join(timeout=2)

        self.assertFalse(runner.is_alive())
        self.assertEqual({"court-a", "court-b"}, set(posts))
        self.assertEqual(
            "booked", state["campaigns"][monitor._court_key(target, "court-b")]["status"]
        )

    def test_first_attempt_on_other_court_continues_after_account_success(self):
        first = {**ALTERNATIVES[0], "facilityId": "court-a"}
        second = {**ALTERNATIVES[1], "facilityId": "court-b"}
        cfg = {"booking": {"enabled": True, "targets": [first, second],
                           "burst_seconds": 0, "retry_interval": 3}}

        for pause in ("before_attempt", "before_final", "before_final_unknown"):
            with self.subTest(pause=pause):
                state = {"processed": {}}
                posts = []
                b_waiting = threading.Event()
                a_booked = threading.Event()

                class FakeClient:
                    def book(self, selection, on_court_selected=None,
                             on_final_submit=None, **_kwargs):
                        is_second = selection.center_id == second["centerId"]
                        if is_second and pause.startswith("before_final"):
                            on_court_selected("court-b")
                            b_waiting.set()
                            if not a_booked.wait(timeout=2):
                                raise AssertionError("A did not finish")
                        else:
                            if not is_second and not b_waiting.wait(timeout=2):
                                raise AssertionError("B did not start")
                            on_court_selected("court-b" if is_second else "court-a")
                        on_final_submit()
                        posts.append(selection.center_id)
                        if is_second:
                            if pause == "before_final_unknown":
                                raise BookingAttemptError(
                                    "timeout", "final_submit", "read_timeout", [],
                                    uncertain=True)
                            return BookingOutcome("failed", "account already booked",
                                                  stage="final_submit", reason_code="slot_taken")
                        return BookingOutcome("booked", "confirmed", reason_code="confirmed")

                def warmup(_client, targets, _config, now, *_args, **_kwargs):
                    if (pause == "before_attempt"
                            and targets[0]["centerId"] == second["centerId"]):
                        b_waiting.set()
                        if not a_booked.wait(timeout=2):
                            raise AssertionError("A did not finish")
                    return now

                def report(row):
                    if row["status"] == "booked":
                        a_booked.set()

                with patch.object(monitor, "wait_for_nearby_release", side_effect=warmup), \
                        patch.object(monitor, "client_from_env",
                                     side_effect=lambda *_a, **_kw: FakeClient()), \
                        patch.object(monitor, "save_state"), \
                        patch.object(monitor, "report_attempt", side_effect=report):
                    monitor.run_parallel_group(
                        [first, second], cfg, state,
                        SimpleNamespace(force=True, dry_run=False, wait=False))

                self.assertEqual({"3", "1"}, set(posts))
                self.assertEqual(
                    "booked", state["account_activities"]["1|2026-10-16|18|2"]["status"])

    def test_definite_rejection_does_not_stop_another_target(self):
        cfg = {"booking": {"enabled": True, "targets": ALTERNATIVES[:2],
                           "burst_seconds": 0, "retry_interval": 3}}
        state, posts, snapshots = {"processed": {}}, [], []
        first_started = threading.Event()

        class FakeClient:
            def book(self, selection, on_final_submit=None, **_kwargs):
                if selection.center_id != "3":
                    self_assert = first_started.wait(timeout=2)
                    if not self_assert:
                        raise AssertionError("first submit did not start")
                on_final_submit()
                posts.append(selection.center_id)
                if selection.center_id == "3":
                    first_started.set()
                    return BookingOutcome(
                        "failed", "already booked", stage="final_submit",
                        reason_code="slot_taken")
                return BookingOutcome("booked", "My Record confirmed", reason_code="confirmed")

        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state", side_effect=lambda value: snapshots.append(
                    json.loads(json.dumps(value)))), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))
        self.assertEqual({"3", "1"}, set(posts))
        self.assertTrue(any(
            entry.get("target") == monitor._target_key(ALTERNATIVES[0])
            and entry["status"] == "final_sent"
            for snapshot in snapshots for entry in snapshot["campaigns"].values()
        ))
        self.assertEqual("booked", state["campaigns"][monitor._target_key(ALTERNATIVES[1])]["status"])
        self.assertNotIn(monitor._target_key(ALTERNATIVES[0]), state["campaigns"])

    def test_restart_skips_unresolved_activity_but_continues_fitness(self):
        fitness = {**ALTERNATIVES[1], "activityId": "101", "activity": "Fitness"}
        cfg = {"booking": {"enabled": True, "targets": [*ALTERNATIVES[:2], fitness],
                           "burst_seconds": 0}}
        release = monitor.release_at(ALTERNATIVES[0], cfg)
        first_key = monitor._target_key(ALTERNATIVES[0])
        state = {"processed": {}, "campaigns": {
            first_key: {"status": "final_sent", "target": first_key},
        }}
        attempted = []

        class FakeClient:
            def book(self, selection, **_kwargs):
                attempted.append(selection.center_id)
                return BookingOutcome("booked", "confirmed", reason_code="confirmed")

        with patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()):
            monitor.run(SimpleNamespace(force=False, dry_run=False, wait=False))
        self.assertEqual(["1"], attempted)
        self.assertEqual("unknown", state["account_activities"]["1|2026-10-16|18|2"]["status"])
        self.assertEqual("final_sent", state["campaigns"][first_key]["status"])
        self.assertEqual("booked", state["campaigns"][monitor._target_key(fitness)]["status"])

    def test_restart_recovers_booked_activity_from_legacy_processed_state(self):
        fitness = {**ALTERNATIVES[0], "activityId": "101", "activity": "Fitness"}
        targets = [ALTERNATIVES[0], ALTERNATIVES[1], fitness]
        cfg = {"booking": {"enabled": True, "targets": targets, "burst_seconds": 0}}
        state = {"processed": {
            monitor._target_key(targets[0]): {"status": "booked", "stage": "verify_record"},
        }}
        attempted = []

        class FakeClient:
            def book(self, selection, **_kwargs):
                attempted.append(selection.activity_id)
                return BookingOutcome("booked", "confirmed", reason_code="confirmed")

        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "client_from_env", return_value=FakeClient()):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))
        self.assertEqual(["101"], attempted)
        self.assertEqual("booked", state["account_activities"]["1|2026-10-16|18|2"]["status"])

    def test_first_step_timeout_does_not_stop_other_timeslot(self):
        targets = [ALTERNATIVES[0], {**ALTERNATIVES[1], "from": "16:30", "to": "17:30"}]
        cfg = {"booking": {"enabled": True, "targets": targets,
                           "burst_seconds": 0, "retry_interval": 3}}
        state, calls = {"processed": {}}, []
        barrier = threading.Barrier(2)

        class FakeClient:
            def book(self, selection, on_final_submit=None, **_kwargs):
                barrier.wait(timeout=2)
                calls.append(selection.center_id)
                if selection.center_id == "3":
                    raise BookingAttemptError("timeout", "first_step", "read_timeout",
                                              [], uncertain=True)
                on_final_submit()
                return BookingOutcome("booked", "My Record confirmed", reason_code="confirmed")

        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))
        self.assertEqual({"3", "1"}, set(calls))
        self.assertEqual("booked", next(iter(state["campaigns"].values()))["status"])
        self.assertEqual({"unknown", "booked"},
                         {entry["status"] for entry in state["processed"].values()})

    def test_prepared_courts_are_used_at_release_without_timetable_request(self):
        targets = ALTERNATIVES[:2]
        cfg = {"booking": {"enabled": True, "targets": targets, "burst_seconds": 0}}
        release = monitor.release_at(targets[0], cfg)
        seen = []
        barrier = threading.Barrier(2)

        class FakeClient:
            def timetable(self, _dataset, _activity, center, *_args):
                return {"columns": [{"slots": [{
                    "date": "2026-10-16", "start": "15:30", "end": "16:30",
                    "facilityIds": [center + "-court"],
                }]}]}

            def book(self, selection, prepared_facility_id="", **_kwargs):
                barrier.wait(timeout=2)
                seen.append((selection.center_id, prepared_facility_id))
                raise BookingAttemptError("no court", "availability", "no_vacancy", [])

        def ready(client, _targets, _cfg, now, *_args, on_ready=None, **_kwargs):
            on_ready(client)
            return now

        clock = SimpleNamespace(now=lambda _tz: release)
        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value={"processed": {}}), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "notify"), \
                patch.object(monitor, "datetime", clock), \
                patch.object(monitor, "wait_for_nearby_release", side_effect=ready), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()):
            monitor.run(SimpleNamespace(force=False, dry_run=False, wait=True))
        self.assertEqual({("3", "3-court"), ("1", "1-court")}, set(seen))

    def test_two_courts_of_one_target_prepare_and_race_independently(self):
        target = ALTERNATIVES[0]
        cfg = {"booking": {"enabled": True, "targets": [target], "burst_seconds": 0}}
        release = monitor.release_at(target, cfg)
        state, attempts, reports = {"processed": {}}, [], []
        barrier = threading.Barrier(2)

        class FakeClient:
            def timetable(self, *_args):
                return {"columns": [{"slots": [{
                    "date": target["date"], "start": target["from"], "end": target["to"],
                    "facilityIds": ["court-a", "court-b"],
                }]}]}

            def book(self, _selection, prepared_facility_id="", on_first_step=None,
                     **_kwargs):
                barrier.wait(timeout=2)
                on_first_step()
                attempts.append(prepared_facility_id)
                raise BookingAttemptError("taken", "confirmation_page", "slot_taken", [])

        def ready(client, _targets, _cfg, now, *_args, on_ready=None, **_kwargs):
            on_ready(client)
            return now

        with patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "wait_for_nearby_release", side_effect=ready), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt", side_effect=reports.append), \
                patch.object(monitor, "notify"):
            monitor.run_parallel_group([target], cfg, state,
                                       SimpleNamespace(force=False, dry_run=False, wait=True))
        self.assertEqual({"court-a", "court-b"}, set(attempts))
        self.assertEqual(["court-a", "court-b"],
                         state["prepared_courts"][monitor._target_key(target)]["facility_ids"])
        self.assertEqual(2, len([r for r in reports if r["status"] == "sending"]))

    def test_cached_courts_survive_failed_preparation_at_release(self):
        target = ALTERNATIVES[0]
        cfg = {"booking": {"enabled": True, "targets": [target], "burst_seconds": 0}}
        release = monitor.release_at(target, cfg)
        key = monitor._target_key(target)
        state = {"processed": {}, "prepared_courts": {
            key: {"facility_ids": ["cached-a", "cached-b"], "at": 1},
        }}
        seen = []

        class FakeClient:
            def timetable(self, *_args):
                raise requests.ReadTimeout()

            def book(self, _selection, prepared_facility_id="", **_kwargs):
                seen.append(prepared_facility_id)
                raise BookingAttemptError("taken", "confirmation_page", "slot_taken", [])

        def failed_warmup(client, _targets, _cfg, now, *_args, on_ready=None, **_kwargs):
            with self.assertRaises(requests.ReadTimeout):
                on_ready(client)
            return now

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(monitor, "STATE_FILE", str(Path(directory) / "state.json")), \
                patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "wait_for_nearby_release", side_effect=failed_warmup), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"):
            monitor.save_state(state)
            monitor.run_parallel_group([target], cfg, monitor.load_state(),
                                       SimpleNamespace(force=False, dry_run=False, wait=True))
        self.assertEqual({"cached-a", "cached-b"}, set(seen))

    def test_stale_cached_court_falls_back_to_fresh_timetable(self):
        target = ALTERNATIVES[0]
        cfg = {"booking": {"enabled": True, "targets": [target],
                           "burst_seconds": 10}}
        release = monitor.release_at(target, cfg)
        key = monitor._target_key(target)
        state = {"processed": {}, "prepared_courts": {
            key: {"facility_ids": ["old-court"], "at": 1},
        }}
        seen = []

        class FakeClient:
            def book(self, _selection, prepared_facility_id="", **_kwargs):
                seen.append(prepared_facility_id)
                if prepared_facility_id:
                    raise BookingAttemptError(
                        "outdated id", "prepared", "request_rejected", [])
                return BookingOutcome("booked", "confirmed", reason_code="confirmed")

        with patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "wait_for_nearby_release",
                             side_effect=lambda _client, _targets, _cfg, now, *_args, **_kwargs: now), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"), \
                patch.object(monitor.time, "sleep"):
            monitor.run_parallel_group([target], cfg, state,
                                       SimpleNamespace(force=False, dry_run=False, wait=True))
        self.assertEqual(["old-court", ""], seen)

    def test_one_booking_does_not_stop_later_release_groups(self):
        targets = [ALTERNATIVES[0], {**ALTERNATIVES[1], "date": "2026-10-17"}]
        cfg = {"booking": {"enabled": True, "targets": targets, "burst_seconds": 0}}
        state, attempted = {"processed": {}}, []

        class FakeClient:
            def book(self, selection, **_kwargs):
                attempted.append(selection.date)
                return BookingOutcome("booked", "confirmed", reason_code="confirmed")

        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))
        self.assertEqual(["2026-10-16", "2026-10-17"], attempted)

    def test_cloud_run_only_waits_for_todays_release(self):
        today = datetime.now(monitor.TZ).date()
        targets = [
            {**ALTERNATIVES[0], "date": (today + timedelta(days=7)).isoformat()},
            {**ALTERNATIVES[1], "date": (today + timedelta(days=8)).isoformat()},
        ]
        cfg = {"booking": {"enabled": True, "lead_days": 7, "targets": targets}}
        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value={}), \
                patch.object(monitor, "run_parallel_group") as run_group:
            monitor.run(SimpleNamespace(today_only=True, force=False,
                                        dry_run=False, wait=True))
        run_group.assert_called_once()
        self.assertEqual([targets[0]], run_group.call_args.args[0])

    def test_multiple_targets_send_initial_requests_and_keep_one_booking_per_activity(self):
        cfg = {"booking": {"enabled": True, "targets": ALTERNATIVES,
                           "burst_seconds": 0}}
        state = {"processed": {}}
        submitted = []
        barrier = threading.Barrier(3)

        class FakeClient:
            def book(self, selection, on_final_submit=None, **_kwargs):
                barrier.wait(timeout=2)
                on_final_submit()
                submitted.append(selection.center_id)
                if selection.center_id == "3":
                    return BookingOutcome("booked", "confirmed", reason_code="confirmed")
                return BookingOutcome("failed", "account limit",
                                      stage="final_submit", reason_code="server_rejected")

        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))
        self.assertEqual({"3", "1", "51"}, set(submitted))
        self.assertEqual(1, len(state["campaigns"]))
        self.assertEqual({"booked"}, {item["status"] for item in state["processed"].values()})
        confirmed = next(iter(state["processed"].values()))["booking"]
        self.assertEqual("Badminton", confirmed["activity"])
        self.assertIn(confirmed["center"], {"Kwong", "Shaw", "Shaw Practice Hall"})
        self.assertEqual("2026-10-16", confirmed["date"])
        self.assertEqual("1", confirmed["account_id"])

    def test_two_accounts_each_book_one_per_activity_without_sharing_courts(self):
        badminton = ALTERNATIVES[0]
        fitness = {**badminton, "activityId": "101", "activity": "Fitness",
                   "centerId": "152", "center": "X202"}
        cfg = {"booking": {"enabled": True, "targets": [badminton, fitness],
                           "burst_seconds": 0}}
        release = monitor.release_at(badminton, cfg)
        state = {"processed": {}, "prepared_courts": {
            monitor._target_key(badminton): {"facility_ids": ["bad-a", "bad-b", "bad-c", "bad-d"]},
            monitor._target_key(fitness): {"facility_ids": ["fit-a", "fit-b", "fit-c", "fit-d"]},
        }}
        barrier = threading.Barrier(8)
        posts, clients = [], []

        class FakeClient:
            def __init__(self, account):
                self.account = account
                clients.append(self)

            def book(self, selection, prepared_facility_id="", on_court_selected=None,
                     on_final_submit=None, **_kwargs):
                barrier.wait(timeout=3)
                on_court_selected(prepared_facility_id)
                on_final_submit()
                posts.append((self.account, selection.activity_id, prepared_facility_id))
                if prepared_facility_id in ("bad-a", "bad-b", "fit-a", "fit-b"):
                    return BookingOutcome("booked", "confirmed", reason_code="confirmed")
                return BookingOutcome("failed", "account limit",
                                      stage="final_submit", reason_code="server_rejected")

        with patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "wait_for_nearby_release",
                             side_effect=lambda _client, _targets, _cfg, now, *_a, **_kw: now), \
                patch.object(monitor, "account_ids_from_env", return_value=["1", "2"]), \
                patch.object(monitor, "client_from_env",
                             side_effect=lambda *_a, **kw: FakeClient(kw.get("account_id", "1"))), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"):
            monitor.run_parallel_group(
                [badminton, fitness], cfg, state,
                SimpleNamespace(force=False, dry_run=False, wait=True))
            monitor.run_parallel_group(
                [badminton, fitness], cfg, state,
                SimpleNamespace(force=False, dry_run=False, wait=True))

        self.assertEqual(8, len(clients))
        self.assertEqual({("1", "2"), ("1", "101"), ("2", "2"), ("2", "101")},
                         {(account, activity) for account, activity, _ in posts})
        self.assertEqual(8, len(posts))
        self.assertEqual(8, len({court for _, _, court in posts}))
        self.assertEqual({"booked"}, {row["status"] for row in state["account_activities"].values()})
        self.assertEqual(4, len(state["court_claims"]))

    def test_unassigned_badminton_targets_start_with_different_accounts(self):
        targets = ALTERNATIVES[:2]
        cfg = {"booking": {"enabled": True, "targets": targets, "burst_seconds": 1}}
        release = monitor.release_at(targets[0], cfg)
        state = {"processed": {}, "prepared_courts": {
            monitor._target_key(target): {"facility_ids": [f"court-{target['centerId']}"]}
            for target in targets
        }}
        submitted = []

        class FakeClient:
            def __init__(self, account):
                self.account = account

            def book(self, selection, prepared_facility_id="", on_court_selected=None,
                     on_final_submit=None, **_kwargs):
                on_court_selected(prepared_facility_id)
                on_final_submit()
                submitted.append((self.account, selection.center_id))
                return BookingOutcome("booked", "confirmed", reason_code="confirmed")

        with patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "wait_for_nearby_release",
                             side_effect=lambda _client, _targets, _cfg, now, *_a, **_kw: now), \
                patch.object(monitor, "account_ids_from_env", return_value=["1", "2"]), \
                patch.object(monitor, "client_from_env",
                             side_effect=lambda *_a, **kw: FakeClient(kw.get("account_id", "1"))), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"):
            monitor.run_parallel_group(
                targets, cfg, state, SimpleNamespace(force=False, dry_run=False, wait=True))
        self.assertEqual({("1", "3"), ("2", "1")}, set(submitted))
        self.assertEqual(2, len(submitted))
        self.assertEqual({"1|2026-10-16|18|2", "2|2026-10-16|18|2"},
                         set(state["account_activities"]))

    def test_auto_assignment_falls_back_to_another_target_after_failure(self):
        targets = ALTERNATIVES[:2]
        cfg = {"booking": {"enabled": True, "targets": targets, "burst_seconds": 1}}
        release = monitor.release_at(targets[0], cfg)
        state = {"processed": {}, "prepared_courts": {
            monitor._target_key(targets[0]): {"facility_ids": ["a"]},
            monitor._target_key(targets[1]): {"facility_ids": ["b1", "b2"]},
        }}
        submitted = []

        class FakeClient:
            def __init__(self, account):
                self.account = account

            def book(self, selection, prepared_facility_id="", on_court_selected=None,
                     on_final_submit=None, **_kwargs):
                if selection.center_id == "3":
                    raise BookingAttemptError("no vacancy", "availability", "no_vacancy", [])
                on_court_selected(prepared_facility_id)
                on_final_submit()
                submitted.append((self.account, prepared_facility_id))
                return BookingOutcome("booked", "confirmed", reason_code="confirmed")

        with patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "wait_for_nearby_release",
                             side_effect=lambda _client, _targets, _cfg, now, *_a, **_kw: now), \
                patch.object(monitor, "account_ids_from_env", return_value=["1", "2"]), \
                patch.object(monitor, "client_from_env",
                             side_effect=lambda *_a, **kw: FakeClient(kw.get("account_id", "1"))), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"):
            monitor.run_parallel_group(
                targets, cfg, state, SimpleNamespace(force=False, dry_run=False, wait=True))
        self.assertEqual({("2", "b1"), ("1", "b2")}, set(submitted))

    def test_assigned_fitness_targets_run_only_for_their_accounts(self):
        targets = [
            {**ALTERNATIVES[0], "activityId": "101", "activity": "Fitness",
             "facilityId": "fit-a", "accountId": "1"},
            {**ALTERNATIVES[1], "activityId": "101", "activity": "Fitness",
             "facilityId": "fit-b", "accountId": "2"},
        ]
        cfg = {"booking": {"enabled": True, "targets": targets, "burst_seconds": 0}}
        state, submitted = {"processed": {}}, []

        class FakeClient:
            def __init__(self, account):
                self.account = account

            def book(self, selection, on_court_selected=None,
                     on_final_submit=None, **_kwargs):
                on_court_selected(selection.facility_id)
                on_final_submit()
                submitted.append((self.account, selection.facility_id))
                return BookingOutcome("booked", "confirmed", reason_code="confirmed")

        with patch.object(monitor, "account_ids_from_env", return_value=["1", "2"]), \
                patch.object(monitor, "client_from_env",
                             side_effect=lambda *_a, **kw: FakeClient(kw.get("account_id", "1"))), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"):
            monitor.run_parallel_group(
                targets, cfg, state, SimpleNamespace(force=True, dry_run=False, wait=False))
        self.assertEqual({("1", "fit-a"), ("2", "fit-b")}, set(submitted))
        self.assertEqual(2, len(submitted))

    def test_assignment_to_missing_account_is_rejected_before_booking(self):
        target = {**ALTERNATIVES[0], "accountId": "2"}
        cfg = {"booking": {"enabled": True, "targets": [target]}}
        with patch.object(monitor, "account_ids_from_env", return_value=["1"]), \
                patch.object(monitor, "client_from_env") as create:
            with self.assertRaisesRegex(Exception, "账号 2"):
                monitor.run_parallel_group(
                    [target], cfg, {"processed": {}},
                    SimpleNamespace(force=True, dry_run=False, wait=False))
        create.assert_not_called()

    def test_login_failure_in_one_account_does_not_stop_other_account(self):
        target = {**ALTERNATIVES[0], "facilityId": "court-a"}
        cfg = {"booking": {"enabled": True, "targets": [target], "burst_seconds": 0}}
        state, posts = {"processed": {}}, []

        class FakeClient:
            def book(self, _selection, on_final_submit=None, **_kwargs):
                on_final_submit()
                posts.append("2")
                return BookingOutcome("booked", "confirmed", reason_code="confirmed")

        def create(*_args, **kwargs):
            if kwargs.get("account_id", "1") == "1":
                raise LoginError("account 1 unavailable")
            return FakeClient()

        with patch.object(monitor, "account_ids_from_env", return_value=["1", "2"]), \
                patch.object(monitor, "client_from_env", side_effect=create), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"):
            monitor.run_parallel_group(
                [target], cfg, state, SimpleNamespace(force=True, dry_run=False, wait=False))
        self.assertEqual(["2"], posts)
        self.assertEqual("booked", state["account_activities"]["2|2026-10-16|18|2"]["status"])

    def test_booking_badminton_does_not_hide_fitness_failure_notification(self):
        badminton = ALTERNATIVES[0]
        fitness = {**badminton, "activityId": "101", "activity": "Fitness"}
        cfg = {"booking": {"enabled": True, "targets": [badminton, fitness],
                           "burst_seconds": 0}}
        state, notified = {"processed": {}}, []

        class FakeClient:
            def book(self, selection, on_final_submit=None, **_kwargs):
                if selection.activity_id == "2":
                    on_final_submit()
                    return BookingOutcome("booked", "confirmed", reason_code="confirmed")
                raise BookingAttemptError("no vacancy", "availability", "no_vacancy", [])

        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify",
                             side_effect=lambda title, _body: notified.append(title) or True), \
                patch.object(monitor, "client_from_env", return_value=FakeClient()):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))
        self.assertEqual(1, len(notified))
        self.assertIn("Fitness", notified[0])

    def test_two_explicit_areas_submit_concurrently_and_server_limits_booking(self):
        targets = [{**ALTERNATIVES[0], "facilityId": court}
                   for court in ("court-a", "court-b")]
        self.assertEqual(2, len(poss_ui.validated_targets(targets)))
        cfg = {"booking": {"enabled": True, "targets": targets, "burst_seconds": 0}}
        state, submitted = {"processed": {}}, []
        barrier = threading.Barrier(2)
        submitted_barrier = threading.Barrier(2)

        class FakeClient:
            def book(self, selection, on_final_submit=None, **_kwargs):
                barrier.wait(timeout=2)
                on_final_submit()
                submitted.append(selection.facility_id)
                submitted_barrier.wait(timeout=2)
                if selection.facility_id == "court-a":
                    return BookingOutcome("booked", "confirmed", reason_code="confirmed")
                return BookingOutcome("failed", "booking limit",
                                      stage="final_submit", reason_code="server_rejected")

        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))
        self.assertEqual({"court-a", "court-b"}, set(submitted))
        self.assertEqual("booked", state["account_activities"]["1|2026-10-16|18|2"]["status"])

    def test_any_area_books_one_prepared_court_and_restart_does_not_repeat(self):
        target = ALTERNATIVES[0]
        cfg = {"booking": {"enabled": True, "targets": [target], "burst_seconds": 0}}
        release = monitor.release_at(target, cfg)
        state = {"processed": {}, "prepared_courts": {
            monitor._target_key(target): {
                "facility_ids": ["a", "b", "c", "d"], "at": 1,
            },
        }}
        submitted = []

        class FakeClient:
            def book(self, _selection, on_final_submit=None,
                     prepared_facility_id="", **_kwargs):
                if not prepared_facility_id:
                    raise BookingAttemptError("no more courts", "availability", "no_vacancy", [])
                on_final_submit()
                submitted.append(prepared_facility_id)
                return BookingOutcome("booked", "confirmed", reason_code="confirmed")

        with patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "wait_for_nearby_release",
                             side_effect=lambda _client, _targets, _cfg, now, *_a, **_kw: now), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"):
            monitor.run_parallel_group(
                [target], cfg, state, SimpleNamespace(force=False, dry_run=False, wait=True))
            monitor.run_parallel_group(
                [target], cfg, state, SimpleNamespace(force=False, dry_run=False, wait=True))
        self.assertEqual({"a", "b", "c", "d"}, set(submitted))
        self.assertEqual(4, len(state["processed"]))

    def test_any_area_and_explicit_area_do_not_double_book_same_court(self):
        wildcard = ALTERNATIVES[0]
        explicit = {**wildcard, "facilityId": "a"}
        cfg = {"booking": {"enabled": True, "targets": [wildcard, explicit],
                           "burst_seconds": 0}}
        release = monitor.release_at(wildcard, cfg)
        state = {"processed": {}, "prepared_courts": {
            monitor._target_key(wildcard): {"facility_ids": ["a", "b"], "at": 1},
            monitor._target_key(explicit): {"facility_ids": ["a"], "at": 1},
        }}
        submitted = []

        class FakeClient:
            def book(self, selection, on_final_submit=None,
                     prepared_facility_id="", **_kwargs):
                if not prepared_facility_id:
                    raise BookingAttemptError("no more courts", "availability", "no_vacancy", [])
                on_final_submit()
                submitted.append((selection.facility_id, prepared_facility_id))
                return BookingOutcome("booked", "confirmed", reason_code="confirmed")

        with patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "wait_for_nearby_release",
                             side_effect=lambda _client, _targets, _cfg, now, *_a, **_kw: now), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"):
            monitor.run_parallel_group(
                [wildcard, explicit], cfg, state,
                SimpleNamespace(force=False, dry_run=False, wait=True))
        self.assertEqual({("", "b"), ("a", "a")}, set(submitted))

    def test_preflight_filters_court_ids_outside_selected_center(self):
        target = ALTERNATIVES[0]
        cfg = {"booking": {"enabled": True, "targets": [target], "burst_seconds": 0}}
        release = monitor.release_at(target, cfg)
        state = {"processed": {}}
        testcase = self

        class FakeClient:
            tree = [{"value": "18", "children": [{"value": "2", "children": [
                {"value": "3", "children": [{"value": "valid", "children": []}]},
            ]}]}]

            def timetable(self, *_args):
                return {"columns": [{"slots": [{
                    "date": target["date"], "start": target["from"], "end": target["to"],
                    "facilityIds": ["wrong", "valid"],
                }]}]}

            def book(self, _selection, prepared_facility_id="", **_kwargs):
                testcase.assertEqual("valid", prepared_facility_id)
                return BookingOutcome("booked", "confirmed", reason_code="confirmed")

        def ready(client, _targets, _cfg, now, *_args, on_ready=None, **_kwargs):
            on_ready(client)
            return now

        with patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "wait_for_nearby_release", side_effect=ready), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"):
            monitor.run_parallel_group(
                [target], cfg, state, SimpleNamespace(force=False, dry_run=False, wait=True))
        key = monitor._target_key(target)
        self.assertEqual(["valid"], state["prepared_courts"][key]["facility_ids"])
        self.assertEqual(1, sum(lane["courts"] for lane in state["readiness"][key]["lanes"].values()))

    def test_fallback_collision_retries_other_court(self):
        target = ALTERNATIVES[0]
        cfg = {"booking": {"enabled": True, "targets": [target],
                           "burst_seconds": 2, "retry_interval": 1}}
        release = monitor.release_at(target, cfg)
        state = {"processed": {}}
        selected = []
        reports = []

        class FakeClient:
            def book(self, _selection, on_court_selected=None,
                     on_final_submit=None, **_kwargs):
                if len(selected) == 2:
                    raise BookingAttemptError("no more courts", "availability", "no_vacancy", [])
                court_id = ("first" if not selected else "second")
                selected.append(court_id)
                if court_id == "first":
                    raise BookingAttemptError("collision", "prepared",
                                              "court_already_attempted", [])
                on_court_selected(court_id)
                on_final_submit()
                return BookingOutcome("booked", "confirmed", reason_code="confirmed")

        with patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "wait_for_nearby_release",
                             side_effect=lambda _client, _targets, _cfg, now, *_a, **_kw: now), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt", side_effect=reports.append), \
                patch.object(monitor, "notify"):
            monitor.run_parallel_group(
                [target], cfg, state, SimpleNamespace(force=False, dry_run=False, wait=True))
        self.assertEqual(["first", "second"], selected)
        self.assertEqual("booked", state["processed"][monitor._court_key(target, "second")]["status"])
        self.assertEqual("court_already_attempted",
                         next(r for r in reports if r["status"] == "booked")[
                             "attempt_failures"][0]["reason_code"])

    def test_booking_result_is_reported_before_slow_lane_finishes(self):
        target = ALTERNATIVES[0]
        cfg = {"booking": {"enabled": True, "targets": [target], "burst_seconds": 0}}
        release = monitor.release_at(target, cfg)
        state = {"processed": {}, "prepared_courts": {
            monitor._target_key(target): {"facility_ids": ["fast", "slow"], "at": 1},
        }}
        slow_waiting, release_slow, fast_reported = (
            threading.Event(), threading.Event(), threading.Event()
        )
        reports = []

        class FakeClient:
            def book(self, _selection, on_final_submit=None,
                     prepared_facility_id="", **_kwargs):
                if prepared_facility_id == "slow":
                    slow_waiting.set()
                    release_slow.wait(timeout=2)
                    raise BookingAttemptError("no vacancy", "availability", "no_vacancy", [])
                slow_waiting.wait(timeout=2)
                on_final_submit()
                return BookingOutcome("booked", "confirmed", reason_code="confirmed",
                                      events=[{"stage": "first_step", "duration_ms": 12}])

        def record(report):
            reports.append(report)
            if report["status"] == "booked":
                fast_reported.set()

        with patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "wait_for_nearby_release",
                             side_effect=lambda _client, _targets, _cfg, now, *_a, **_kw: now), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt", side_effect=record), \
                patch.object(monitor, "notify"):
            runner = threading.Thread(
                target=monitor.run_parallel_group,
                args=([target], cfg, state, SimpleNamespace(
                    force=False, dry_run=False, wait=True)),
            )
            runner.start()
            self.assertTrue(fast_reported.wait(timeout=2))
            self.assertTrue(runner.is_alive())
            self.assertTrue(reports[0]["run_id"])
            release_slow.set()
            runner.join(timeout=2)
        self.assertFalse(runner.is_alive())

    def test_runner_progress_uses_current_run_and_counts_stages(self):
        target = ALTERNATIVES[0]
        key = monitor._target_key(target)
        cfg = {"booking": {"enabled": True, "targets": [target], "lead_days": 7,
                           "trigger_time": "08:30"}}
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            log_file = Path(directory) / "booking_attempts.jsonl"
            state_file.write_text(json.dumps({
                "active_run": {"id": "current", "targets": [key]},
                "readiness": {key: {"run_id": "current", "lanes": {
                    "0": {"status": "ready", "courts": 2, "stage": "prepare"},
                }}},
            }))
            events = [
                {"run_id": "old", "target_key": key, "court_id": "x", "status": "booked"},
                {"run_id": "current", "target_key": key, "court_id": "a",
                 "status": "booked", "stage": "verify_record", "reason_code": "confirmed",
                 "events": [{"stage": "first_step", "duration_ms": 1200}]},
                {"run_id": "current", "target_key": key, "court_id": "b",
                 "status": "unknown", "stage": "final_submit", "reason_code": "read_timeout",
                 "events": [{"stage": "final_submit", "duration_ms": 30000,
                             "error": "ReadTimeout"}]},
            ]
            log_file.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            with patch.object(monitor, "STATE_FILE", str(state_file)), \
                    patch.object(monitor, "LOG_FILE", str(log_file)), \
                    patch.object(poss_ui, "load_config", return_value=cfg):
                row = poss_ui.runner_progress()["targets"][0]
        self.assertEqual(("ready", 2, 1, 1),
                         (row["readiness"], row["courts"], row["confirmed"], row["unknown"]))
        self.assertEqual(1, row["failures"]["final_submit"])
        self.assertEqual(30000, row["latency"]["final_submit"]["maxMs"])

    def test_runner_progress_tracks_activity_separately_for_each_account(self):
        badminton = ALTERNATIVES[0]
        fitness = {**badminton, "activityId": "101", "activity": "Fitness"}
        keys = [monitor._target_key(target) for target in (badminton, fitness)]
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(json.dumps({
                "active_run": {"id": "two-accounts", "targets": keys,
                               "accounts": ["1", "2"]},
                "account_activities": {
                    "1|2026-10-16|18|2": {"status": "booked"},
                    "2|2026-10-16|18|101": {"status": "unknown"},
                },
            }))
            cfg = {"booking": {"targets": [badminton, fitness], "lead_days": 7,
                               "trigger_time": "08:30"}}
            with patch.object(monitor, "STATE_FILE", str(state_file)), \
                    patch.object(monitor, "LOG_FILE", str(Path(directory) / "empty.jsonl")), \
                    patch.object(poss_ui, "load_config", return_value=cfg):
                rows = poss_ui.runner_progress()["targets"]
        self.assertEqual(["booked", "", "", "unknown"],
                         [row["activityStatus"] for row in rows])
        self.assertIn("Badminton", rows[0]["label"])
        self.assertIn("Fitness", rows[1]["label"])

    def test_runner_progress_only_shows_assigned_account_for_pinned_target(self):
        target = {**ALTERNATIVES[0], "accountId": "2"}
        key = monitor._target_key(target)
        cfg = {"booking": {"targets": [target], "lead_days": 7,
                           "trigger_time": "08:30"}}
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(json.dumps({
                "active_run": {"id": "assigned", "targets": [key],
                               "accounts": ["1", "2"]},
            }))
            with patch.object(monitor, "STATE_FILE", str(state_file)), \
                    patch.object(monitor, "LOG_FILE", str(Path(directory) / "empty.jsonl")), \
                    patch.object(poss_ui, "load_config", return_value=cfg):
                rows = poss_ui.runner_progress()["targets"]
        self.assertEqual(["2"], [row["accountId"] for row in rows])

    def test_ui_account_save_keeps_passwords_out_of_account_status(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(poss_ui, "ROOT", directory):
            poss_ui.write_env({"accounts": [
                {"id": "1", "netid": "one", "password": "secret-one"},
                {"id": "2", "netid": "two", "password": "secret-two"},
            ]})
            status = poss_ui.account_status()
            self.assertEqual(["1", "2"], [row["id"] for row in status["accounts"]])
            self.assertNotIn("secret-", json.dumps(status))
            poss_ui.write_env({"accounts": [
                {"id": "1", "netid": "one", "password": ""},
            ]})
            values = (Path(directory) / ".env").read_text()
            self.assertIn("POSS_PASSWORD=secret-one", values)
            self.assertNotIn("POSS_NETID_2", values)
            self.assertNotIn("secret-two", values)

    def test_saving_a_new_account_verifies_login_and_balance_in_response(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(poss_ui, "ROOT", directory), \
                patch.dict(poss_ui._account_checks, clear=True):
            checked = []

            class FakeClient:
                def __init__(self, slot):
                    self.slot = slot

                def login(self):
                    checked.append(self.slot)

                def deposit_balance(self):
                    return {"amount": "25.00", "currency": "HKD"}

            server = ThreadingHTTPServer(("127.0.0.1", 0), poss_ui.Handler)
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                with patch.object(poss_ui, "client_from_env",
                                  side_effect=lambda _path, account_id: FakeClient(account_id)):
                    url = f"http://127.0.0.1:{server.server_port}/api/account"
                    body = {"accounts": [
                        {"id": "1", "netid": "one", "password": "secret-one"},
                        {"id": "2", "netid": "two", "password": "secret-two"},
                    ]}
                    request = Request(url, data=json.dumps(body).encode(),
                                      headers={"Content-Type": "application/json"},
                                      method="POST")
                    with urlopen(request, timeout=3) as response:
                        result = json.load(response)
                self.assertEqual({"1", "2"}, set(checked))
                self.assertEqual(["verified", "verified"],
                                 [row["check"]["status"] for row in result["account"]["accounts"]])
                self.assertEqual("25.00", result["account"]["accounts"][1]["check"]["balance"]["amount"])
                self.assertNotIn("secret-", json.dumps(result))
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_runner_progress_flags_persisted_submit_after_interruption(self):
        target = ALTERNATIVES[0]
        key = monitor._target_key(target)
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            log_file = Path(directory) / "booking_attempts.jsonl"
            state_file.write_text(json.dumps({
                "active_run": {"id": "interrupted", "targets": [key]},
                "campaigns": {
                    monitor._court_key(target, "court-a"): {
                        "status": "final_sent", "at": 101, "stage": "final_submit",
                        "run_id": "interrupted",
                    },
                },
            }))
            log_file.write_text(json.dumps({
                "run_id": "interrupted", "target_key": key, "status": "sending",
                "court_id": "court-a", "events": [],
            }) + "\n")
            cfg = {"booking": {"targets": [target], "lead_days": 7,
                               "trigger_time": "08:30"}}
            with patch.object(monitor, "STATE_FILE", str(state_file)), \
                    patch.object(monitor, "LOG_FILE", str(log_file)), \
                    patch.object(poss_ui, "load_config", return_value=cfg):
                row = poss_ui.runner_progress()["targets"][0]
        self.assertEqual((0, 1), (row["sending"], row["unknown"]))
        self.assertIn("进程中断", row["last"])

    def test_runner_status_http_includes_preflight_and_counts(self):
        target = ALTERNATIVES[0]
        key = monitor._target_key(target)
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(json.dumps({
                "active_run": {"id": "test-run", "targets": [key]},
                "readiness": {key: {"run_id": "test-run", "lanes": {
                    "0": {"status": "ready", "courts": 1, "stage": "prepare"},
                }}},
            }))
            log_file = Path(directory) / "attempts.jsonl"
            log_file.write_text(json.dumps({
                "run_id": "test-run", "target_key": key, "court_id": "a",
                "status": "booked", "stage": "verify_record", "reason_code": "confirmed",
                "events": [], "attempt_failures": [],
            }) + "\n")
            config = {"booking": {"targets": [target], "lead_days": 7,
                                  "trigger_time": "08:30"}}
            server = ThreadingHTTPServer(("127.0.0.1", 0), poss_ui.Handler)
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                with patch.object(monitor, "STATE_FILE", str(state_file)), \
                        patch.object(monitor, "LOG_FILE", str(log_file)), \
                        patch.object(poss_ui, "load_config", return_value=config), \
                        patch.object(poss_ui, "runner_status", return_value={"running": False}):
                    with urlopen(f"http://127.0.0.1:{server.server_port}/api/runner/status",
                                 timeout=2) as response:
                        payload = json.load(response)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)
        self.assertEqual(1, payload["progress"]["targets"][0]["confirmed"])
        self.assertEqual("ready", payload["progress"]["targets"][0]["readiness"])

    def test_record_verification_requires_matching_area(self):
        row = (f"{SELECTION.center_name} 14 Oct 2026 (Wed) "
               "08:30 AM - 09:30 AM")
        c = PossClient("demo", "demo")
        c.session = SimpleNamespace(get=lambda *_a, **_kw: Response(
            200, f"<table><tr><td>{row} Court 10 Individual Booking (Confirmed)"
                 "</td></tr></table>"
        ))
        self.assertFalse(c._record_contains_booking(
            SELECTION, SELECTION.center_name, "Court 1"))
        self.assertTrue(c._record_contains_booking(
            SELECTION, SELECTION.center_name, "Court 10"))

    def test_late_first_step_unknown_does_not_erase_confirmed_booking(self):
        target = ALTERNATIVES[0]
        cfg = {"booking": {"enabled": True, "targets": [target], "burst_seconds": 0}}
        release = monitor.release_at(target, cfg)
        key = monitor._target_key(target)
        state = {"processed": {}, "prepared_courts": {
            key: {"facility_ids": ["slow", "fast"], "at": 1},
        }}
        slow_started, fast_finished = threading.Event(), threading.Event()

        class FakeClient:
            def book(self, _selection, prepared_facility_id="", on_final_submit=None,
                     **_kwargs):
                if prepared_facility_id == "slow":
                    slow_started.set()
                    fast_finished.wait(timeout=2)
                    raise BookingAttemptError("timeout", "first_step", "read_timeout",
                                              [], uncertain=True)
                slow_started.wait(timeout=2)
                on_final_submit()
                fast_finished.set()
                return BookingOutcome("booked", "confirmed", reason_code="confirmed")

        with patch.object(monitor, "datetime", SimpleNamespace(now=lambda _tz: release)), \
                patch.object(monitor, "wait_for_nearby_release",
                             side_effect=lambda _client, _targets, _cfg, now, *_args, **_kwargs: now), \
                patch.object(monitor, "client_from_env", side_effect=lambda *_: FakeClient()), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt"), \
                patch.object(monitor, "notify"):
            monitor.run_parallel_group([target], cfg, state,
                                       SimpleNamespace(force=False, dry_run=False, wait=True))
        self.assertEqual(
            "booked", state["processed"][monitor._court_key(target, "fast")]["status"]
        )
        self.assertEqual(
            "unknown", state["processed"][monitor._court_key(target, "slow")]["status"]
        )

    def test_ui_saves_multiple_centers_and_times_without_pinning_an_area(self):
        targets = [ALTERNATIVES[0], {**ALTERNATIVES[1], "from": "16:30", "to": "17:30"}]
        saved = poss_ui.validated_targets(targets + [targets[0]])
        self.assertEqual([("3", "15:30"), ("1", "16:30")],
                         [(item["centerId"], item["from"]) for item in saved])
        self.assertEqual("", saved[0]["facilityId"])
        with self.assertRaises(poss_ui.PossError):
            poss_ui.validated_targets([{**targets[0], "from": "25:30"}])

    def test_ui_preserves_optional_target_account_without_changing_auto_targets(self):
        targets = [
            {**ALTERNATIVES[0], "accountId": "2"},
            ALTERNATIVES[1],
        ]
        saved = poss_ui.validated_targets(targets)
        self.assertEqual("2", saved[0]["accountId"])
        self.assertNotIn("accountId", saved[1])
        self.assertEqual(saved, poss_ui.validated_targets(saved))
        with self.assertRaises(poss_ui.PossError):
            poss_ui.validated_targets([{**ALTERNATIVES[0], "accountId": "bad"}])

    def test_ui_target_api_saves_assignment_and_rejects_unconfigured_account(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            accounts = {"accounts": [
                {"id": "1", "netid": "one", "passwordSet": True},
                {"id": "2", "netid": "two", "passwordSet": True},
            ]}
            server = ThreadingHTTPServer(("127.0.0.1", 0), poss_ui.Handler)
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                with patch.object(poss_ui, "CONFIG_FILE", str(config_path)), \
                        patch.object(poss_ui, "account_status", return_value=accounts):
                    def save(account):
                        request = Request(
                            f"http://127.0.0.1:{server.server_port}/api/targets",
                            data=json.dumps({"targets": [
                                {**ALTERNATIVES[0], "accountId": account},
                                ALTERNATIVES[1],
                            ]}).encode(),
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        with urlopen(request, timeout=2) as response:
                            return json.load(response)

                    saved = save("2")["targets"]
                    self.assertEqual("2", saved[0]["accountId"])
                    self.assertNotIn("accountId", saved[1])
                    with self.assertRaises(HTTPError) as raised:
                        save("3")
                    self.assertEqual(400, raised.exception.code)
                    raised.exception.close()
                    self.assertEqual(saved, poss_ui.load_config()["booking"]["targets"])
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_manual_booking_uses_the_target_account(self):
        accounts = {"accounts": [
            {"id": "1", "netid": "one", "passwordSet": True},
            {"id": "2", "netid": "two", "passwordSet": True},
        ]}
        second = SimpleNamespace(login=lambda: None)
        first = object()
        with patch.object(poss_ui, "account_status", return_value=accounts), \
                patch.object(poss_ui, "get_client", return_value=first), \
                patch.object(poss_ui, "client_from_env", return_value=second) as create:
            self.assertIs(second, poss_ui.client_for_target({"accountId": "2"}))
            create.assert_called_once_with(
                str(Path(poss_ui.ROOT) / ".env"), account_id="2")
            self.assertIs(first, poss_ui.client_for_target({}))
            with self.assertRaises(LoginError):
                poss_ui.client_for_target({"accountId": "3"})

    def test_refreshes_authenticated_session_at_0827(self):
        base = datetime(2026, 10, 9, 8, 20, tzinfo=monitor.TZ)
        elapsed = [0]
        cfg = {"booking": {"lead_days": 7, "trigger_time": "08:30"}}

        class FakeClient:
            logins = []
            heartbeats = []
            expired = False

            def login(self):
                self.logins.append(elapsed[0])

            def heartbeat(self, force=False):
                self.heartbeats.append(elapsed[0])
                if elapsed[0] >= 7 * 60 and not self.expired:
                    self.expired = True
                    raise SessionExpired("expired")

        fake = FakeClient()
        clock = SimpleNamespace(now=lambda _tz: base + timedelta(seconds=elapsed[0]))
        with patch.object(monitor, "datetime", clock), \
                patch.object(monitor.time, "monotonic", side_effect=lambda: elapsed[0]), \
                patch.object(monitor.time, "sleep", side_effect=lambda seconds: elapsed.__setitem__(
                    0, elapsed[0] + seconds)):
            now = monitor.wait_for_nearby_release(
                fake, [{"date": "2026-10-16"}], cfg, base, wait_long=True,
            )
        self.assertEqual(datetime(2026, 10, 9, 8, 30, tzinfo=monitor.TZ), now)
        self.assertIn(7 * 60, fake.heartbeats)
        self.assertIn(9 * 60, fake.heartbeats)
        self.assertEqual([0, 7 * 60], fake.logins)

    def test_prepares_courts_before_release_and_refreshes_at_0827(self):
        base = datetime(2026, 10, 9, 8, 20, tzinfo=monitor.TZ)
        elapsed, prepared = [0], []
        cfg = {"booking": {"lead_days": 7, "trigger_time": "08:30"}}

        class FakeClient:
            def login(self):
                pass

            def heartbeat(self, force=False):
                pass

        clock = SimpleNamespace(now=lambda _tz: base + timedelta(seconds=elapsed[0]))
        with patch.object(monitor, "datetime", clock), \
                patch.object(monitor.time, "monotonic", side_effect=lambda: elapsed[0]), \
                patch.object(monitor.time, "sleep", side_effect=lambda seconds: elapsed.__setitem__(
                    0, elapsed[0] + seconds)):
            monitor.wait_for_nearby_release(
                FakeClient(), [{"date": "2026-10-16"}], cfg, base, wait_long=True,
                on_ready=lambda _client: prepared.append(elapsed[0]),
            )
        self.assertEqual([0, 7 * 60], prepared)

    def test_transient_ssl_heartbeat_does_not_end_local_wait(self):
        base = datetime(2026, 10, 8, 8, 25, tzinfo=monitor.TZ)
        elapsed = [0]
        cfg = {"booking": {"lead_days": 7, "trigger_time": "08:30"}}

        class FakeClient:
            logins = 0
            heartbeats = 0

            def login(self):
                self.logins += 1

            def heartbeat(self, force=False):
                self.heartbeats += 1
                if self.heartbeats == 1:
                    raise requests.exceptions.SSLError("temporary TLS failure")

        fake = FakeClient()
        clock = SimpleNamespace(now=lambda _tz: base + timedelta(seconds=elapsed[0]))
        reports = []
        statuses = []
        with patch.object(monitor, "datetime", clock), \
                patch.object(monitor.time, "monotonic", side_effect=lambda: elapsed[0]), \
                patch.object(monitor.time, "sleep", side_effect=lambda seconds: elapsed.__setitem__(
                    0, elapsed[0] + seconds)), \
                patch.object(monitor, "report_attempt", side_effect=reports.append):
            now = monitor.wait_for_nearby_release(
                fake, [{"date": "2026-10-15"}], cfg, base, wait_long=True,
                on_status=lambda *status: statuses.append(status),
            )
        self.assertEqual(datetime(2026, 10, 8, 8, 30, tzinfo=monitor.TZ), now)
        self.assertEqual(1, fake.logins)
        self.assertGreaterEqual(fake.heartbeats, 2)
        self.assertEqual("tls_error", reports[0]["reason_code"])
        self.assertEqual("heartbeat", reports[0]["stage"])
        self.assertEqual("recovered", reports[1]["status"])
        self.assertIn(("retrying", "heartbeat", "tls_error"), statuses)
        self.assertIn(("ready", "heartbeat", "session_ready"), statuses)

    def test_timetable_busy_response_preserves_status_and_retry_after(self):
        c = client(responses=[Response(503, retry_after="4")])
        with self.assertRaises(TimetableError) as raised:
            c._post_timetable({"searchDate": ""})
        self.assertEqual(503, raised.exception.http_status)
        self.assertEqual(4, raised.exception.retry_after)
        self.assertEqual(1, c.session.posts)

    def test_rejected_response_logs_safe_evidence_at_timetable_and_first_step(self):
        cases = (
            ("timetable", 403, "Access denied", "access_denied"),
            ("first_step", 403, "Access denied", "access_denied"),
            ("first_step", 500, "Internal Server Error", "server_busy"),
        )
        for stage, status, message, hint in cases:
            with self.subTest(stage=stage, status=status):
                private_text = f"<html>{message} user=private-netid token=private-token</html>"
                response = Response(status, private_text)
                response.headers["Content-Type"] = "text/html; charset=UTF-8"
                c = client(responses=[response])
                if stage == "timetable":
                    c.timetable = lambda *_args: c._post_timetable({"searchDate": ""})
                with self.assertRaises(BookingAttemptError) as raised:
                    c.book(SELECTION)
                event = next(e for e in raised.exception.events if e["stage"] == stage
                             and "http_status" in e)
                self.assertEqual(status, event["http_status"])
                self.assertEqual(hint, event["response_hint"])
                self.assertEqual("text/html", event["content_type"])
                self.assertEqual(16, len(event["body_sha256"]))
                self.assertNotIn("private-", json.dumps(raised.exception.events))

    def test_timetable_rate_limit_uses_server_wait(self):
        c = client(responses=[Response(429, retry_after="7")])
        with self.assertRaises(TimetableError) as raised:
            c._post_timetable({"searchDate": ""})
        self.assertEqual(429, raised.exception.http_status)
        self.assertEqual(7, raised.exception.retry_after)

    def test_busy_timetable_retries_then_books(self):
        target = {"centerId": "152", "activityId": "101", "date": SELECTION.date,
                  "from": SELECTION.start, "to": SELECTION.end, "center": "X202"}
        cfg = {"booking": {"enabled": True, "targets": [target],
                           "burst_seconds": 120, "retry_interval": 3}}
        reports, sleeps = [], []

        class FakeClient:
            calls = 0

            def book(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise BookingAttemptError("HTTP 503", "timetable", "transient_http",
                                              [{"stage": "timetable", "http_status": 503}],
                                              retry_after=4)
                return BookingOutcome("booked", "My Record confirmed", reason_code="confirmed")

        fake = FakeClient()
        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value={"processed": {}}), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt", side_effect=reports.append), \
                patch.object(monitor, "notify"), \
                patch.object(monitor, "client_from_env", return_value=fake), \
                patch.object(monitor.time, "sleep", side_effect=sleeps.append):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))
        self.assertEqual(2, fake.calls)
        self.assertEqual([4], sleeps)
        self.assertEqual("booked", reports[-1]["status"])

    def test_no_vacancy_reports_availability_without_post(self):
        c = client(available=False)
        with self.assertRaises(BookingAttemptError) as raised:
            c.book(SELECTION)
        self.assertEqual(("availability", "no_vacancy"),
                         (raised.exception.stage, raised.exception.reason_code))
        self.assertFalse(raised.exception.uncertain)
        self.assertEqual(0, c.session.posts)

    def test_confirmation_missing_reports_step_without_final_post(self):
        c = client(responses=[Response(200, "<html>No confirmation form</html>")])
        with self.assertRaises(BookingAttemptError) as raised:
            c.book(SELECTION)
        self.assertEqual("confirmation_page", raised.exception.stage)
        self.assertEqual(1, c.session.posts)

    def test_prepared_court_skips_release_timetable_and_uses_fresh_form(self):
        confirmation = (
            '<form action="make_book_submit.do" method="post">'
            '<input name="boMakeBookFacilities[0].facilityId" value="1601">'
            '<input name="CSRFToken" value="next-token"></form>'
        )
        c = client(available=False, responses=[
            Response(200, confirmation), Response(200, "Booking successful"),
        ])
        c.heartbeat = lambda force=False: False
        c.timetable = lambda *_args: self.fail("release timetable must be skipped")
        c._record_contains_booking = lambda *_args: True
        result = c.book(SELECTION, prepared_facility_id="1601")
        self.assertEqual("booked", result.status)
        self.assertEqual(2, c.session.posts)
        self.assertEqual("prepared", result.events[0]["stage"])

    def test_first_step_waits_longer_than_timetable(self):
        confirmation = (
            '<form action="make_book_submit.do" method="post">'
            '<input name="boMakeBookFacilities[0].facilityId" value="1601">'
            '<input name="CSRFToken" value="next-token"></form>'
        )
        c = client(responses=[Response(200, confirmation),
                              Response(200, "already been booked")])
        timeouts = []
        original_post = c.session.post

        def record_timeout(*args, **kwargs):
            timeouts.append(kwargs["timeout"])
            return original_post(*args, **kwargs)

        c.session.post = record_timeout
        c.book(SELECTION)
        self.assertEqual([CONFIRMATION_TIMEOUT, FINAL_TIMEOUT], timeouts)
        self.assertGreater(CONFIRMATION_TIMEOUT[1], 8)

    def test_final_timeout_is_uncertain_and_not_retried(self):
        confirmation = (
            '<form action="make_book_submit.do" method="post">'
            '<input name="boMakeBookFacilities[0].facilityId" value="1601">'
            '<input name="CSRFToken" value="next-token"></form>'
        )
        c = client(responses=[Response(200, confirmation), requests.Timeout("secret=abc"),
                              Response(200, "<table></table>"), Response(200, "<table></table>"),
                              Response(200, "<table></table>")])
        with patch("poss_client.time.sleep"), self.assertRaises(BookingAttemptError) as raised:
            c.book(SELECTION)
        self.assertEqual("final_submit", raised.exception.stage)
        self.assertTrue(raised.exception.uncertain)
        self.assertNotIn("secret=abc", str(raised.exception))
        self.assertEqual(2, c.session.posts)
        self.assertEqual(3, len([event for event in raised.exception.events
                                 if event["stage"] == "reconcile_record"]))

    def test_final_timeout_checks_record_without_reposting(self):
        confirmation = (
            '<form action="make_book_submit.do" method="post">'
            '<input name="boMakeBookFacilities[0].facilityId" value="1601">'
            '<input name="CSRFToken" value="next-token"></form>'
        )
        c = client(responses=[Response(200, confirmation), requests.ReadTimeout()])
        c._record_contains_booking = lambda *_args: True
        result = c.book(SELECTION)
        self.assertEqual("booked", result.status)
        self.assertTrue(result.verified)
        self.assertEqual(2, c.session.posts)

    def test_unrecognized_result_releases_other_targets_without_record_wait(self):
        confirmation = (
            '<form action="make_book_submit.do" method="post">'
            '<input name="boMakeBookFacilities[0].facilityId" value="1601">'
            '<input name="CSRFToken" value="next-token"></form>'
        )
        denial = '<html><div>Exceeded the maximum number of bookings</div>' + confirmation
        c = client(responses=[Response(200, confirmation), Response(200, denial)])
        def must_not_check_record(*_args):
            raise AssertionError("unknown_result must not block other final submits")
        c._record_contains_booking = must_not_check_record
        result = c.book(SELECTION)
        self.assertEqual("failed", result.status)
        self.assertEqual("unknown_result", result.reason_code)
        self.assertEqual([{"stage": "reconcile_record", "state": "skipped",
                           "reason": "unknown_result"}],
                         [e for e in result.events if e["stage"] == "reconcile_record"])
        detail = next(e for e in result.events if e["stage"] == "final_submit"
                      and "http_status" in e)
        self.assertEqual("booking_limit", detail["response_hint"])
        self.assertTrue(detail["confirmation_form"])
        self.assertNotIn("Exceeded the maximum", json.dumps(result.events))

    def test_final_http_error_is_not_treated_as_definitive_rejection(self):
        confirmation = (
            '<form action="make_book_submit.do" method="post">'
            '<input name="boMakeBookFacilities[0].facilityId" value="1601">'
            '<input name="CSRFToken" value="next-token"></form>'
        )
        c = client(responses=[Response(200, confirmation),
                              Response(503, "Booking unsuccessful"),
                              Response(200, "<table></table>"), Response(200, "<table></table>"),
                              Response(200, "<table></table>")])
        with patch("poss_client.time.sleep"):
            outcome = c.book(SELECTION)
        self.assertEqual("unknown", outcome.status)
        self.assertEqual("result_http_error", outcome.reason_code)
        self.assertEqual(2, c.session.posts)

    def test_unknown_final_redirect_logs_result_fingerprint_without_url_or_body(self):
        confirmation = (
            '<form action="make_book_submit.do" method="post">'
            '<input name="boMakeBookFacilities[0].facilityId" value="1601">'
            '<input name="CSRFToken" value="next-token"></form>'
        )
        c = client(responses=[
            Response(200, confirmation),
            Response(302, location="make_book_result.do?token=private-token"),
            Response(200, "<html>Unclear result for private-netid</html>"),
        ])
        c._record_contains_booking = lambda *_args: False
        with patch("poss_client.time.sleep"):
            outcome = c.book(SELECTION)
        self.assertEqual("failed", outcome.status)
        self.assertEqual("unknown_result", outcome.reason_code)
        final = next(e for e in outcome.events if e["stage"] == "final_submit"
                     and "http_status" in e)
        result = next(e for e in outcome.events if e["stage"] == "result_page")
        self.assertEqual("booking_result", final["redirect_target"])
        self.assertEqual(200, result["http_status"])
        self.assertEqual("unclassified", result["response_hint"])
        self.assertFalse(result["confirmation_form"])
        self.assertEqual(16, len(result["body_sha256"]))
        self.assertNotIn("private-", json.dumps(outcome.events))

    def test_first_step_timeout_is_not_assumed_to_be_a_safe_retry(self):
        c = client(responses=[requests.ReadTimeout("secret=abc")])
        with self.assertRaises(BookingAttemptError) as raised:
            c.book(SELECTION)
        self.assertEqual(("first_step", True),
                         (raised.exception.stage, raised.exception.uncertain))
        self.assertEqual(1, c.session.posts)

    def test_final_submit_intent_is_persisted_before_post(self):
        confirmation = (
            '<form action="make_book_submit.do" method="post">'
            '<input name="boMakeBookFacilities[0].facilityId" value="1601">'
            '<input name="CSRFToken" value="next-token"></form>'
        )
        c = client(responses=[Response(200, confirmation), requests.ReadTimeout(),
                              Response(200, "<table></table>"), Response(200, "<table></table>"),
                              Response(200, "<table></table>")])
        events = []

        def before_submit():
            events.append(("persisted", c.session.posts))

        original_post = c.session.post

        def track_post(*args, **kwargs):
            events.append(("post", c.session.posts))
            return original_post(*args, **kwargs)

        c.session.post = track_post
        with patch("poss_client.time.sleep"), self.assertRaises(BookingAttemptError):
            c.book(SELECTION, on_final_submit=before_submit)
        self.assertEqual([("post", 0), ("persisted", 1), ("post", 1)], events)

    def test_first_step_progress_is_logged_before_request(self):
        c = client(responses=[requests.ReadTimeout()])
        events = []
        original_post = c.session.post

        def track_post(*args, **kwargs):
            events.append(("post", c.session.posts))
            return original_post(*args, **kwargs)

        c.session.post = track_post
        with self.assertRaises(BookingAttemptError):
            c.book(SELECTION, on_first_step=lambda: events.append(("dispatching", c.session.posts)))
        self.assertEqual([("dispatching", 0), ("post", 0)], events)

    def test_success_records_stage_timing_and_verification(self):
        confirmation = (
            '<form action="make_book_submit.do" method="post">'
            '<input name="boMakeBookFacilities[0].facilityId" value="1601">'
            '<input name="CSRFToken" value="next-token"></form>'
        )
        c = client(responses=[
            Response(200, confirmation),
            Response(302, location="make_book_result.do"),
            Response(200, "You have successfully confirmed the booking"),
        ])
        c._record_contains_booking = lambda *_args: True
        result = c.book(SELECTION)
        self.assertEqual(("booked", "confirmed"), (result.status, result.reason_code))
        self.assertTrue(result.verified)
        self.assertIn("final_submit", [event["stage"] for event in result.events])
        self.assertTrue(all(event["duration_ms"] >= 0 for event in result.events
                            if "duration_ms" in event))
        self.assertEqual(2, c.session.posts)

    def test_taken_court_is_distinguished_from_generic_rejection(self):
        confirmation = (
            '<form action="make_book_submit.do" method="post">'
            '<input name="boMakeBookFacilities[0].facilityId" value="1601">'
            '<input name="CSRFToken" value="next-token"></form>'
        )
        c = client(responses=[Response(200, confirmation),
                              Response(200, "This facility has already been booked")])
        result = c.book(SELECTION)
        self.assertEqual(("failed", "slot_taken"), (result.status, result.reason_code))

    def test_taken_court_rechecks_other_courts_in_release_window(self):
        target = {"centerId": "3", "activityId": "2", "date": "2026-10-16",
                  "from": "15:30", "to": "16:30", "center": "Kwong"}
        cfg = {"booking": {"enabled": True, "targets": [target],
                           "burst_seconds": 120, "retry_interval": 3}}
        calls, sleeps, reports = [], [], []

        class FakeClient:
            def book(self, *_args, **_kwargs):
                calls.append(1)
                if len(calls) == 1:
                    return BookingOutcome(
                        "failed", "court taken", stage="final_submit", reason_code="slot_taken")
                return BookingOutcome("booked", "My Record confirmed", reason_code="confirmed")

        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value={"processed": {}}), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt", side_effect=reports.append), \
                patch.object(monitor, "notify"), \
                patch.object(monitor, "client_from_env", return_value=FakeClient()), \
                patch.object(monitor.time, "sleep", side_effect=sleeps.append):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))
        self.assertEqual(2, len(calls))
        self.assertEqual([0.5], sleeps)
        self.assertEqual("booked", reports[0]["status"])

    def test_latest_cancelled_record_does_not_verify_old_booking(self):
        row = (f"{SELECTION.center_name} 14 Oct 2026 (Wed) "
               "08:30 AM - 09:30 AM")
        c = PossClient("demo", "demo")
        c.session = SimpleNamespace(get=lambda *_args, **_kwargs: Response(
            200, f"<table><tr><td>{row} Individual Booking Refund (Cancelled)</td></tr>"
                 f"<tr><td>{row} Individual Booking (Confirmed)</td></tr></table>"
        ))
        self.assertFalse(c._record_contains_booking(SELECTION, SELECTION.center_name))

    def test_monitor_report_and_deduplicate_failure_notification(self):
        target = {
            "datasetId": "18", "activityId": "101", "centerId": "152",
            "facilityId": "1601", "date": SELECTION.date,
            "from": SELECTION.start, "to": SELECTION.end,
            "activity": SELECTION.activity_name, "center": SELECTION.center_name,
        }
        cfg = {"booking": {"enabled": True, "targets": [target],
                           "lead_days": 7, "trigger_time": "08:30",
                           "burst_seconds": 0, "retry_interval": 3}}
        state = {"processed": {}}
        reports, notifications = [], []

        class FakeClient:
            def book(self, *_args, **_kwargs):
                raise BookingAttemptError("没有名额", "availability", "no_vacancy",
                                          [{"stage": "availability", "available": 0}])

        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt", side_effect=reports.append), \
                patch.object(monitor, "notify", side_effect=lambda *args: notifications.append(args) or True), \
                patch.object(monitor, "client_from_env", return_value=FakeClient()):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))

        self.assertEqual(2, len(reports))
        self.assertEqual(1, len(notifications))
        self.assertEqual("no_vacancy", reports[-1]["reason_code"])
        self.assertEqual({}, state["processed"])

    def test_report_file_contains_no_raw_result_page(self):
        report = {"target": "X202", "status": "failed", "stage": "availability",
                  "reason_code": "no_vacancy", "message": "没有名额",
                  "release_lag_ms": 50, "attempts": 2,
                  "events": [{"stage": "availability", "available": 0}]}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(monitor, "LOG_FILE", str(Path(tmp) / "attempts.jsonl")), \
                patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(Path(tmp) / "summary.md")}):
            monitor.report_attempt(report)
            saved = json.loads((Path(tmp) / "attempts.jsonl").read_text())
            summary = (Path(tmp) / "summary.md").read_text()
        self.assertEqual("no_vacancy", saved["reason_code"])
        self.assertIn("没有名额", summary)

    def test_missed_release_is_reported_without_request(self):
        target = {"centerId": "152", "activityId": "101", "date": "2020-10-14",
                  "from": "08:30", "to": "09:30", "center": "X202"}
        cfg = {"booking": {"enabled": True, "targets": [target],
                           "lead_days": 7, "trigger_time": "08:30"}}
        state = {"processed": {}}
        reports = []

        class FakeClient:
            def book(self, *_args, **_kwargs):
                self.fail("must not book a missed target")

        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt", side_effect=reports.append), \
                patch.object(monitor, "notify", return_value=True), \
                patch.object(monitor, "client_from_env", return_value=FakeClient()):
            monitor.run(SimpleNamespace(force=False, dry_run=False, wait=False))

        self.assertEqual("missed_release_window", reports[0]["reason_code"])
        self.assertEqual(0, reports[0]["attempts"])
        self.assertEqual("failed", next(iter(state["processed"].values()))["status"])

    def test_no_vacancy_retries_only_before_submit(self):
        target = {"datasetId": "18", "activityId": "101", "centerId": "152",
                  "facilityId": "1601", "date": SELECTION.date,
                  "from": SELECTION.start, "to": SELECTION.end, "center": "X202"}
        cfg = {"booking": {"enabled": True, "targets": [target],
                           "lead_days": 7, "trigger_time": "08:30",
                           "burst_seconds": 10, "retry_interval": 1}}
        state = {"processed": {}}
        reports = []

        class FakeClient:
            calls = 0

            def book(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise BookingAttemptError("无名额", "availability", "no_vacancy",
                                              [{"stage": "availability", "available": 0}])
                return BookingOutcome(
                    status="booked", message="My Record 已确认", stage="final_submit",
                    reason_code="confirmed", center="X202",
                    events=[{"stage": "verify_record", "confirmed": True}],
                    result_html="raw private result",
                )

        fake = FakeClient()
        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state"), \
                patch.object(monitor, "report_attempt", side_effect=reports.append), \
                patch.object(monitor, "notify") as notify, \
                patch.object(monitor, "client_from_env", return_value=fake), \
                patch.object(monitor.time, "sleep"):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))

        self.assertEqual(2, fake.calls)
        self.assertEqual("booked", reports[0]["status"])
        self.assertEqual(2, reports[0]["attempts"])
        self.assertNotIn("raw private result", json.dumps(state))
        notify.assert_not_called()

    def test_uncertain_final_submit_stops_and_records_state(self):
        target = {"centerId": "152", "activityId": "101", "date": SELECTION.date,
                  "from": SELECTION.start, "to": SELECTION.end, "center": "X202"}
        cfg = {"booking": {"enabled": True, "targets": [target],
                           "lead_days": 7, "trigger_time": "08:30",
                           "burst_seconds": 120, "retry_interval": 1}}
        state = {"processed": {}}
        reports = []
        saved = []

        class FakeClient:
            calls = 0

            def book(self, *_args, on_final_submit=None, **_kwargs):
                self.calls += 1
                on_final_submit()
                raise BookingAttemptError(
                    "Timeout: POSS 网络请求失败", "final_submit", "network_error",
                    [{"stage": "final_submit", "state": "sending"}], uncertain=True,
                )

        fake = FakeClient()
        with patch.object(monitor, "load_config", return_value=cfg), \
                patch.object(monitor, "load_state", return_value=state), \
                patch.object(monitor, "save_state",
                             side_effect=lambda value: saved.append(json.loads(json.dumps(value)))), \
                patch.object(monitor, "report_attempt", side_effect=reports.append), \
                patch.object(monitor, "notify", return_value=True), \
                patch.object(monitor, "client_from_env", return_value=fake):
            monitor.run(SimpleNamespace(force=True, dry_run=False, wait=False))
            monitor.run(SimpleNamespace(force=False, dry_run=False, wait=False))

        self.assertEqual(1, fake.calls)
        self.assertTrue(any(
            entry["status"] == "final_sent"
            for snapshot in saved for entry in snapshot["campaigns"].values()
        ))
        self.assertEqual("unknown", reports[0]["status"])
        self.assertEqual("unknown", next(iter(state["processed"].values()))["status"])


if __name__ == "__main__":
    unittest.main()
