IF OBJECT_ID(N'dbo.user_change_history', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.user_change_history (
        history_id BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_user_change_history PRIMARY KEY,
        user_id INT NOT NULL,
        changed_by_staff_id INT NOT NULL,
        changed_at DATETIME2(3) NOT NULL CONSTRAINT DF_user_change_history_changed_at DEFAULT SYSDATETIME(),
        before_json NVARCHAR(MAX) NOT NULL,
        after_json NVARCHAR(MAX) NOT NULL,
        CONSTRAINT FK_user_change_history_user FOREIGN KEY (user_id) REFERENCES dbo.Users (id),
        CONSTRAINT FK_user_change_history_staff FOREIGN KEY (changed_by_staff_id) REFERENCES dbo.staff_accounts (staff_id)
    );

    CREATE INDEX IX_user_change_history_changed_at
        ON dbo.user_change_history (changed_at DESC, history_id DESC);
    CREATE INDEX IX_user_change_history_user_id
        ON dbo.user_change_history (user_id, changed_at DESC);
END;