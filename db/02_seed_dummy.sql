-- Dummy data for development only. Delete the staff rows and rotate PINs before real use.
-- Dummy logins:  Admin PIN 9001 | Counter 1 PIN 1001 | Counter 2 PIN 1002 | family 9000000001 / Test@123

insert into staff_accounts (display_name, pin_hash, role) values
 ('Admin',     'pbkdf2_sha256$200000$b288bb5d3d5f37d01d842f52cd1ea2d9$86d759461ccf8f1c3c63bd60d05672b53c3b8f68d018091d6e19d1c3a3e823b4', 'admin'),
 ('Counter 1', 'pbkdf2_sha256$200000$575164948835b1bc16f83130f95d084f$d0b93ea08e029f1d02955c121c2e841dd551de06c0a55b605b26b95b3ba6faca', 'counter'),
 ('Counter 2', 'pbkdf2_sha256$200000$62b85b89a33873e6a8ca8143c30b5832$a7eb6f1f368f5997eee8c5406aa9b3e267ae3af6cc9bdd769d1772c9f68192ca', 'counter');

insert into users (husband_name, wife_name, husband_job, phone_number, native_place, current_place, wife_job, is_thaimama, password_hash) values
 ('அருண் குமார்',  'பிரியா',   'Engineer',  '9000000001', 'திருச்சி',  'சென்னை',     'Teacher',   false,
  'pbkdf2_sha256$200000$7691e97876c26c765282fe39ae9c9033$7ee501f3d49eb7fe062d1e91e973858b60f494f5ec6bff9b434c11f29aa350ef'),
 ('சுரேஷ்',        'கவிதா',    'Farmer',    '9000000002', 'சேலம்',     'சேலம்',      null,        true,  null),
 ('ராஜேஷ்',        'தீபா',     'Business',  '9000000003', 'வேலூர்',    'கோயம்புத்தூர்', 'Nurse',  false, null),
 ('முருகன்',       'லட்சுமி',  'Driver',    '9000000004', 'மதுரை',     'மதுரை',      null,        false, null),
 ('செல்வம்',       'மீனா',     'Teacher',   '9000000005', 'தஞ்சாவூர்', 'பெங்களூரு',  'Tailor',    false, null),
 ('Test Husband 6', 'Test Wife 6', 'Clerk',   '9000000006', 'Hosur',     'Hosur',      null,        false, null),
 ('Test Husband 7', 'Test Wife 7', 'Shop',    '9000000007', 'Erode',     'Erode',      null,        false, null),
 ('Test Husband 8', 'Test Wife 8', 'Doctor',  '9000000008', 'Karur',     'Chennai',    'Doctor',    false, null);

-- Event dates are relative to today (IST) because contributions cannot be dated before their event.
insert into event (event_name, event_date, event_place, event_location, host_user_id) values
 ('Test Wedding',        (now() at time zone 'Asia/Kolkata')::date - 7, 'Test Mandapam', 'Trichy', 1),
 ('Test Housewarming',   (now() at time zone 'Asia/Kolkata')::date,     'Test Hall',     'Salem',  null);

-- Sample contributions to event 1 (host = family 1), with note breakdowns and the counter who collected them.
do $$
declare
    v_tx integer;
begin
    v_tx := process_contribution(2, 1, 1, 1000);
    insert into transaction_denominations values (v_tx, 500, 2);
    insert into transaction_collectors (transaction_id, staff_id) values (v_tx, 2);

    v_tx := process_contribution(3, 1, 1, 750);
    insert into transaction_denominations values (v_tx, 500, 1), (v_tx, 200, 1), (v_tx, 50, 1);
    insert into transaction_collectors (transaction_id, staff_id) values (v_tx, 2);

    v_tx := process_contribution(4, 1, 1, 500);
    insert into transaction_denominations values (v_tx, 100, 5);
    insert into transaction_collectors (transaction_id, staff_id) values (v_tx, 3);
end;
$$;
