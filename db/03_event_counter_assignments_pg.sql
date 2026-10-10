-- Supabase SQL Editor: additive upgrade for an existing database.
BEGIN;

CREATE TABLE IF NOT EXISTS public.event_counter_assignments (
    event_id integer NOT NULL REFERENCES public.event(event_id) ON DELETE CASCADE,
    staff_id integer NOT NULL REFERENCES public.staff_accounts(staff_id),
    assigned_by integer NOT NULL REFERENCES public.staff_accounts(staff_id),
    assigned_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, staff_id)
);

ALTER TABLE public.event_counter_assignments ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.event_counter_assignments FROM anon, authenticated;
COMMIT;
