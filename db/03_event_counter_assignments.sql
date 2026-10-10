IF OBJECT_ID(N'dbo.event_counter_assignments', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.event_counter_assignments (
        event_id INT NOT NULL,
        staff_id INT NOT NULL,
        assigned_by INT NOT NULL,
        assigned_at DATETIME2 NOT NULL CONSTRAINT DF_event_counter_assignments_assigned_at DEFAULT SYSDATETIME(),
        CONSTRAINT PK_event_counter_assignments PRIMARY KEY (event_id, staff_id),
        CONSTRAINT FK_event_counter_assignments_event FOREIGN KEY (event_id)
            REFERENCES dbo.event (event_id) ON DELETE CASCADE,
        CONSTRAINT FK_event_counter_assignments_staff FOREIGN KEY (staff_id)
            REFERENCES dbo.staff_accounts (staff_id),
        CONSTRAINT FK_event_counter_assignments_assigned_by FOREIGN KEY (assigned_by)
            REFERENCES dbo.staff_accounts (staff_id)
    );
END;