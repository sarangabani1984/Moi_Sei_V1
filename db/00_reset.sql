-- DESTRUCTIVE: drops every E TO E object in the public schema. Run only on an empty/disposable Supabase project.
drop function if exists process_contribution(integer, integer, integer, numeric);
drop table if exists transaction_collectors cascade;
drop table if exists transaction_denominations cascade;
drop table if exists event_contributor_serials cascade;
drop table if exists user_change_history cascade;
drop table if exists event_counter_assignments cascade;
drop table if exists journal_entries cascade;
drop table if exists transactions cascade;
drop table if exists event cascade;
drop table if exists staff_accounts cascade;
drop table if exists users cascade;
drop sequence if exists users_serial_number_seq;

-- Leftovers from the older Moi_Sei Supabase script, if it was ever run on this project.
drop view if exists event_summary cascade;
drop view if exists family_transactions cascade;
drop table if exists denomination_tracking cascade;
drop table if exists staff_sessions cascade;
drop table if exists events cascade;
