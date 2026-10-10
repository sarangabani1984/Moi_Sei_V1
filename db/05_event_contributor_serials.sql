-- SQL Server: one serial per unique contributing family per event.
-- Existing families are numbered by their first recorded contribution.
SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF OBJECT_ID(N'dbo.event_contributor_serials', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.event_contributor_serials (
        event_id INT NOT NULL REFERENCES dbo.event(event_id),
        user_id INT NOT NULL REFERENCES dbo.Users(id),
        serial_number INT NOT NULL CHECK (serial_number > 0),
        CONSTRAINT PK_event_contributor_serials PRIMARY KEY (event_id, user_id),
        CONSTRAINT UQ_event_contributor_serials_number UNIQUE (event_id, serial_number)
    );

    ;WITH families AS (
        SELECT event_id, user_id, MIN(transaction_id) AS first_transaction_id
        FROM dbo.journal_entries WITH (TABLOCKX, HOLDLOCK)
        WHERE entry_type = 'CONTRIBUTED'
        GROUP BY event_id, user_id
    )
    INSERT INTO dbo.event_contributor_serials (event_id, user_id, serial_number)
    SELECT event_id, user_id,
        ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY first_transaction_id, user_id)
    FROM families;
END;

COMMIT TRANSACTION;
