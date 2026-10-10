import asyncio
import json
import unittest
from decimal import Decimal
from unittest.mock import patch

import main


class CounterDenominationSummaryTests(unittest.TestCase):
    def setUp(self):
        self.sessions = patch.dict(main.STAFF_SESSIONS, {
            "counter-test": {"staff_id": 1, "display_name": "C1", "role": "counter"},
            "admin-test": {"staff_id": 9, "display_name": "Admin", "role": "admin"},
        }, clear=True)
        self.sessions.start()
        self.addCleanup(self.sessions.stop)

    def request(self, token=None, path="/events/7/counter-denomination-summary"):
        async def execute():
            messages = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                messages.append(message)

            await main.app({
                "type": "http", "asgi": {"version": "3.0"},
                "http_version": "1.1", "method": "GET", "scheme": "http",
                "path": path, "raw_path": path.encode(), "query_string": b"",
                "root_path": "", "server": ("test", 80), "client": ("test", 1),
                "headers": [(b"authorization", f"Bearer {token}".encode())] if token else [],
            }, receive, send)
            status = next(m["status"] for m in messages if m["type"] == "http.response.start")
            body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            return status, json.loads(body)

        return asyncio.run(execute())

    def test_assigned_counter_sees_all_counter_totals_for_only_selected_event(self):
        totals = [
            {"staff_id": 1, "display_name": "C1", "total_amount": Decimal("3500"), "contribution_count": 2},
            {"staff_id": 2, "display_name": "C2", "total_amount": Decimal("7000"), "contribution_count": 3},
            {"staff_id": 3, "display_name": "C3", "total_amount": 0, "contribution_count": 0},
        ]
        denominations = [
            {"staff_id": 1, "denominations": {500: 3, 200: 10}},
            {"staff_id": 2, "denominations": {500: 10, 100: 10, 50: 20}},
        ]
        with patch.object(main, "is_event_assigned_to_staff", return_value=True) as access, \
             patch.object(main, "get_staff_collections_summary", return_value=totals) as summary, \
             patch.object(main, "get_staff_denomination_summary", return_value=denominations) as deno:
            status, rows = self.request("counter-test")
        self.assertEqual(status, 200)
        access.assert_called_once_with(7, 1)
        summary.assert_called_once_with(7)
        deno.assert_called_once_with(7)
        self.assertEqual([r["display_name"] for r in rows], ["C1", "C2", "C3"])
        self.assertEqual(rows[1]["denominations"], {"500": 10, "100": 10, "50": 20})
        self.assertEqual(rows[2]["denominations"], {})
        self.assertEqual(Decimal(str(rows[0]["total_amount"])), Decimal("3500"))
        self.assertEqual(set(rows[0]), {"staff_id", "display_name", "total_amount", "denominations"})

    def test_unassigned_counter_is_denied_before_reading_totals(self):
        with patch.object(main, "is_event_assigned_to_staff", return_value=False), \
             patch.object(main, "get_staff_collections_summary") as summary, \
             patch.object(main, "get_staff_denomination_summary") as deno:
            status, _ = self.request("counter-test")
        self.assertEqual(status, 403)
        summary.assert_not_called()
        deno.assert_not_called()

    def test_login_required(self):
        with patch.object(main, "get_staff_collections_summary") as summary:
            for token in (None, "invalid-token"):
                self.assertEqual(self.request(token)[0], 401)
        summary.assert_not_called()

    def test_admin_can_view_event_and_empty_counters(self):
        with patch.object(main, "is_event_assigned_to_staff") as access, \
             patch.object(main, "get_staff_collections_summary", return_value=[]), \
             patch.object(main, "get_staff_denomination_summary", return_value=[]):
            self.assertEqual(self.request("admin-test"), (200, []))
        access.assert_not_called()

    def test_existing_all_event_admin_endpoint_stays_admin_only(self):
        self.assertEqual(self.request("counter-test", "/staff/collections-summary")[0], 403)
        self.assertEqual(self.request("counter-test", "/staff/collections-denomination-summary")[0], 403)


if __name__ == "__main__":
    unittest.main()
