import hashlib
import json
import os
from decimal import Decimal

from dotenv import load_dotenv
from psycopg import Error as PsycopgError
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

load_dotenv()

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Lazily-created pool. prepare_threshold=None keeps it compatible with Supabase's transaction pooler."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            open=True,
        )
    return _pool


def _all(sql: str, params=None) -> list[dict]:
    with get_pool().connection() as connection:
        return connection.execute(sql, params).fetchall()


def _one(sql: str, params=None) -> dict | None:
    with get_pool().connection() as connection:
        return connection.execute(sql, params).fetchone()


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


def _error_message(error: Exception) -> str:
    diag = getattr(error, "diag", None)
    return getattr(diag, "message_primary", None) or str(error)


# ----------------------------------------------------------------------------
# Staff accounts
# ----------------------------------------------------------------------------

def create_staff_account(display_name: str, pin: str, role: str = "counter") -> dict:
    """Create a new Admin Panel login (PIN-based). role is 'counter' or 'admin'."""
    return _one(
        """
        INSERT INTO staff_accounts (display_name, pin_hash, role)
        VALUES (%s, %s, %s)
        RETURNING staff_id, display_name, role, is_active, created_at
        """,
        (display_name, hash_password(pin), role),
    )


def get_staff_by_pin(pin: str) -> dict | None:
    """Find the active staff account whose PIN matches (iterates active accounts; staff count is expected to stay small)."""
    for row in _all("SELECT staff_id, display_name, pin_hash, role FROM staff_accounts WHERE is_active"):
        if verify_password(pin, row["pin_hash"]):
            return {"staff_id": row["staff_id"], "display_name": row["display_name"], "role": row["role"]}
    return None


def list_staff_accounts() -> list[dict]:
    """All staff accounts for the admin's Manage Counters panel (never exposes pin_hash)."""
    return _all("SELECT staff_id, display_name, role, is_active, created_at FROM staff_accounts ORDER BY staff_id")


def get_staff_collections(staff_id: int, event_id: int | None = None) -> list[dict]:
    """Contributions personally recorded by this staff login (optionally scoped to one event)."""
    query = """
        SELECT je.transaction_id, je.amount, je.event_id, u.id AS user_id,
               u.husband_name AS contributor_name,
               u.phone_number AS contributor_phone, tc.collected_at
        FROM transaction_collectors tc
        JOIN journal_entries je ON je.transaction_id = tc.transaction_id AND je.entry_type = 'CONTRIBUTED'
        JOIN users u ON u.id = je.user_id
        WHERE tc.staff_id = %s
    """
    params: list = [staff_id]
    if event_id is not None:
        query += " AND je.event_id = %s"
        params.append(event_id)
    query += " ORDER BY tc.collected_at DESC"
    return _all(query, params)


def get_staff_collections_summary(event_id: int | None = None) -> list[dict]:
    """Totals grouped by counter login, for the admin's cross-counter reconciliation view."""
    event_join_filter = " AND je.event_id = %s" if event_id is not None else ""
    query = f"""
        SELECT sa.staff_id, sa.display_name,
               COUNT(je.transaction_id) AS contribution_count,
               COALESCE(SUM(je.amount), 0) AS total_amount
        FROM staff_accounts sa
        LEFT JOIN transaction_collectors tc ON tc.staff_id = sa.staff_id
        LEFT JOIN journal_entries je ON je.transaction_id = tc.transaction_id AND je.entry_type = 'CONTRIBUTED'{event_join_filter}
        WHERE sa.role = 'counter'
        GROUP BY sa.staff_id, sa.display_name
        ORDER BY sa.staff_id
    """
    return _all(query, [event_id] if event_id is not None else None)


def get_staff_denomination_summary(event_id: int | None = None) -> list[dict]:
    """Per-counter note breakdown (denomination -> note count), for the admin's cash reconciliation view."""
    event_filter = "WHERE je.event_id = %s" if event_id is not None else ""
    query = f"""
        WITH staff_tx AS (
            SELECT tc.staff_id, tc.transaction_id
            FROM transaction_collectors tc
            JOIN journal_entries je ON je.transaction_id = tc.transaction_id AND je.entry_type = 'CONTRIBUTED'
            {event_filter}
        )
        SELECT sa.staff_id, sa.display_name, td.denomination, SUM(td.note_count) AS note_count
        FROM staff_accounts sa
        LEFT JOIN staff_tx st ON st.staff_id = sa.staff_id
        LEFT JOIN transaction_denominations td ON td.transaction_id = st.transaction_id
        WHERE sa.role = 'counter'
        GROUP BY sa.staff_id, sa.display_name, td.denomination
        ORDER BY sa.staff_id, td.denomination DESC NULLS LAST
    """
    summary: dict[int, dict] = {}
    for row in _all(query, [event_id] if event_id is not None else None):
        entry = summary.setdefault(
            row["staff_id"], {"staff_id": row["staff_id"], "display_name": row["display_name"], "denominations": {}}
        )
        if row["denomination"] is not None and row["note_count"]:
            entry["denominations"][row["denomination"]] = int(row["note_count"])
    return list(summary.values())


# ----------------------------------------------------------------------------
# Families (users)
# ----------------------------------------------------------------------------

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
    # An explicit serial_number overrides the sequence default for this insert only.
    return _one(
        f"""
        INSERT INTO users (
            husband_name, wife_name, husband_job, phone_number,
            native_place, current_place, wife_job, others, initial, notes,
            is_thaimama, serial_number
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                COALESCE(%s::integer, nextval('users_serial_number_seq')))
        RETURNING {USER_COLUMNS}
        """,
        (husband_name, wife_name, husband_job, phone_number, native_place, current_place,
         wife_job, others, initial, notes, is_thaimama, serial_number),
    )


def get_next_serial_number(event_id: int | None = None) -> int:
    """Preview the next serial number. Scoped to an event (distinct contributors to it + 1) when event_id is given,
    otherwise the global next value (the DB sequence assigns the real one when no explicit value is inserted)."""
    if event_id is not None:
        row = _one(
            """
            SELECT COUNT(DISTINCT user_id) + 1 AS next_number
            FROM journal_entries
            WHERE event_id = %s AND entry_type = 'CONTRIBUTED'
            """,
            (event_id,),
        )
    else:
        row = _one("SELECT COALESCE(MAX(serial_number), 0) + 1 AS next_number FROM users")
    return int(row["next_number"])


def get_event_serial_status(event_id: int, contributor_id: int | None = None) -> dict:
    """The next number is a preview; a family's saved event serial is stable."""
    row = _one(
        """
        SELECT COUNT(*) AS collected_family_count,
               MAX(CASE WHEN user_id = %s THEN serial_number END) AS serial_number
        FROM event_contributor_serials WHERE event_id = %s
        """, (contributor_id, event_id),
    )
    count = int(row["collected_family_count"])
    return {"collected_family_count": count, "next_serial_number": count + 1, "serial_number": row["serial_number"]}


def get_users(search: str | None = None) -> list[dict]:
    if search:
        pattern = f"%{search}%"
        return _all(
            f"""
            SELECT {USER_COLUMNS}
            FROM users
            WHERE husband_name ILIKE %s OR wife_name ILIKE %s
               OR husband_job ILIKE %s OR phone_number ILIKE %s
               OR family_deity ILIKE %s OR email ILIKE %s
               OR search_alias ILIKE %s OR native_place ILIKE %s
               OR current_place ILIKE %s OR wife_job ILIKE %s OR others ILIKE %s
               OR initial ILIKE %s OR notes ILIKE %s
            ORDER BY id
            """,
            [pattern] * 13,
        )
    return _all(f"SELECT {USER_COLUMNS} FROM users ORDER BY id")


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
    with get_pool().connection() as connection:
        before = connection.execute(
            f"SELECT {USER_COLUMNS} FROM users WHERE id = %s FOR UPDATE", (user_id,)
        ).fetchone()
        if before is None:
            return None
        after = connection.execute(
            f"""
            UPDATE users
            SET husband_name = %s, wife_name = %s, husband_job = %s, phone_number = %s,
                native_place = %s, current_place = %s, wife_job = %s,
                others = %s, initial = %s, notes = %s, is_thaimama = %s, updated_at = now()
            WHERE id = %s
            RETURNING {USER_COLUMNS}
            """,
            (husband_name, wife_name, husband_job, phone_number, native_place, current_place,
             wife_job, others, initial, notes, is_thaimama, user_id),
        ).fetchone()
        if after is None:
            raise RuntimeError("Updated user could not be read back.")
        before = {key: value for key, value in before.items() if key != "password_hash"}
        after = {key: value for key, value in after.items() if key != "password_hash"}
        editable_fields = (
            "husband_name", "wife_name", "husband_job", "phone_number", "native_place",
            "current_place", "wife_job", "others", "initial", "notes", "is_thaimama",
        )
        if changed_by_staff_id is not None and any(before[field] != after[field] for field in editable_fields):
            connection.execute(
                """
                INSERT INTO user_change_history(user_id, changed_by_staff_id, before_json, after_json)
                VALUES (%s, %s, %s, %s)
                """,
                (user_id, changed_by_staff_id,
                 json.dumps(before, ensure_ascii=False, default=str),
                 json.dumps(after, ensure_ascii=False, default=str)),
            )
        return after


def get_user_change_history(search: str | None = None) -> list[dict]:
    """Return recent profile changes in the same shape as the SQL Server backend."""
    query = """
        SELECT h.history_id, h.user_id, u.husband_name, u.phone_number,
               s.display_name AS changed_by, h.changed_at, h.before_json, h.after_json
        FROM user_change_history h
        JOIN users u ON u.id = h.user_id
        JOIN staff_accounts s ON s.staff_id = h.changed_by_staff_id
    """
    params = None
    if search:
        query += """
            WHERE u.husband_name ILIKE %s OR u.wife_name ILIKE %s OR u.phone_number ILIKE %s
               OR h.before_json ILIKE %s OR h.after_json ILIKE %s
        """
        params = [f"%{search}%"] * 5
    return _all(query + " ORDER BY h.changed_at DESC, h.history_id DESC LIMIT 200", params)


# ----------------------------------------------------------------------------
# Events and contributions
# ----------------------------------------------------------------------------

EVENT_COLUMNS = "event_id, event_name, event_date, event_place, event_location, host_user_id, is_active"


def get_active_events() -> list[dict]:
    """List active events for the admin event-selection dropdown, newest first."""
    return _all(
        f"""
        SELECT {EVENT_COLUMNS}
        FROM event
        WHERE is_active
        ORDER BY event_date DESC, event_id DESC
        """
    )


def get_assigned_active_events(staff_id: int) -> list[dict]:
    """List only active events assigned to this counter."""
    columns = ", ".join(f"e.{column.strip()}" for column in EVENT_COLUMNS.split(","))
    return _all(
        f"""
        SELECT {columns}
        FROM event e
        JOIN event_counter_assignments a ON a.event_id = e.event_id
        WHERE e.is_active AND a.staff_id = %s
        ORDER BY e.event_date DESC, e.event_id DESC
        """,
        (staff_id,),
    )


def get_event_counter_assignments(event_id: int) -> dict:
    with get_pool().connection() as connection:
        counters = connection.execute(
            "SELECT staff_id, display_name FROM staff_accounts WHERE role = 'counter' AND is_active ORDER BY display_name"
        ).fetchall()
        assigned = connection.execute(
            "SELECT staff_id FROM event_counter_assignments WHERE event_id = %s", (event_id,)
        ).fetchall()
    return {"counters": counters, "assigned_counter_ids": [row["staff_id"] for row in assigned]}


def replace_event_counter_assignments(event_id: int, counter_ids: list[int], assigned_by: int) -> None:
    """Validate and replace assignments atomically; serialize edits to the same event."""
    with get_pool().connection() as connection:
        event = connection.execute(
            "SELECT event_id FROM event WHERE event_id = %s AND is_active FOR UPDATE", (event_id,)
        ).fetchone()
        if event is None:
            raise ValueError("Active event not found.")
        if counter_ids:
            valid = connection.execute(
                "SELECT COUNT(*) AS counter_count FROM staff_accounts WHERE role = 'counter' AND is_active AND staff_id = ANY(%s)",
                (counter_ids,),
            ).fetchone()
            if valid["counter_count"] != len(counter_ids):
                raise ValueError("One or more selected counters are inactive or invalid.")
        connection.execute("DELETE FROM event_counter_assignments WHERE event_id = %s", (event_id,))
        for counter_id in counter_ids:
            connection.execute(
                "INSERT INTO event_counter_assignments(event_id, staff_id, assigned_by) VALUES (%s, %s, %s)",
                (event_id, counter_id, assigned_by),
            )


def is_event_assigned_to_staff(event_id: int, staff_id: int) -> bool:
    return _one(
        """
        SELECT 1 FROM event_counter_assignments a
        JOIN event e ON e.event_id = a.event_id
        WHERE a.event_id = %s AND a.staff_id = %s AND e.is_active
        """,
        (event_id, staff_id),
    ) is not None


def create_event(
    event_name: str,
    event_date: str,
    event_place: str | None,
    event_location: str | None,
    host_user_id: int | None = None,
) -> dict:
    """Create a new event. host_user_id is optional; leaving it NULL lets the admin pick a receiver on the first contribution."""
    return _one(
        f"""
        INSERT INTO event (event_name, event_date, event_place, event_location, host_user_id)
        VALUES (%s, %s::date, %s, %s, %s)
        RETURNING {EVENT_COLUMNS}
        """,
        (event_name, event_date, event_place, event_location, host_user_id),
    )


def get_event_contributors(event_id: int) -> list[dict]:
    """Families who have already contributed to this event, most recent first (admin sidebar default view)."""
    return _all(
        """
        SELECT u.id, u.husband_name, u.phone_number, je.amount, je.transaction_id
        FROM journal_entries je
        JOIN users u ON u.id = je.user_id
        WHERE je.event_id = %s AND je.entry_type = 'CONTRIBUTED'
        ORDER BY je.transaction_id DESC
        """,
        (event_id,),
    )


def get_event_transactions(event_id: int) -> list[dict]:
    """Return contribution details for the admin's event report."""
    return _all(
        """
        SELECT u.husband_name, u.wife_name, u.native_place, u.phone_number, je.amount
        FROM journal_entries je
        JOIN users u ON u.id = je.user_id
        WHERE je.event_id = %s AND je.entry_type = 'CONTRIBUTED'
        ORDER BY je.transaction_id
        """,
        (event_id,),
    )


def get_transaction_denomination_details(event_id: int, transaction_id: int) -> dict | None:
    rows = _all(
        """
        SELECT je.event_id, tc.staff_id, td.denomination, td.note_count
        FROM journal_entries je
        LEFT JOIN transaction_collectors tc ON tc.transaction_id = je.transaction_id
        LEFT JOIN transaction_denominations td ON td.transaction_id = je.transaction_id
        WHERE je.event_id = %s AND je.transaction_id = %s AND je.entry_type = 'CONTRIBUTED'
        ORDER BY td.denomination DESC NULLS LAST
        """,
        (event_id, transaction_id),
    )
    if not rows:
        return None
    return {
        "event_id": rows[0]["event_id"],
        "staff_id": rows[0]["staff_id"],
        "denominations": {row["denomination"]: row["note_count"] for row in rows if row["denomination"] is not None},
    }


def process_contribution(
    contributor_id: int,
    receiver_id: int,
    event_id: int,
    amount: float,
    denominations: dict[int, int] | None = None,
    staff_id: int | None = None,
) -> tuple[bool, str, int | None, int | None]:
    """Record one double-entry contribution via process_contribution(), plus the optional note breakdown and
    collecting staff member, all in one database transaction."""
    try:
        with get_pool().connection() as connection:
            event = connection.execute(
                "SELECT event_id FROM event WHERE event_id = %s AND is_active FOR UPDATE", (event_id,)
            ).fetchone()
            if not event:
                return False, "Event ID does not exist or is inactive.", None, None
            transaction_id = connection.execute(
                "SELECT process_contribution(%s::integer, %s::integer, %s::integer, %s::numeric) AS transaction_id",
                (contributor_id, receiver_id, event_id, Decimal(str(amount))),
            ).fetchone()["transaction_id"]
            existing = connection.execute(
                "SELECT serial_number FROM event_contributor_serials WHERE event_id = %s AND user_id = %s",
                (event_id, contributor_id),
            ).fetchone()
            if existing:
                serial_number = existing["serial_number"]
            else:
                serial_number = connection.execute(
                    "SELECT COALESCE(MAX(serial_number), 0) + 1 AS next_number FROM event_contributor_serials WHERE event_id = %s",
                    (event_id,),
                ).fetchone()["next_number"]
                connection.execute(
                    "INSERT INTO event_contributor_serials (event_id, user_id, serial_number) VALUES (%s, %s, %s)",
                    (event_id, contributor_id, serial_number),
                )

            for denomination, note_count in (denominations or {}).items():
                if note_count and note_count > 0:
                    connection.execute(
                        "INSERT INTO transaction_denominations (transaction_id, denomination, note_count) VALUES (%s, %s, %s)",
                        (transaction_id, denomination, note_count),
                    )

            if staff_id:
                connection.execute(
                    "INSERT INTO transaction_collectors (transaction_id, staff_id) VALUES (%s, %s)",
                    (transaction_id, staff_id),
                )
        return True, "Contribution recorded successfully.", serial_number, transaction_id
    except PsycopgError as error:
        return False, _error_message(error), None, None


def get_event_denomination_summary(event_id: int) -> dict:
    """Live total amount + note-by-note breakdown collected so far for this event."""
    total = _one(
        "SELECT COALESCE(SUM(amount), 0) AS total_amount FROM journal_entries WHERE event_id = %s AND entry_type = 'CONTRIBUTED'",
        (event_id,),
    )
    rows = _all(
        """
        SELECT td.denomination, SUM(td.note_count) AS total_notes
        FROM transaction_denominations td
        JOIN journal_entries je ON je.transaction_id = td.transaction_id AND je.entry_type = 'CONTRIBUTED'
        WHERE je.event_id = %s
        GROUP BY td.denomination
        ORDER BY td.denomination DESC
        """,
        (event_id,),
    )
    return {
        "total_amount": float(total["total_amount"]),
        "denominations": {row["denomination"]: int(row["total_notes"]) for row in rows},
    }


# ----------------------------------------------------------------------------
# Family portal
# ----------------------------------------------------------------------------

def get_family_for_login(phone_number: str) -> dict | None:
    """Look up an active family by phone for the login screen."""
    return _one(
        """
        SELECT id, husband_name, wife_name, phone_number, password_hash
        FROM users
        WHERE phone_number = %s AND is_active
        """,
        (phone_number,),
    )


def get_family_profile(user_id: int) -> dict | None:
    """Full editable profile for the logged-in family's Profile screen (no password_hash)."""
    return _one(
        """
        SELECT id, husband_name, wife_name, husband_job, wife_job, phone_number,
               native_place, current_place, others, initial, notes, is_thaimama,
               serial_number, family_deity, email, search_alias, is_active
        FROM users
        WHERE id = %s AND is_active
        """,
        (user_id,),
    )


def set_family_password_if_unset(user_id: int, new_password: str) -> bool:
    """First-time password setup; refuses if a password already exists (use change_family_password instead)."""
    with get_pool().connection() as connection:
        row = connection.execute(
            "SELECT password_hash FROM users WHERE id = %s AND is_active FOR UPDATE", (user_id,)
        ).fetchone()
        if not row or row["password_hash"]:
            return False
        connection.execute(
            "UPDATE users SET password_hash = %s, updated_at = now() WHERE id = %s",
            (hash_password(new_password), user_id),
        )
        return True


def change_family_password(user_id: int, current_password: str, new_password: str) -> tuple[bool, str]:
    """Change an existing password after verifying the current one."""
    try:
        with get_pool().connection() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id = %s AND is_active FOR UPDATE", (user_id,)
            ).fetchone()
            if not row or not row["password_hash"]:
                return False, "கடவுச்சொல் இன்னும் அமைக்கப்படவில்லை."
            if not verify_password(current_password, row["password_hash"]):
                return False, "தற்போதைய கடவுச்சொல் தவறானது."
            connection.execute(
                "UPDATE users SET password_hash = %s, updated_at = now() WHERE id = %s",
                (hash_password(new_password), user_id),
            )
            return True, "கடவுச்சொல் புதுப்பிக்கப்பட்டது."
    except PsycopgError as error:
        return False, _error_message(error)


def get_my_contributions(family_id: int) -> list[dict]:
    """Everything this family has given, with receiver + event details (double-entry read)."""
    return _all(
        """
        SELECT
            t.transaction_id, t.transaction_date, je.event_id, e.event_name,
            receiver.id AS receiver_id, receiver.husband_name AS receiver_husband_name,
            receiver.phone_number AS receiver_phone_number, je.amount
        FROM journal_entries je
        JOIN journal_entries received
            ON received.transaction_id = je.transaction_id AND received.entry_type = 'RECEIVED'
        JOIN transactions t ON t.transaction_id = je.transaction_id
        JOIN event e ON e.event_id = je.event_id
        JOIN users receiver ON receiver.id = received.user_id
        WHERE je.user_id = %s AND je.entry_type = 'CONTRIBUTED'
        ORDER BY t.transaction_date DESC, t.transaction_id DESC
        """,
        (family_id,),
    )


def get_my_receipts(family_id: int) -> list[dict]:
    """Everything this family has received, with contributor + event details (double-entry read)."""
    return _all(
        """
        SELECT
            t.transaction_id, t.transaction_date, je.event_id, e.event_name,
            contributor.id AS contributor_id, contributor.husband_name AS contributor_husband_name,
            contributor.phone_number AS contributor_phone_number, je.amount
        FROM journal_entries je
        JOIN journal_entries contributed
            ON contributed.transaction_id = je.transaction_id AND contributed.entry_type = 'CONTRIBUTED'
        JOIN transactions t ON t.transaction_id = je.transaction_id
        JOIN event e ON e.event_id = je.event_id
        JOIN users contributor ON contributor.id = contributed.user_id
        WHERE je.user_id = %s AND je.entry_type = 'RECEIVED'
        ORDER BY t.transaction_date DESC, t.transaction_id DESC
        """,
        (family_id,),
    )


def get_partner_history(family_id: int) -> list[dict]:
    """One summary row per family ever exchanged with: total given, total received, net difference."""
    return _all(
        """
        WITH tx_pairs AS (
            SELECT c.transaction_id, c.user_id AS contributor_id, r.user_id AS receiver_id, c.amount
            FROM journal_entries c
            JOIN journal_entries r
                ON r.transaction_id = c.transaction_id AND r.entry_type = 'RECEIVED'
            WHERE c.entry_type = 'CONTRIBUTED' AND (c.user_id = %(fid)s OR r.user_id = %(fid)s)
        )
        SELECT
            other.id AS other_user_id, other.husband_name, other.wife_name, other.phone_number,
            other.native_place, other.current_place,
            SUM(CASE WHEN tp.contributor_id = %(fid)s THEN tp.amount ELSE 0 END) AS total_given,
            SUM(CASE WHEN tp.receiver_id = %(fid)s THEN tp.amount ELSE 0 END) AS total_received,
            SUM(CASE WHEN tp.contributor_id = %(fid)s THEN tp.amount ELSE 0 END)
                - SUM(CASE WHEN tp.receiver_id = %(fid)s THEN tp.amount ELSE 0 END) AS net_difference
        FROM tx_pairs tp
        JOIN users other ON other.id = CASE WHEN tp.contributor_id = %(fid)s THEN tp.receiver_id ELSE tp.contributor_id END
        GROUP BY other.id, other.husband_name, other.wife_name, other.phone_number, other.native_place, other.current_place
        ORDER BY other.husband_name
        """,
        {"fid": family_id},
    )


def get_partner_transactions(family_id: int, other_family_id: int) -> list[dict]:
    """Full chronological ledger between two specific families, with a running net balance."""
    return _all(
        """
        WITH tx_pairs AS (
            SELECT c.transaction_id, c.user_id AS contributor_id, r.user_id AS receiver_id,
                   c.amount, t.transaction_date, e.event_name
            FROM journal_entries c
            JOIN journal_entries r
                ON r.transaction_id = c.transaction_id AND r.entry_type = 'RECEIVED'
            JOIN transactions t ON t.transaction_id = c.transaction_id
            JOIN event e ON e.event_id = c.event_id
            WHERE c.entry_type = 'CONTRIBUTED'
              AND ((c.user_id = %(fid)s AND r.user_id = %(other)s) OR (c.user_id = %(other)s AND r.user_id = %(fid)s))
        )
        SELECT
            transaction_id, transaction_date, event_name,
            CASE WHEN contributor_id = %(fid)s THEN 'GIVEN' ELSE 'RECEIVED' END AS direction,
            amount,
            SUM(CASE WHEN contributor_id = %(fid)s THEN amount ELSE -amount END)
                OVER (ORDER BY transaction_date, transaction_id ROWS UNBOUNDED PRECEDING) AS running_net_difference
        FROM tx_pairs
        ORDER BY transaction_date ASC, transaction_id ASC
        """,
        {"fid": family_id, "other": other_family_id},
    )
