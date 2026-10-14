import unittest
from unittest.mock import patch

import calendar_sync


class CalendarSyncTest(unittest.TestCase):
    def test_confirmed_booking_uses_snapshot_and_deduplicates_state_sections(self):
        key = "18|2|3||2026-10-22|15:30|16:30|INDV|court:123"
        booking = {
            "account_id": "2", "activity": "Badminton",
            "center": "Kwong On Jubilee Sports Centre", "facility": "Court 3",
            "facility_id": "123", "date": "2026-10-22",
            "start": "15:30", "end": "16:30",
        }
        state = {
            "processed": {key: {"status": "booked", "account_id": "2",
                                "booking": booking}},
            "campaigns": {key: {"status": "booked", "account_id": "2",
                                "booking": booking}},
            "account_activities": {"2|2026-10-22|18|2": {"status": "booked"}},
        }
        events = calendar_sync.confirmed_bookings(state, {})

        self.assertEqual(1, len(events))
        event = next(iter(events.values()))
        self.assertEqual("Court 3", event["facility"])
        self.assertEqual("2", event["account"])
        args = calendar_sync.event_args(event, "POSS 预约")
        self.assertIn(event["marker"], args[3])
        self.assertIn("Court 3", args[1])
        self.assertEqual(15, len(args))

    def test_unknown_or_unsubmitted_booking_never_appears(self):
        key = "18|2|3||2026-10-22|15:30|16:30|INDV|court:123"
        state = {"campaigns": {
            key: {"status": "final_sent", "account_id": "1"},
            key.replace("123", "456"): {"status": "unknown", "account_id": "2"},
        }}
        self.assertEqual({}, calendar_sync.confirmed_bookings(state, {}))

    def test_legacy_booking_falls_back_to_center_after_config_date_changes(self):
        key = "18|2|3||2026-10-22|15:30|16:30|INDV|court:123"
        config = {"booking": {"targets": [{
            "datasetId": "18", "activityId": "2", "centerId": "3",
            "facilityId": "", "date": "2026-10-23", "from": "15:30",
            "to": "16:30", "activity": "Badminton", "center": "Kwong",
        }]}}
        state = {"processed": {key: {"status": "booked", "account_id": "1"}}}
        event = next(iter(calendar_sync.confirmed_bookings(state, config).values()))

        self.assertEqual("Badminton", event["activity"])
        self.assertEqual("Kwong", event["center"])
        self.assertEqual("123", event["facility"])
        self.assertEqual("2026-10-22", event["date"])

    def test_sync_uses_remote_and_local_confirmations_without_writing_dry_run(self):
        remote = {"processed": {
            "18|2|3||2026-10-22|15:30|16:30|INDV|court:123":
                {"status": "booked", "account_id": "1"},
        }}
        with patch.object(calendar_sync, "remote_data", return_value=(remote, {})), \
                patch.object(calendar_sync, "ROOT", calendar_sync.ROOT / "missing"), \
                patch.object(calendar_sync, "add_event") as writer:
            calendar_sync.sync_once("POSS 预约", dry_run=True)
            writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
