-- OPTIONAL: run after migrations 03, 04 and 05, only if you want test records.
-- Uses existing active staff; never creates logins or changes PINs/passwords.
-- Re-running skips an already-created demo event. All inserts are atomic.
BEGIN;
SET LOCAL search_path = public;

DO $$
DECLARE
    v_marker constant text := 'MOISEI_SUPABASE_UPGRADE_DEMO_V1';
    v_event_name constant text := '[DEMO] Supabase upgrade test V1';
    v_admin integer;
    v_counters integer[];
    v_families integer[] := ARRAY[]::integer[];
    v_user integer;
    v_event integer;
    v_tx integer;
    v_staff integer;
    v_i integer;
    v_phone text;
    v_before text;
    v_after text;
BEGIN
    -- Serializes repeated SQL Editor runs of this demo script.
    PERFORM pg_advisory_xact_lock(615042601);
    SELECT min(staff_id) INTO v_admin
    FROM staff_accounts WHERE is_active AND role = 'admin';
    SELECT array_agg(staff_id ORDER BY staff_id) INTO v_counters
    FROM staff_accounts WHERE is_active AND role = 'counter';

    IF v_admin IS NULL OR coalesce(cardinality(v_counters), 0) = 0 THEN
        RAISE EXCEPTION 'Create an active admin and at least one active counter in the app before running the demo.';
    END IF;
    IF EXISTS (SELECT 1 FROM event WHERE event_name = v_event_name) THEN
        RAISE NOTICE 'Demo event already exists; no data inserted.';
        RETURN;
    END IF;

    FOR v_i IN 1..4 LOOP
        v_phone := '999990000' || v_i::text;
        IF EXISTS (SELECT 1 FROM users WHERE phone_number = v_phone) THEN
            RAISE EXCEPTION 'Demo phone % already exists. No changes saved; choose unused demo phone numbers.', v_phone;
        END IF;
        INSERT INTO users (
            husband_name, wife_name, phone_number, native_place, current_place, notes
        ) VALUES (
            CASE WHEN v_i = 1 THEN '[DEMO] Host Family'
                 ELSE '[DEMO] Contributor ' || (v_i - 1)::text END,
            '[DEMO] Spouse', v_phone, 'Demo Town', 'Demo Town', v_marker
        ) RETURNING id INTO v_user;
        v_families := array_append(v_families, v_user);
    END LOOP;

    INSERT INTO event (
        event_name, event_date, event_place, event_location, host_user_id
    ) VALUES (
        v_event_name, (now() AT TIME ZONE 'Asia/Kolkata')::date,
        '[DEMO] Hall', '[DEMO] Town', v_families[1]
    ) RETURNING event_id INTO v_event;

    FOREACH v_staff IN ARRAY v_counters LOOP
        INSERT INTO event_counter_assignments(event_id, staff_id, assigned_by)
        VALUES (v_event, v_staff, v_admin);
    END LOOP;

    FOR v_i IN 1..3 LOOP
        v_staff := v_counters[1 + ((v_i - 1) % cardinality(v_counters))];
        v_tx := process_contribution(v_families[v_i + 1], v_families[1], v_event,
                                    CASE v_i WHEN 1 THEN 1000 WHEN 2 THEN 750 ELSE 500 END);
        IF v_i = 1 THEN
            INSERT INTO transaction_denominations VALUES (v_tx, 500, 2);
        ELSIF v_i = 2 THEN
            INSERT INTO transaction_denominations VALUES
                (v_tx, 500, 1), (v_tx, 200, 1), (v_tx, 50, 1);
        ELSE
            INSERT INTO transaction_denominations VALUES (v_tx, 100, 5);
        END IF;
        INSERT INTO transaction_collectors(transaction_id, staff_id)
        VALUES (v_tx, v_staff);
        INSERT INTO event_contributor_serials(event_id, user_id, serial_number)
        VALUES (v_event, v_families[v_i + 1], v_i);
    END LOOP;

    SELECT (to_jsonb(u) - 'password_hash')::text INTO v_before
    FROM users u WHERE id = v_families[2];
    UPDATE users SET current_place = 'Demo Updated Town', updated_at = now()
    WHERE id = v_families[2];
    SELECT (to_jsonb(u) - 'password_hash')::text INTO v_after
    FROM users u WHERE id = v_families[2];
    INSERT INTO user_change_history(user_id, changed_by_staff_id, before_json, after_json)
    VALUES (v_families[2], v_admin, v_before, v_after);

    RAISE NOTICE 'Created demo event %, 4 families, 3 contributing families, total 2250. Next event serial is 4.', v_event;
END;
$$;
COMMIT;

-- Expect: collected_family_count = 3, next_serial_number = 4, total_collected = 2250.
SELECT e.event_id, e.event_name,
       (SELECT count(*) FROM event_contributor_serials s
        WHERE s.event_id = e.event_id) AS collected_family_count,
       (SELECT coalesce(max(s.serial_number), 0) + 1 FROM event_contributor_serials s
        WHERE s.event_id = e.event_id) AS next_serial_number,
       (SELECT sum(j.amount) FROM journal_entries j
        WHERE j.event_id = e.event_id AND j.entry_type = 'CONTRIBUTED') AS total_collected
FROM event e WHERE e.event_name = '[DEMO] Supabase upgrade test V1';
