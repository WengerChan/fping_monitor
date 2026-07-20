-- fping_monitor initial schema
-- State values: UNKNOWN, UP, DOWN
-- Event values: DOWN, RECOVER

-- WAL 模式 + NORMAL 同步：写性能优于 FULL，对崩溃一致性影响可控（最近
-- 一两次事务可能丢，但监控场景重跑一轮即可恢复）。
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- 当前 schema 版本。启动时如果 user_version=0 才执行 DDL，并设成 1。
PRAGMA user_version;

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
