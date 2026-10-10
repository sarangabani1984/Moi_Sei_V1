-- PostgreSQL: run on existing deployments before starting the updated API.
BEGIN;
SET LOCAL search_path = public;
LOCK TABLE journal_entries IN ACCESS EXCLUSIVE MODE;
CREATE TABLE IF NOT EXISTS event_contributor_serials (
    event_id integer NOT NULL REFERENCES event(event_id),
    user_id integer NOT NULL REFERENCES users(id),
    serial_number integer NOT NULL CHECK (serial_number > 0),
    PRIMARY KEY (event_id, user_id),
    UNIQUE (event_id, serial_number)
);
LOCK TABLE event_contributor_serials IN ACCESS EXCLUSIVE MODE;

WITH families AS (
    SELECT event_id, user_id, MIN(transaction_id) AS first_transaction_id
    FROM journal_entries WHERE entry_type = 'CONTRIBUTED'
    GROUP BY event_id, user_id
), missing AS (
    SELECT f.* FROM families f
    WHERE NOT EXISTS (
        SELECT 1 FROM event_contributor_serials s
        WHERE s.event_id = f.event_id AND s.user_id = f.user_id
    )
), numbered AS (
    SELECT m.event_id, m.user_id,
        COALESCE((SELECT MAX(s.serial_number) FROM event_contributor_serials s
                  WHERE s.event_id = m.event_id), 0)
        + ROW_NUMBER() OVER (
            PARTITION BY m.event_id ORDER BY m.first_transaction_id, m.user_id
        ) AS serial_number
    FROM missing m
)
INSERT INTO event_contributor_serials (event_id, user_id, serial_number)
SELECT event_id, user_id, serial_number FROM numbered;

ALTER TABLE event_contributor_serials ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE event_contributor_serials FROM anon, authenticated;
COMMIT;
