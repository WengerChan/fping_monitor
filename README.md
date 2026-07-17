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
- Pluggable `Detector` and `Channel` interfaces — add TCP / HTTP / SSH
  probes or new notification channels without touching the core.
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
    ├── config.yaml         # global config (interval, thresholds, fping, notify)
    ├── server.yaml         # host list (name, ip, tags)
    ├── state.db            # SQLite database (created at runtime, mounted as volume)
    ├── sql/schema.sql      # DDL
    ├── logs/               # rotating logs (mounted as volume)
    ├── docs/architecture.html  # visual architecture overview
    ├── tests/              # pytest suite
    ├── Dockerfile
    ├── docker-compose.yml
    └── Makefile

## Quick start (container)

```bash
# 1. Build
make docker

# 2. Set the DingTalk webhook (don't commit the secret)
export DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=..."

# 3. Edit server.yaml to add the hosts you want to monitor
cat server.yaml

# 4. Run as a long-lived container
make docker-up

# 5. Tail logs
docker compose logs -f monitor
```

The container:
- Reads `config.yaml` and `server.yaml` from the project directory (mounted read-only).
- Persists `state.db` and `logs/` on the host so restarts don't lose history.
- Restarts automatically unless you `make docker-down`.

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

## Adding a new probe (TCP / HTTP / SSH)

Implement the `Detector` protocol and inject it into `Scheduler`:

```python
class HttpsDetector:
    def detect(self, hosts): ...

# in monitor.py build_components:
scheduler = Scheduler(cfg=cfg, db=db,
                      detector=CompositeDetector(FpingDetector(...), HttpsDetector(...)),
                      notifier=notifier)
```

`merge_results(*maps)` in `detector.py` ANDs results across detectors so
you can require "ping AND https" without any other change.

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
