import hashlib
import json
import os

import pyodbc


def get_connection():
    connection_string = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        r"SERVER=localhost\SQLEXPRESS;"
        "DATABASE=MoiSei;"
        "Trusted_Connection=yes;"
        "TrustServerCertificate=yes;"
    )

    return pyodbc.connect(connection_string)


def hash_password(password: str) -> str:
    """Salted PBKDF2-SHA256 hash, same scheme Moi_Sei uses so existing family passwords keep working."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify against the pbkdf2_sha256$iterations$salt_hex$digest_hex format."""
    try:
        algorithm, iterations, salt_hex, digest_hex = stored_hash.split("$")
    except (ValueError, AttributeError):
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
    return candidate.hex() == digest_hex


def create_staff_account(display_name: str, pin: str, role: str = "counter") -> dict:
    """Create a new Admin Panel login (PIN-based). role is 'counter' or 'admin'."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO dbo.staff_accounts (display_name, pin_hash, role)
            OUTPUT INSERTED.staff_id, INSERTED.display_name, INSERTED.role, INSERTED.is_active, INSERTED.created_at
            VALUES (?, ?, ?)
            """,
            display_name,
            hash_password(pin),
            role,
        )
        row = cursor.fetchone()
        connection.commit()
        columns = [column[0] for column in cursor.description]
        return dict(zip(columns, row))
    finally:
        connection.close()


def get_staff_by_pin(pin: str) -> dict | None:
    """Find the active staff account whose PIN matches (iterates active accounts; staff count is expected to stay small)."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT staff_id, display_name, pin_hash, role FROM dbo.staff_accounts WHERE is_active = 1")
        for staff_id, display_name, pin_hash, role in cursor.fetchall():
            if verify_password(pin, pin_hash):
                return {"staff_id": staff_id, "display_name": display_name, "role": role}
        return None
    finally:
        connection.close()


def list_staff_accounts() -> list[dict]:
    """All staff accounts for the admin's Manage Counters panel (never exposes pin_hash)."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT staff_id, display_name, role, is_active, created_at FROM dbo.staff_accounts ORDER BY staff_id"
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def get_staff_collections(staff_id: int, event_id: int | None = None) -> list[dict]:
    """Contributions personally recorded by this staff login (optionally scoped to one event)."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        query = """
                 SELECT je.transaction_id, je.amount, je.event_id, u.id AS user_id,
                     u.husband_name AS contributor_name,
                   u.phone_number AS contributor_phone, tc.collected_at
            FROM dbo.transaction_collectors tc
            JOIN dbo.journal_entries je ON je.transaction_id = tc.transaction_id AND je.entry_type = 'CONTRIBUTED'
            JOIN dbo.Users u ON u.id = je.user_id
            WHERE tc.staff_id = ?
        """
        params: list = [staff_id]
        if event_id is not None:
            query += " AND je.event_id = ?"
            params.append(event_id)
        query += " ORDER BY tc.collected_at DESC"
        cursor.execute(query, *params)
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def get_staff_collections_summary(event_id: int | None = None) -> list[dict]:
    """Totals grouped by counter login, for the admin's cross-counter reconciliation view."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        event_join_filter = " AND je.event_id = ?" if event_id is not None else ""
        query = f"""
            SELECT sa.staff_id, sa.display_name,
                   COUNT(je.transaction_id) AS contribution_count,
                   ISNULL(SUM(je.amount), 0) AS total_amount
            FROM dbo.staff_accounts sa
            LEFT JOIN dbo.transaction_collectors tc ON tc.staff_id = sa.staff_id
            LEFT JOIN dbo.journal_entries je ON je.transaction_id = tc.transaction_id AND je.entry_type = 'CONTRIBUTED'{event_join_filter}
            WHERE sa.role = 'counter'
            GROUP BY sa.staff_id, sa.display_name
            ORDER BY sa.staff_id
        """
        params = [event_id] if event_id is not None else []
        cursor.execute(query, *params)
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def get_staff_denomination_summary(event_id: int | None = None) -> list[dict]:
    """Per-counter note breakdown (denomination -> note count), for the admin's cash reconciliation view.
    Only reflects contributions recorded after the denomination + collector tagging features shipped."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        event_filter = "WHERE je.event_id = ?" if event_id is not None else ""
        query = f"""
            WITH staff_tx AS (
                SELECT tc.staff_id, tc.transaction_id
                FROM dbo.transaction_collectors tc
                JOIN dbo.journal_entries je ON je.transaction_id = tc.transaction_id AND je.entry_type = 'CONTRIBUTED'
                {event_filter}
            )
            SELECT sa.staff_id, sa.display_name, td.denomination, SUM(td.note_count) AS note_count
            FROM dbo.staff_accounts sa
            LEFT JOIN staff_tx st ON st.staff_id = sa.staff_id
            LEFT JOIN dbo.transaction_denominations td ON td.transaction_id = st.transaction_id
            WHERE sa.role = 'counter'
            GROUP BY sa.staff_id, sa.display_name, td.denomination
            ORDER BY sa.staff_id, td.denomination DESC
        """
        params = [event_id] if event_id is not None else []
        cursor.execute(query, *params)
        summary: dict[int, dict] = {}
        for staff_id, display_name, denomination, note_count in cursor.fetchall():
            entry = summary.setdefault(staff_id, {"staff_id": staff_id, "display_name": display_name, "denominations": {}})
            if denomination is not None and note_count:
                entry["denominations"][denomination] = int(note_count)
        return list(summary.values())
    finally:
        connection.close()


USER_COLUMNS = """
    id, husband_name, wife_name, husband_job, phone_number,
    family_deity, email, created_at, updated_at, is_active, password_hash,
    search_alias, native_place, current_place, wife_job, others, initial, notes,
    serial_number, is_thaimama
"""


def create_user(
    husband_name: str,
    wife_name: str | None,
    husband_job: str | None,
    phone_number: str,
    native_place: str | None,
    current_place: str | None,
    wife_job: str | None,
    others: str | None,
    initial: str | None,
    notes: str | None,
    is_thaimama: bool = False,
    serial_number: int | None = None,
) -> dict:
    connection = get_connection()
    cursor = connection.cursor()
    try:
        if serial_number is not None:
            # Legacy family-record number, distinct from the serial assigned to an event contribution.
            cursor.execute(
                """
                     INSERT INTO dbo.Users (
                      husband_name, wife_name, husband_job, phone_number,
                      native_place, current_place, wife_job, others, initial, notes,
                      is_thaimama, serial_number
                     )
                     OUTPUT INSERTED.id, INSERTED.husband_name, INSERTED.wife_name,
                         INSERTED.husband_job, INSERTED.phone_number,
                         INSERTED.family_deity, INSERTED.email, INSERTED.created_at,
                         INSERTED.updated_at, INSERTED.is_active, INSERTED.password_hash,
                         INSERTED.search_alias, INSERTED.native_place,
                         INSERTED.current_place, INSERTED.wife_job, INSERTED.others,
                         INSERTED.initial, INSERTED.notes, INSERTED.serial_number,
                         INSERTED.is_thaimama
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                husband_name,
                wife_name,
                husband_job,
                phone_number,
                native_place,
                current_place,
                wife_job,
                others,
                initial,
                notes,
                is_thaimama,
                serial_number,
            )
        else:
            cursor.execute(
                """
                     INSERT INTO dbo.Users (
                      husband_name, wife_name, husband_job, phone_number,
                      native_place, current_place, wife_job, others, initial, notes,
                      is_thaimama
                     )
                     OUTPUT INSERTED.id, INSERTED.husband_name, INSERTED.wife_name,
                         INSERTED.husband_job, INSERTED.phone_number,
                         INSERTED.family_deity, INSERTED.email, INSERTED.created_at,
                         INSERTED.updated_at, INSERTED.is_active, INSERTED.password_hash,
                         INSERTED.search_alias, INSERTED.native_place,
                         INSERTED.current_place, INSERTED.wife_job, INSERTED.others,
                         INSERTED.initial, INSERTED.notes, INSERTED.serial_number,
                         INSERTED.is_thaimama
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                     husband_name,
                     wife_name,
                     husband_job,
                phone_number,
                     native_place,
                     current_place,
                     wife_job,
                     others,
                     initial,
                     notes,
                     is_thaimama,
            )
        row = cursor.fetchone()
        connection.commit()
        return dict(zip([column[0] for column in cursor.description], row))
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_next_serial_number(event_id: int | None = None) -> int:
    """Preview the next serial number. Scoped to an event (distinct contributors to it + 1) when event_id is given,
    otherwise the global next value (the DB sequence assigns the real one when no explicit value is inserted)."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        if event_id is not None:
            cursor.execute(
                """
                SELECT ISNULL(COUNT(DISTINCT user_id), 0) + 1
                FROM dbo.journal_entries
                WHERE event_id = ? AND entry_type = 'CONTRIBUTED'
                """,
                event_id,
            )
        else:
            cursor.execute("SELECT ISNULL(MAX(serial_number), 0) + 1 FROM dbo.Users")
        return cursor.fetchone()[0]
    finally:
        connection.close()


def get_event_serial_status(event_id: int, contributor_id: int | None = None) -> dict:
    """The next number is a preview; a family's saved event serial is stable."""
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT COUNT(*), MAX(CASE WHEN user_id = ? THEN serial_number END)
            FROM dbo.event_contributor_serials WHERE event_id = ?
            """, contributor_id, event_id,
        )
        count, serial_number = cursor.fetchone()
        return {"collected_family_count": count, "next_serial_number": count + 1, "serial_number": serial_number}
    finally:
        connection.close()


def get_users(search: str | None = None) -> list[dict]:
    connection = get_connection()
    cursor = connection.cursor()
    try:
        if search:
            pattern = f"%{search}%"
            cursor.execute(
                """
                SELECT {USER_COLUMNS}
                FROM dbo.Users
                WHERE husband_name LIKE ? OR wife_name LIKE ?
                   OR husband_job LIKE ? OR phone_number LIKE ?
                   OR family_deity LIKE ? OR email LIKE ?
                   OR search_alias LIKE ? OR native_place LIKE ?
                   OR current_place LIKE ? OR wife_job LIKE ? OR others LIKE ?
                   OR initial LIKE ? OR notes LIKE ?
                ORDER BY id
                """.format(USER_COLUMNS=USER_COLUMNS),
                *(pattern for _ in range(13)),
            )
        else:
            cursor.execute(
                """SELECT {USER_COLUMNS}
                FROM dbo.Users
                ORDER BY id""".format(USER_COLUMNS=USER_COLUMNS)
            )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def update_user(
    user_id: int,
    husband_name: str,
    wife_name: str | None,
    husband_job: str | None,
    phone_number: str,
    native_place: str | None,
    current_place: str | None,
    wife_job: str | None,
    others: str | None,
    initial: str | None,
    notes: str | None,
    is_thaimama: bool = False,
    changed_by_staff_id: int | None = None,
) -> dict | None:
    connection = get_connection()
    cursor = connection.cursor()
    try:
        before_snapshot = _get_user_change_snapshot(cursor, user_id)
        if before_snapshot is None:
            connection.rollback()
            return None

        cursor.execute(
            """
            UPDATE dbo.Users
            SET husband_name = ?, wife_name = ?, husband_job = ?, phone_number = ?,
                native_place = ?, current_place = ?, wife_job = ?,
                others = ?, initial = ?, notes = ?, is_thaimama = ?, updated_at = SYSDATETIME()
            WHERE id = ?
            """,
            husband_name,
            wife_name,
            husband_job,
            phone_number,
            native_place,
            current_place,
            wife_job,
            others,
            initial,
            notes,
            is_thaimama,
            user_id,
        )
        if cursor.rowcount == 0:
            connection.rollback()
            return None

        after_snapshot = _get_user_change_snapshot(cursor, user_id)
        if after_snapshot is None:
            raise RuntimeError("Updated user could not be read back.")

        editable_fields = (
            "husband_name", "wife_name", "husband_job", "phone_number", "native_place",
            "current_place", "wife_job", "others", "initial", "notes", "is_thaimama",
        )
        if changed_by_staff_id is not None and any(before_snapshot[field] != after_snapshot[field] for field in editable_fields):
            cursor.execute(
                """
                INSERT INTO dbo.user_change_history (user_id, changed_by_staff_id, before_json, after_json)
                VALUES (?, ?, ?, ?)
                """,
                user_id,
                changed_by_staff_id,
                json.dumps(before_snapshot, ensure_ascii=False, default=str),
                json.dumps(after_snapshot, ensure_ascii=False, default=str),
            )
        connection.commit()
        return after_snapshot
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _get_user_change_snapshot(cursor, user_id: int) -> dict | None:
    cursor.execute(
        "SELECT {USER_COLUMNS} FROM dbo.Users WHERE id = ?".format(USER_COLUMNS=USER_COLUMNS),
        user_id,
    )
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [column[0] for column in cursor.description]
    return {column: value for column, value in zip(columns, row) if column != "password_hash"}


def get_user_change_history(search: str | None = None) -> list[dict]:
    """Return recent user profile changes for the Admin history view."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        query = """
            SELECT TOP 200 h.history_id, h.user_id, u.husband_name, u.phone_number,
                   s.display_name AS changed_by, h.changed_at, h.before_json, h.after_json
            FROM dbo.user_change_history h
            JOIN dbo.Users u ON u.id = h.user_id
            JOIN dbo.staff_accounts s ON s.staff_id = h.changed_by_staff_id
        """
        params: list[str] = []
        if search:
            query += """
            WHERE u.husband_name LIKE ? OR u.wife_name LIKE ? OR u.phone_number LIKE ?
               OR h.before_json LIKE ? OR h.after_json LIKE ?
            """
            pattern = f"%{search}%"
            params.extend([pattern] * 5)
        query += " ORDER BY h.changed_at DESC, h.history_id DESC"
        cursor.execute(query, *params)
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


EVENT_COLUMNS = "event_id, event_name, event_date, event_place, event_location, host_user_id, is_active"


def get_active_events() -> list[dict]:
    """List active events for the admin event-selection dropdown, newest first."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"""
            SELECT {EVENT_COLUMNS}
            FROM dbo.event
            WHERE is_active = 1
            ORDER BY event_date DESC, event_id DESC
            """
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def get_assigned_active_events(staff_id: int) -> list[dict]:
    """List active events assigned to one counter."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"""
            SELECT {', '.join(f'e.{column.strip()}' for column in EVENT_COLUMNS.split(','))}
            FROM dbo.event e
            JOIN dbo.event_counter_assignments a ON a.event_id = e.event_id
            WHERE e.is_active = 1 AND a.staff_id = ?
            ORDER BY e.event_date DESC, e.event_id DESC
            """,
            staff_id,
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def get_event_counter_assignments(event_id: int) -> dict:
    """Return active counter accounts and their assignment state for one event."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT staff_id, display_name FROM dbo.staff_accounts WHERE role = 'counter' AND is_active = 1 ORDER BY display_name"
        )
        counters = [{"staff_id": row[0], "display_name": row[1]} for row in cursor.fetchall()]
        cursor.execute(
            "SELECT staff_id FROM dbo.event_counter_assignments WHERE event_id = ?",
            event_id,
        )
        assigned_counter_ids = [row[0] for row in cursor.fetchall()]
        return {"counters": counters, "assigned_counter_ids": assigned_counter_ids}
    finally:
        connection.close()


def replace_event_counter_assignments(event_id: int, counter_ids: list[int], assigned_by: int) -> None:
    """Replace the counter assignment set for one event atomically."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT 1 FROM dbo.event WHERE event_id = ? AND is_active = 1", event_id)
        if not cursor.fetchone():
            raise ValueError("Active event not found.")

        if counter_ids:
            placeholders = ",".join("?" for _ in counter_ids)
            cursor.execute(
                f"SELECT COUNT(*) FROM dbo.staff_accounts WHERE role = 'counter' AND is_active = 1 AND staff_id IN ({placeholders})",
                *counter_ids,
            )
            if cursor.fetchone()[0] != len(counter_ids):
                raise ValueError("One or more selected counters are inactive or invalid.")

        cursor.execute("DELETE FROM dbo.event_counter_assignments WHERE event_id = ?", event_id)
        for counter_id in counter_ids:
            cursor.execute(
                "INSERT INTO dbo.event_counter_assignments (event_id, staff_id, assigned_by) VALUES (?, ?, ?)",
                event_id,
                counter_id,
                assigned_by,
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def is_event_assigned_to_staff(event_id: int, staff_id: int) -> bool:
    """Check that a counter has been assigned to an active event."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT 1
            FROM dbo.event_counter_assignments a
            JOIN dbo.event e ON e.event_id = a.event_id
            WHERE a.event_id = ? AND a.staff_id = ? AND e.is_active = 1
            """,
            event_id,
            staff_id,
        )
        return cursor.fetchone() is not None
    finally:
        connection.close()


def create_event(
    event_name: str,
    event_date: str,
    event_place: str | None,
    event_location: str | None,
    host_user_id: int | None = None,
) -> dict:
    """Create a new event. host_user_id is optional; leaving it NULL lets the admin pick a receiver on the first contribution."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"""
            INSERT INTO dbo.event (event_name, event_date, event_place, event_location, host_user_id)
            OUTPUT INSERTED.{EVENT_COLUMNS.replace(", ", ", INSERTED.")}
            VALUES (?, ?, ?, ?, ?)
            """,
            event_name,
            event_date,
            event_place,
            event_location,
            host_user_id,
        )
        row = cursor.fetchone()
        connection.commit()
        return dict(zip([column[0] for column in cursor.description], row))
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_event_contributors(event_id: int) -> list[dict]:
    """Families who have already contributed to this event, most recent first (admin sidebar default view)."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT u.id, u.husband_name, u.phone_number, je.amount, je.transaction_id
            FROM dbo.journal_entries je
            JOIN dbo.Users u ON u.id = je.user_id
            WHERE je.event_id = ? AND je.entry_type = 'CONTRIBUTED'
            ORDER BY je.transaction_id DESC
            """,
            event_id,
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def get_event_transactions(event_id: int) -> list[dict]:
    """Return each contribution for an event with the contributor's requested report details."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
                 SELECT contributor.husband_name AS husband_name,
                     contributor.wife_name AS wife_name,
                     contributor.native_place,
                     contributor.phone_number,
                     je.amount
            FROM dbo.journal_entries je
            JOIN dbo.Users contributor ON contributor.id = je.user_id
            WHERE je.event_id = ? AND je.entry_type = 'CONTRIBUTED'
                 ORDER BY je.transaction_id
            """,
            event_id,
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def get_transaction_denomination_details(event_id: int, transaction_id: int) -> dict | None:
    """Return saved note counts and collector ownership for one contribution transaction."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT je.event_id, tc.staff_id, td.denomination, td.note_count
            FROM dbo.journal_entries je
            LEFT JOIN dbo.transaction_collectors tc ON tc.transaction_id = je.transaction_id
            LEFT JOIN dbo.transaction_denominations td ON td.transaction_id = je.transaction_id
            WHERE je.event_id = ? AND je.transaction_id = ? AND je.entry_type = 'CONTRIBUTED'
            ORDER BY td.denomination DESC
            """,
            event_id,
            transaction_id,
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        return {
            "event_id": rows[0][0],
            "staff_id": rows[0][1],
            "denominations": {row[2]: row[3] for row in rows if row[2] is not None},
        }
    finally:
        connection.close()


def process_contribution(
    contributor_id: int,
    receiver_id: int,
    event_id: int,
    amount: float,
    denominations: dict[int, int] | None = None,
    staff_id: int | None = None,
) -> tuple[bool, str, int | None, int | None]:
    """Record one double-entry contribution via the existing, already-deployed sp_ProcessContribution.
    Optionally also saves the counted cash denomination breakdown (note value -> count), and which
    logged-in staff member recorded it. The event lock also protects the event/family serial allocation."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        # Serialize saves for the event until contribution, family serial and cash tags all commit.
        cursor.execute(
            "SELECT event_id FROM dbo.event WITH (UPDLOCK, HOLDLOCK) WHERE event_id = ? AND is_active = 1",
            event_id,
        )
        if not cursor.fetchone():
            raise ValueError("Event ID does not exist or is inactive.")
        cursor.execute(
            "EXEC dbo.sp_ProcessContribution @ContributorId = ?, @ReceiverId = ?, @EventId = ?, @Amount = ?",
            contributor_id,
            receiver_id,
            event_id,
            amount,
        )
        cursor.execute(
            """
            SELECT TOP 1 transaction_id FROM dbo.journal_entries
            WHERE user_id = ? AND event_id = ? AND entry_type = 'CONTRIBUTED'
            ORDER BY transaction_id DESC
            """, contributor_id, event_id,
        )
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("Contribution did not produce a transaction.")
        transaction_id = row[0]
        cursor.execute(
            "SELECT serial_number FROM dbo.event_contributor_serials WHERE event_id = ? AND user_id = ?",
            event_id, contributor_id,
        )
        existing = cursor.fetchone()
        if existing:
            serial_number = existing[0]
        else:
            cursor.execute(
                "SELECT ISNULL(MAX(serial_number), 0) + 1 FROM dbo.event_contributor_serials WHERE event_id = ?",
                event_id,
            )
            serial_number = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO dbo.event_contributor_serials (event_id, user_id, serial_number) VALUES (?, ?, ?)",
                event_id, contributor_id, serial_number,
            )

        if transaction_id and denominations:
            for denomination, note_count in denominations.items():
                if note_count and note_count > 0:
                    cursor.execute(
                        "INSERT INTO dbo.transaction_denominations (transaction_id, denomination, note_count) VALUES (?, ?, ?)",
                        transaction_id,
                        denomination,
                        note_count,
                    )

        if transaction_id and staff_id:
            cursor.execute(
                "INSERT INTO dbo.transaction_collectors (transaction_id, staff_id) VALUES (?, ?)",
                transaction_id,
                staff_id,
            )

        connection.commit()
        return True, "Contribution recorded successfully.", serial_number, transaction_id
    except Exception as error:
        connection.rollback()
        return False, str(error), None, None
    finally:
        connection.close()


def get_event_denomination_summary(event_id: int) -> dict:
    """Live total amount + note-by-note breakdown collected so far for this event (only reflects contributions
    recorded after the transaction_denominations feature shipped)."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT ISNULL(SUM(amount), 0) FROM dbo.journal_entries WHERE event_id = ? AND entry_type = 'CONTRIBUTED'",
            event_id,
        )
        total_amount = float(cursor.fetchone()[0])

        cursor.execute(
            """
            SELECT td.denomination, SUM(td.note_count) AS total_notes
            FROM dbo.transaction_denominations td
            JOIN dbo.journal_entries je ON je.transaction_id = td.transaction_id AND je.entry_type = 'CONTRIBUTED'
            WHERE je.event_id = ?
            GROUP BY td.denomination
            ORDER BY td.denomination DESC
            """,
            event_id,
        )
        denominations = {row[0]: row[1] for row in cursor.fetchall()}
        return {"total_amount": total_amount, "denominations": denominations}
    finally:
        connection.close()


def get_family_for_login(phone_number: str) -> dict | None:
    """Look up an active family by phone for the login screen."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT id, husband_name, wife_name, phone_number, password_hash
            FROM dbo.Users
            WHERE phone_number = ? AND is_active = 1
            """,
            phone_number,
        )
        row = cursor.fetchone()
        if not row:
            return None
        columns = [column[0] for column in cursor.description]
        return dict(zip(columns, row))
    finally:
        connection.close()


def get_family_profile(user_id: int) -> dict | None:
    """Full editable profile for the logged-in family's Profile screen (no password_hash)."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT id, husband_name, wife_name, husband_job, wife_job, phone_number,
                   native_place, current_place, others, initial, notes, is_thaimama,
                   serial_number, family_deity, email, search_alias, is_active
            FROM dbo.Users
            WHERE id = ? AND is_active = 1
            """,
            user_id,
        )
        row = cursor.fetchone()
        if not row:
            return None
        columns = [column[0] for column in cursor.description]
        return dict(zip(columns, row))
    finally:
        connection.close()


def set_family_password_if_unset(user_id: int, new_password: str) -> bool:
    """First-time password setup; refuses if a password already exists (use change_family_password instead)."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT password_hash FROM dbo.Users WHERE id = ? AND is_active = 1", user_id)
        row = cursor.fetchone()
        if not row or row[0]:
            return False
        cursor.execute(
            "UPDATE dbo.Users SET password_hash = ?, updated_at = SYSDATETIME() WHERE id = ?",
            hash_password(new_password),
            user_id,
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def change_family_password(user_id: int, current_password: str, new_password: str) -> tuple[bool, str]:
    """Change an existing password after verifying the current one."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT password_hash FROM dbo.Users WHERE id = ? AND is_active = 1", user_id)
        row = cursor.fetchone()
        if not row or not row[0]:
            return False, "கடவுச்சொல் இன்னும் அமைக்கப்படவில்லை."
        if not verify_password(current_password, row[0]):
            return False, "தற்போதைய கடவுச்சொல் தவறானது."
        cursor.execute(
            "UPDATE dbo.Users SET password_hash = ?, updated_at = SYSDATETIME() WHERE id = ?",
            hash_password(new_password),
            user_id,
        )
        connection.commit()
        return True, "கடவுச்சொல் புதுப்பிக்கப்பட்டது."
    except Exception as error:
        connection.rollback()
        return False, str(error)
    finally:
        connection.close()


def get_my_contributions(family_id: int) -> list[dict]:
    """Everything this family has given, with receiver + event details (double-entry read)."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT
                t.transaction_id, t.transaction_date, je.event_id, e.event_name,
                receiver.id AS receiver_id, receiver.husband_name AS receiver_husband_name,
                receiver.phone_number AS receiver_phone_number, je.amount
            FROM dbo.journal_entries je
            JOIN dbo.journal_entries received
                ON received.transaction_id = je.transaction_id AND received.entry_type = 'RECEIVED'
            JOIN dbo.transactions t ON t.transaction_id = je.transaction_id
            JOIN dbo.event e ON e.event_id = je.event_id
            JOIN dbo.Users receiver ON receiver.id = received.user_id
            WHERE je.user_id = ? AND je.entry_type = 'CONTRIBUTED'
            ORDER BY t.transaction_date DESC, t.transaction_id DESC
            """,
            family_id,
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def get_my_receipts(family_id: int) -> list[dict]:
    """Everything this family has received, with contributor + event details (double-entry read)."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT
                t.transaction_id, t.transaction_date, je.event_id, e.event_name,
                contributor.id AS contributor_id, contributor.husband_name AS contributor_husband_name,
                contributor.phone_number AS contributor_phone_number, je.amount
            FROM dbo.journal_entries je
            JOIN dbo.journal_entries contributed
                ON contributed.transaction_id = je.transaction_id AND contributed.entry_type = 'CONTRIBUTED'
            JOIN dbo.transactions t ON t.transaction_id = je.transaction_id
            JOIN dbo.event e ON e.event_id = je.event_id
            JOIN dbo.Users contributor ON contributor.id = contributed.user_id
            WHERE je.user_id = ? AND je.entry_type = 'RECEIVED'
            ORDER BY t.transaction_date DESC, t.transaction_id DESC
            """,
            family_id,
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def get_partner_history(family_id: int) -> list[dict]:
    """One summary row per family ever exchanged with: total given, total received, net difference."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            WITH tx_pairs AS (
                SELECT c.transaction_id, c.user_id AS contributor_id, r.user_id AS receiver_id, c.amount
                FROM dbo.journal_entries c
                JOIN dbo.journal_entries r
                    ON r.transaction_id = c.transaction_id AND r.entry_type = 'RECEIVED'
                WHERE c.entry_type = 'CONTRIBUTED' AND (c.user_id = ? OR r.user_id = ?)
            )
            SELECT
                other.id AS other_user_id, other.husband_name, other.wife_name, other.phone_number,
                other.native_place, other.current_place,
                SUM(CASE WHEN tp.contributor_id = ? THEN tp.amount ELSE 0 END) AS total_given,
                SUM(CASE WHEN tp.receiver_id = ? THEN tp.amount ELSE 0 END) AS total_received,
                SUM(CASE WHEN tp.contributor_id = ? THEN tp.amount ELSE 0 END)
                    - SUM(CASE WHEN tp.receiver_id = ? THEN tp.amount ELSE 0 END) AS net_difference
            FROM tx_pairs tp
            JOIN dbo.Users other ON other.id = CASE WHEN tp.contributor_id = ? THEN tp.receiver_id ELSE tp.contributor_id END
            GROUP BY other.id, other.husband_name, other.wife_name, other.phone_number, other.native_place, other.current_place
            ORDER BY other.husband_name
            """,
            family_id, family_id, family_id, family_id, family_id, family_id, family_id,
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def get_partner_transactions(family_id: int, other_family_id: int) -> list[dict]:
    """Full chronological ledger between two specific families, with a running net balance."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            WITH tx_pairs AS (
                SELECT c.transaction_id, c.user_id AS contributor_id, r.user_id AS receiver_id,
                       c.amount, t.transaction_date, e.event_name
                FROM dbo.journal_entries c
                JOIN dbo.journal_entries r
                    ON r.transaction_id = c.transaction_id AND r.entry_type = 'RECEIVED'
                JOIN dbo.transactions t ON t.transaction_id = c.transaction_id
                JOIN dbo.event e ON e.event_id = c.event_id
                WHERE c.entry_type = 'CONTRIBUTED'
                  AND ((c.user_id = ? AND r.user_id = ?) OR (c.user_id = ? AND r.user_id = ?))
            )
            SELECT
                transaction_id, transaction_date, event_name,
                CASE WHEN contributor_id = ? THEN 'GIVEN' ELSE 'RECEIVED' END AS direction,
                amount,
                SUM(CASE WHEN contributor_id = ? THEN amount ELSE -amount END)
                    OVER (ORDER BY transaction_date, transaction_id ROWS UNBOUNDED PRECEDING) AS running_net_difference
            FROM tx_pairs
            ORDER BY transaction_date ASC, transaction_id ASC
            """,
            family_id, other_family_id, other_family_id, family_id, family_id, family_id,
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()
