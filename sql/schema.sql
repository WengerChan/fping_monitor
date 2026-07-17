-- fping_monitor initial schema
-- State values: UNKNOWN, UP, DOWN
-- Event values: DOWN, RECOVER

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS hosts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE,
    ip            TEXT    NOT NULL,
    tags          TEXT    NOT NULL DEFAULT '',  -- 逗号分隔，例如 "prod,db,shanghai"
    status        TEXT    NOT NULL DEFAULT 'UNKNOWN'
                  CHECK (status IN ('UNKNOWN', 'UP', 'DOWN')),
    fail_count    INTEGER NOT NULL DEFAULT 0,
    recover_count INTEGER NOT NULL DEFAULT 0,
    last_check    TEXT,
    last_change   TEXT
);

CREATE INDEX IF NOT EXISTS idx_hosts_status ON hosts(status);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id INTEGER NOT NULL,
    event   TEXT    NOT NULL CHECK (event IN ('DOWN', 'RECOVER')),
    time    TEXT    NOT NULL,
    message TEXT,
    FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_host_time ON events(host_id, time DESC);
