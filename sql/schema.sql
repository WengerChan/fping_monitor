-- fping_monitor initial schema
-- State values: UNKNOWN, UP, DOWN
-- Event values: DOWN, RECOVER

-- 注意：本文件只放 DDL。PRAGMA 设置（journal_mode / synchronous /
-- foreign_keys）统一在 ``database.Database._open_connection`` 里应用，
-- 每次新连接都会重新设置一次（这两个 PRAGMA 是连接级别的，不会写进
-- db header），别在这里重复设，避免双源真理。

-- 当前 schema 版本。启动时如果 user_version=0 才执行 DDL，并设成 1。
-- ``PRAGMA user_version`` 由 ``database.Database._init_schema`` 直接
-- 读取 / 写入，不依赖本文件。

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
