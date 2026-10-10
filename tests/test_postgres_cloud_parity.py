import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

import database
import database_pg
import main


class PostgresCloudParityTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.pool = MagicMock()
        self.pool.connection.return_value.__enter__.return_value = self.connection
        patcher = patch.object(database_pg, "get_pool", return_value=self.pool)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_postgres_api_imports_without_opening_database_pool(self):
        env = dict(os.environ, DB_BACKEND="postgres")
        result = subprocess.run(
            [sys.executable, "-c", "import main, database_pg; assert database_pg._pool is None; print(main.app.title)"],
            cwd=Path(__file__).resolve().parents[1], env=env,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("EToE User API", result.stdout)

    def test_assignment_and_update_signatures_match_sqlserver(self):
        for name in (
            "get_assigned_active_events", "get_event_counter_assignments",
            "replace_event_counter_assignments", "is_event_assigned_to_staff",
            "get_event_transactions", "get_transaction_denomination_details",
            "update_user", "get_user_change_history",
        ):
            with self.subTest(name=name):
                self.assertEqual(inspect.signature(getattr(database_pg, name)),
                                 inspect.signature(getattr(database, name)))

    def test_assignment_list_shape(self):
        self.connection.execute.return_value.fetchall.side_effect = [
            [{"staff_id": 2, "display_name": "Counter 1"}], [{"staff_id": 2}],
        ]
        self.assertEqual(database_pg.get_event_counter_assignments(7), {
            "counters": [{"staff_id": 2, "display_name": "Counter 1"}],
            "assigned_counter_ids": [2],
        })

    def test_replacement_locks_event_before_deleting_and_inserting(self):
        self.connection.execute.return_value.fetchone.side_effect = [
            {"event_id": 7}, {"counter_count": 2},
        ]
        database_pg.replace_event_counter_assignments(7, [2, 3], 1)
        calls = self.connection.execute.call_args_list
        self.assertIn("FOR UPDATE", calls[0].args[0])
        self.assertEqual(calls[1].args[1], ([2, 3],))
        self.assertIn("DELETE", calls[2].args[0])
        self.assertEqual([call.args[1] for call in calls[3:]], [(7, 2, 1), (7, 3, 1)])

    def test_invalid_replacement_does_not_delete_assignments(self):
        for rows in ([None], [{"event_id": 7}, {"counter_count": 1}]):
            with self.subTest(rows=rows):
                self.connection.reset_mock()
                self.connection.execute.return_value.fetchone.side_effect = rows
                with self.assertRaises(ValueError):
                    database_pg.replace_event_counter_assignments(7, [2, 3], 1)
                self.assertFalse(any("DELETE" in call.args[0]
                                     for call in self.connection.execute.call_args_list))
                self.assertIs(self.pool.connection.return_value.__exit__.call_args.args[0], ValueError)

    def test_empty_assignment_set_clears_event(self):
        self.connection.execute.return_value.fetchone.return_value = {"event_id": 7}
        database_pg.replace_event_counter_assignments(7, [], 1)
        self.assertEqual(self.connection.execute.call_count, 2)
        self.assertIn("DELETE", self.connection.execute.call_args.args[0])

    def test_assigned_events_and_access_are_scoped_to_active_event(self):
        with patch.object(database_pg, "_all", return_value=[]) as query:
            self.assertEqual(database_pg.get_assigned_active_events(2), [])
        self.assertEqual(query.call_args.args[1], (2,))
        self.assertIn("e.is_active", query.call_args.args[0])
        with patch.object(database_pg, "_one", return_value=None) as lookup:
            self.assertFalse(database_pg.is_event_assigned_to_staff(7, 2))
        self.assertEqual(lookup.call_args.args[1], (7, 2))
        self.assertIn("e.is_active", lookup.call_args.args[0])

    def test_denomination_details_preserve_ownership_and_empty_counts(self):
        for rows, expected in (
            ([], None),
            ([{"event_id": 7, "staff_id": None, "denomination": None, "note_count": None}],
             {"event_id": 7, "staff_id": None, "denominations": {}}),
            ([{"event_id": 7, "staff_id": 2, "denomination": 500, "note_count": 2}],
             {"event_id": 7, "staff_id": 2, "denominations": {500: 2}}),
        ):
            with self.subTest(rows=rows):
                with patch.object(database_pg, "_all", return_value=rows) as query:
                    self.assertEqual(database_pg.get_transaction_denomination_details(7, 99), expected)
                self.assertEqual(query.call_args.args[1], (7, 99))
                self.assertIn("entry_type = 'CONTRIBUTED'", query.call_args.args[0])

    def test_event_report_filters_contribution_rows(self):
        rows = [{"husband_name": "Family", "wife_name": None, "native_place": "Town",
                 "phone_number": "test-phone", "amount": 500}]
        with patch.object(database_pg, "_all", return_value=rows) as query:
            self.assertEqual(database_pg.get_event_transactions(7), rows)
        self.assertEqual(query.call_args.args[1], (7,))
        self.assertIn("entry_type = 'CONTRIBUTED'", query.call_args.args[0])

    def test_counter_collections_include_family_id_for_sidebar_edit(self):
        for event_id in (None, 7):
            with self.subTest(event_id=event_id):
                row = {"user_id": 5, "transaction_id": 99, "amount": 500}
                with patch.object(database_pg, "_all", return_value=[row]) as query:
                    self.assertEqual(database_pg.get_staff_collections(2, event_id), [row])
                sql, params = query.call_args.args
                self.assertIn("u.id AS user_id", sql)
                self.assertEqual(params, [2] if event_id is None else [2, 7])
                self.assertIn("tc.staff_id = %s", sql)
                self.assertIn("entry_type = 'CONTRIBUTED'", sql)

    def test_profile_update_endpoint_attributes_edits_to_authenticated_staff(self):
        request = main.UserRequest(husband_name=" New ", phone_number="test-phone")
        for role in ("admin", "counter"):
            with self.subTest(role=role):
                with patch.object(main, "update_user", return_value={"id": 5}) as update:
                    result = main.update_user_endpoint(5, request, {"staff_id": 2, "role": role})
                self.assertEqual(result, {"id": 5})
                self.assertEqual(update.call_args.kwargs, {
                    "user_id": 5, "changed_by_staff_id": 2, **main.clean_user_fields(request),
                })

    def test_profile_update_endpoint_records_postgres_history(self):
        self.connection.execute.return_value.fetchone.side_effect = [
            self.profile("Old"), self.profile("New"),
        ]
        request = main.UserRequest(husband_name="New", phone_number="test-phone")
        with patch.object(main, "update_user", database_pg.update_user):
            result = main.update_user_endpoint(5, request, {"staff_id": 2, "role": "counter"})
        self.assertEqual(result["husband_name"], "New")
        history = self.connection.execute.call_args
        self.assertIn("INSERT INTO user_change_history", history.args[0])
        self.assertEqual(history.args[1][:2], (5, 2))

    def update_profile(self, changed_by=1):
        return database_pg.update_user(
            5, "New", None, None, "test-phone", None, None, None, None, None, None,
            changed_by_staff_id=changed_by,
        )

    def profile(self, name):
        return {
            "id": 5, "husband_name": name, "wife_name": None, "husband_job": None,
            "phone_number": "test-phone", "native_place": None, "current_place": None,
            "wife_job": None, "others": None, "initial": None, "notes": None,
            "is_thaimama": False, "password_hash": "private",
        }

    def test_profile_history_written_atomically_without_password_hash(self):
        self.connection.execute.return_value.fetchone.side_effect = [
            self.profile("Old"), self.profile("New"),
        ]
        result = self.update_profile()
        self.assertNotIn("password_hash", result)
        calls = self.connection.execute.call_args_list
        self.assertIn("FOR UPDATE", calls[0].args[0])
        self.assertIn("INSERT INTO user_change_history", calls[2].args[0])
        user_id, staff_id, before, after = calls[2].args[1]
        self.assertEqual((user_id, staff_id), (5, 1))
        self.assertEqual(json.loads(before)["husband_name"], "Old")
        self.assertEqual(json.loads(after)["husband_name"], "New")
        self.assertNotIn("password_hash", before + after)

    def test_unchanged_or_unattributed_profile_has_no_history_insert(self):
        for old_name, staff_id in (("New", 1), ("Old", None)):
            with self.subTest(old_name=old_name, staff_id=staff_id):
                self.connection.reset_mock()
                self.connection.execute.return_value.fetchone.side_effect = [
                    self.profile(old_name), self.profile("New"),
                ]
                self.update_profile(staff_id)
                self.assertEqual(self.connection.execute.call_count, 2)

    def test_missing_profile_is_not_updated(self):
        self.connection.execute.return_value.fetchone.return_value = None
        self.assertIsNone(self.update_profile())
        self.assertEqual(self.connection.execute.call_count, 1)

    def test_history_failure_propagates_for_transaction_rollback(self):
        first = MagicMock()
        first.fetchone.return_value = self.profile("Old")
        second = MagicMock()
        second.fetchone.return_value = self.profile("New")
        self.connection.execute.side_effect = [first, second, RuntimeError("history insert failed")]
        with self.assertRaisesRegex(RuntimeError, "history insert failed"):
            self.update_profile()
        self.assertIs(self.pool.connection.return_value.__exit__.call_args.args[0], RuntimeError)

    def test_history_search_is_parameterized_and_limited(self):
        with patch.object(database_pg, "_all", return_value=[]) as query:
            database_pg.get_user_change_history("Family")
        self.assertEqual(query.call_args.args[1], ["%Family%"] * 5)
        self.assertIn("LIMIT 200", query.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
