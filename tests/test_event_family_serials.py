import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import database
import database_pg
import main


class EventFamilySerialApiTests(unittest.TestCase):
    def test_save_returns_database_serial_and_uses_it_for_message(self):
        request = main.ContributionRequest(contributor_id=1, receiver_id=2, event_id=3, amount=500)
        tasks = main.BackgroundTasks()
        staff = {"staff_id": 4, "display_name": "C1", "role": "admin"}
        with patch.object(main, "process_contribution", return_value=(True, "Saved", 11, 99)):
            result = main.create_contribution_endpoint(request, tasks, staff)
        self.assertEqual(result["serial_number"], 11)
        self.assertEqual(result["transaction_id"], 99)
        self.assertEqual(tasks.tasks[0].args[-1], 11)

    def test_preview_is_shared_but_existing_family_serial_is_returned(self):
        status = {"collected_family_count": 13, "next_serial_number": 14, "serial_number": 11}
        with patch.object(main, "get_event_serial_status", return_value=status) as lookup:
            result = main.next_serial_number_endpoint(3, 1, {"role": "admin"})
        lookup.assert_called_once_with(3, 1)
        self.assertEqual(result, status)

    def test_failed_contribution_has_no_receipt_message(self):
        tasks = main.BackgroundTasks()
        request = main.ContributionRequest(contributor_id=1, receiver_id=2, event_id=3, amount=500)
        with patch.object(main, "process_contribution", return_value=(False, "Failed", None, None)):
            with self.assertRaises(main.HTTPException):
                main.create_contribution_endpoint(request, tasks, {"staff_id": 4, "role": "admin"})
        self.assertEqual(tasks.tasks, [])


class PostgresFamilySerialTests(unittest.TestCase):
    def save_with_rows(self, rows):
        connection = MagicMock()
        connection.execute.return_value.fetchone.side_effect = rows
        pool = MagicMock()
        pool.connection.return_value.__enter__.return_value = connection
        with patch.object(database_pg, "get_pool", return_value=pool):
            result = database_pg.process_contribution(1, 2, 3, 500, {500: 1}, 4)
        calls = [call.args[0] for call in connection.execute.call_args_list]
        self.assertIn("FOR UPDATE", calls[0])
        return result, calls

    def test_new_family_allocated_under_event_lock(self):
        result, calls = self.save_with_rows([
            {"event_id": 3}, {"transaction_id": 99}, None, {"next_number": 11},
        ])
        self.assertEqual(result, (True, "Contribution recorded successfully.", 11, 99))
        self.assertTrue(any("INSERT INTO event_contributor_serials" in sql for sql in calls))

    def test_repeat_family_does_not_allocate_another_number(self):
        result, calls = self.save_with_rows([
            {"event_id": 3}, {"transaction_id": 100}, {"serial_number": 11},
        ])
        self.assertEqual(result[2:], (11, 100))
        self.assertFalse(any("INSERT INTO event_contributor_serials" in sql for sql in calls))


@unittest.skipUnless(os.getenv("RUN_SQLSERVER_SERIAL_TESTS") == "1", "Requires local SQL Server; creates and removes isolated fixtures.")
class SqlServerConcurrentSerialTests(unittest.TestCase):
    def setUp(self):
        self.event_ids = []
        self.user_ids = []
        self.addCleanup(self.cleanup)
        suffix = uuid.uuid4().hex[:12]
        with database.get_connection() as connection:
            cursor = connection.cursor()
            for i in range(16):
                cursor.execute(
                    "INSERT INTO dbo.Users (husband_name, phone_number) OUTPUT INSERTED.id VALUES (?, ?)",
                    "Serial concurrency test", f"test-{suffix}-{i}",
                )
                self.user_ids.append(cursor.fetchone()[0])
            for i in range(2):
                cursor.execute(
                    "INSERT INTO dbo.event (event_name, event_date, host_user_id) OUTPUT INSERTED.event_id VALUES (?, CONVERT(date, SYSDATETIME()), ?)",
                    f"Serial concurrency test {suffix}-{i}", self.user_ids[0],
                )
                self.event_ids.append(cursor.fetchone()[0])
            connection.commit()

    def cleanup(self):
        with database.get_connection() as connection:
            cursor = connection.cursor()
            for event_id in self.event_ids:
                cursor.execute("SELECT DISTINCT transaction_id FROM dbo.journal_entries WHERE event_id = ?", event_id)
                transaction_ids = [row[0] for row in cursor.fetchall()]
                for transaction_id in transaction_ids:
                    cursor.execute("DELETE FROM dbo.transaction_denominations WHERE transaction_id = ?", transaction_id)
                    cursor.execute("DELETE FROM dbo.transaction_collectors WHERE transaction_id = ?", transaction_id)
                cursor.execute("DELETE FROM dbo.event_contributor_serials WHERE event_id = ?", event_id)
                cursor.execute("DELETE FROM dbo.journal_entries WHERE event_id = ?", event_id)
                for transaction_id in transaction_ids:
                    cursor.execute("DELETE FROM dbo.transactions WHERE transaction_id = ?", transaction_id)
                cursor.execute("DELETE FROM dbo.event WHERE event_id = ?", event_id)
            for user_id in self.user_ids:
                cursor.execute("DELETE FROM dbo.Users WHERE id = ?", user_id)
            connection.commit()

    def save(self, user_id, event_id=None, staff_id=None):
        return database.process_contribution(
            user_id, self.user_ids[0], event_id or self.event_ids[0], 500,
            denominations={500: 1}, staff_id=staff_id,
        )

    def test_parallel_counters_repeats_event_isolation_and_failed_save(self):
        for number, user_id in enumerate(self.user_ids[1:11], start=1):
            result = self.save(user_id)
            self.assertTrue(result[0], result[1])
            self.assertEqual(result[2], number)
        self.assertEqual(database.get_event_serial_status(self.event_ids[0])["next_serial_number"], 11)
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(self.save, self.user_ids[11:14]))
        self.assertTrue(all(r[0] for r in results), results)
        self.assertEqual(sorted(r[2] for r in results), [11, 12, 13])
        self.assertEqual(len({r[3] for r in results}), 3)
        repeat = self.save(self.user_ids[11])
        self.assertTrue(repeat[0], repeat[1])
        self.assertEqual(repeat[2], results[0][2])
        self.assertEqual(database.get_event_serial_status(self.event_ids[0])["collected_family_count"], 13)
        failed = self.save(self.user_ids[14], staff_id=-1)
        self.assertFalse(failed[0])
        self.assertEqual(failed[2:], (None, None))
        self.assertEqual(database.get_event_serial_status(self.event_ids[0])["next_serial_number"], 14)
        self.assertEqual(database.get_event_denomination_summary(self.event_ids[0])["total_amount"], 7000)
        saved = self.save(self.user_ids[14])
        self.assertTrue(saved[0], saved[1])
        self.assertEqual(saved[2], 14)
        second_event = self.save(self.user_ids[11], self.event_ids[1])
        self.assertTrue(second_event[0], second_event[1])
        self.assertEqual(second_event[2], 1)


if __name__ == "__main__":
    unittest.main()
