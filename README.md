# fping-monitor

A lightweight server liveness monitor built on `fping`, SQLite and Python.
One fping invocation per cycle covers every host, so it scales linearly with
packet count, not with the number of hosts.

Designed to run as a **long-lived container daemon** — no one-shot or cron
mode is exposed. The container keeps cycling, picks up changes to
`server.yaml` on the next cycle, and survives restarts via persisted
SQLite + log volumes.

## Highlights

- Single batched `fping` call per cycle (no per-host invocations).
- Debounced state machine: `failure_threshold` consecutive failures to mark
  a host DOWN, `recovery_threshold` consecutive successes to recover.
- All state in SQLite, all configuration in YAML.
- Hosts carry **business tags** (e.g. `prod`, `db`, `shanghai`) that show up
  in alert messages for faster triage.
- Hosts carry **business tags** (e.g. `prod`, `db`, `shanghai`) that show up
  in alert messages for faster triage.
- Pluggable `Channel` interface — add Email / Slack / Cuckoo channels
  without touching the core.
- Daily rotating logs via `logging`, never `print`.
- Built-in DingTalk channel (markdown + signature), with a Cuckoo stub
  reserved for your internal alert platform.

## Layout

    fping-monitor/
    ├── monitor.py          # entry point: long-running daemon loop
    ├── scheduler.py        # StateMachine + Scheduler (one cycle)
    ├── detector.py         # FpingDetector (Protocol: detect -> {name: bool})
    ├── notifier.py         # Notifier + DingTalkChannel + CuckooChannel stub
    ├── database.py         # SQLite layer, schema in sql/schema.sql
    ├── models.py           # Host (with tags), Event, HostStatus, EventType
    ├── util.py             # YAML loading, logging setup, fping parser
    ├── conf/               # all config files in one place (single mount target)
    │   ├── config.yaml     # global config (interval, thresholds, fping, notify)
    │   └── server.yaml     # host list (name, ip, tags)
    ├── state.db            # SQLite database (created at runtime, mounted as volume)
    ├── sql/schema.sql      # DDL
    ├── logs/               # rotating logs (mounted as volume)
    ├── docs/architecture.html  # visual architecture overview
    ├── tests/              # pytest suite
    ├── Dockerfile
    └── docker-compose.yml

## Quick start (container)

```bash
# 1. Build
make docker

# 2. Set the DingTalk webhook (don't commit the secret)
export DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=..."

# 3. Edit conf/server.yaml to add the hosts you want to monitor
cat conf/server.yaml

# 4. Run as a long-lived container
make docker-up

# 5. Tail logs
docker compose logs -f monitor
```

The container:
- Mounts the whole `./conf/` directory (read-only) — any file you add there
  becomes accessible inside the container, no `docker-compose.yml` edits needed.
- Persists `state.db` and `logs/` on the host so restarts don't lose history.
- Restarts automatically unless you `make docker-down`.

**Why a `conf/` directory instead of separate files?** When you add a new
YAML (e.g. `conf/server-prod.yaml`, `conf/server-staging.yaml`, or a
dev override `conf/server.local.yaml`), you can point `--servers` at it
without touching the compose file. The whole directory is one bind mount
on the host.

## Hot reload of config & host list

**No restart needed** when you edit `server.yaml` or `config.yaml`. Two trigger modes:

1. **Passive (default)**: each detection cycle checks the mtime of both YAML
   files. If either changed, the watcher re-reads it and the next cycle uses
   the new values.
   - `server.yaml` changed → host list is upserted into SQLite immediately
   - `config.yaml` changed → `FpingDetector` / `Notifier` / `StateMachine`
     are rebuilt (new thresholds, new notification channels, new fping args
     all take effect immediately)
   - `interval` changed → the next sleep uses the new duration
   - `logging.level` changed → the logger updates its level at once

2. **Active**: send `SIGHUP` to the container to force an immediate reload
   (no need to wait for the next cycle).

```bash
# Inside a container
docker kill -s HUP fping-monitor

# Bare process (dev mode)
kill -HUP <pid>
```

Fields that hot-reload:

| Field | Behaviour |
|---|---|
| `interval` | Next sleep uses the new value |
| `failure_threshold` / `recovery_threshold` | State machine picks them up next cycle |
| `fping.*` | `FpingDetector` is rebuilt |
| `notify.channels` | `Notifier` is rebuilt (webhook URL, signature, @-mentions all hot) |
| `logging.level` | Effective immediately |
| `server.yaml` `hosts` | Upserted into SQLite at once |
| `database` path | **Not** hot-reloadable (DB connection opens at startup) |

## Logging (Logstash / ELK)

Every log line is **single-line JSON**, so Logstash can consume it with
`codec => json_lines` — no grok patterns required. `logging.format: json`
is the default in `config.yaml`.

**Sample output** (`logs/fping_monitor.log`):

```json
{"ts":"2026-07-17T09:16:51.772580+00:00","level":"INFO","logger":"fping_monitor","message":"收到 SIGHUP，强制重载配置","signal":"SIGHUP"}
{"ts":"2026-07-17T09:16:51.772889+00:00","level":"INFO","logger":"fping_monitor","message":"检测结果","event":"detection","results":{"gw":true,"dns8":false}}
{"ts":"2026-07-17T09:16:51.772939+00:00","level":"INFO","logger":"fping_monitor","message":"状态变更","event":"state_change","host":"gw","ip":"192.168.1.1","tags":["network","infra"],"from_status":"UP","to_status":"DOWN","fired_kind":"DOWN"}
```

Fixed fields: `ts` (UTC ISO 8601) / `level` / `logger` / `message`
Custom fields: anything passed via `log.info(..., extra={"k": "v"})` becomes
a top-level JSON field.

**Minimal Logstash pipeline**:

```ruby
input {
  file {
    path => "/var/log/fping-monitor/*.log"
    start_position => "beginning"
    sincedb_path => "/var/lib/logstash/sincedb_fping"
    codec => "json_lines"
  }
}

filter {
  if [event] == "state_change" { mutate { add_tag => ["fping_state_change"] } }
  if [level] == "ERROR"       { mutate { add_tag => ["alert"] } }

  date {
    match  => ["ts", "ISO8601"]
    target => "@timestamp"
  }
}

output {
  elasticsearch {
    hosts => ["http://es:9200"]
    index => "fping-monitor-%{+YYYY.MM.dd}"
  }
}
```

**Useful business fields for Kibana dashboards / alerts**:

| Field | Type | When | Use |
|---|---|---|---|
| `event="detection"` | object | every cycle | `results.<host_name>` is bool per host |
| `event="state_change"` | object | on state transitions | `host` + `from_status` + `to_status` |
| `host` | keyword | same | host name (indexed) |
| `tags` | keyword[] | same | business tag array (filterable) |
| `channel` | keyword | notify failure | which channel had the problem |
| `errcode` / `errmsg` | keyword/int | DingTalk business error | debug webhook failures |
| `cycle_changes` | int | every cycle | transitions this cycle (>0 = worth a look) |
| `hosts` | int | server.yaml change | number of hosts synced |
| `signal` | keyword | on signal | SIGHUP / SIGTERM etc. |

To get a human-readable text format for local dev, set
`logging.format: text` in `config.yaml` — the change takes effect on the
next cycle (handler is rebuilt).

## State machine

```
UNKNOWN + alive   -> UP        (no notify)
UNKNOWN + dead    -> UNKNOWN   (count, no notify)
UP      + alive   -> UP        (counters reset)
UP      + dead    -> UP        (fail_count++; if >= F -> DOWN, notify)
DOWN    + alive   -> DOWN      (recover_count++; if >= R -> UP, notify)
DOWN    + dead    -> DOWN      (fail_count++, no notify)
```

`F` = `failure_threshold`, `R` = `recovery_threshold`.

## Host tags

`server.yaml` hosts may include a `tags` list. Tags are persisted in
SQLite and rendered in alert messages:

```yaml
hosts:
  - name: db-prod-sh-1
    ip: 10.1.2.3
    tags: [prod, db, shanghai]
```

The DingTalk alert body looks like:

```
### 🔴 DOWN: db-prod-sh-1 (10.1.2.3)

- 状态：**DOWN**
- tags: `prod`,`db`,`shanghai`
- 时间：2026-07-17T15:30:00
- 连续失败：3  连续成功：0
```

## Bulk IP spec in `server.yaml`

The `ip` field accepts several shorthand forms so you don't have to spell
out every host one by one:

| Syntax | Meaning | Expanded count |
|---|---|---|
| `"10.1.2.3"` | single IP | 1 |
| `"10.1.2.0/24"` | CIDR (excludes net/broadcast) | 254 |
| `"10.1.2.0/30"` | CIDR | 2 (drops .0 and .3) |
| `"10.1.2.3-10.1.2.10"` | full range | 8 |
| `"10.1.2.3-10"` | short range (end = last octet) | 8 |
| `["8.8.8.8", "1.1.1.0/30"]` | list of mixed specs | 3 |

When a spec expands to multiple IPs, **`name` is auto-suffixed with
`-<ip>`** to keep rows unique:

```yaml
hosts:
  - name: web
    ip: 10.1.2.0/28
    tags: [web, prod]
```

becomes `web-10.1.2.1` ... `web-10.1.2.14` (14 rows, tags propagated).

**Safety rail**: a single spec may not expand to more than 1024 hosts
(protects against accidental `0.0.0.0/0`). Violations abort the upsert
without partial writes.

## Adding a new channel (Cuckoo, email, …)

1. Subclass in `notifier.py` (mirror `DingTalkChannel`).
2. Register in `_CHANNELS = {..., "cuckoo": CuckooChannel}`.
3. Add to `config.yaml`:

```yaml
notify:
  channels:
    - type: cuckoo
      endpoint: https://cuckoo.internal/api/alert
      token: ${CUCKOO_TOKEN}
```

Cuckoo is currently a stub that raises `NotImplementedError` on
construction — it shows up in `_CHANNELS` so config validation works,
but the channel is skipped until you implement it.

## Health check (docker HEALTHCHECK)

The container has a built-in `HEALTHCHECK` that runs

```bash
python monitor.py healthcheck
```

It does exactly two things:

1. Opens the SQLite database and reads the host list
2. Runs `fping` once against `healthcheck.gateway` (default `1.1.1.1`)

Exit code `0` = healthy, `1` = unhealthy. With `docker compose ps` you
see the status; with `docker inspect` you get the full output.

The gateway field is just a reachability smoke test — change it to
whatever address you trust to be up (your router, an internal VIP, etc.).

## Tests

```bash
make test
```

Coverage (33 tests):

- `test_parser.py` — fping output parser
- `test_state_machine.py` — full transition table
- `test_database.py` — SQLite round-trip + tag persistence
- `test_notifier.py` — DingTalk payloads (Mock) + signature + env var injection
- `test_scheduler.py` — end-to-end with fake detector

## Architecture overview

See `docs/architecture.html` for a visual map of the modules, data flow,
sequence of a cycle, state machine, and extension points.
