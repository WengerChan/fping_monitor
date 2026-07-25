# Changelog

fping-monitor 的所有重要改动按时间倒序记录。

## [unreleased] · 2026-07-25

### Refactored

- **健康检查改为独立 `healthcheck.py` 脚本**：原来 `python monitor.py
  healthcheck` 子命令从 `monitor.py` 中剥离出来，做成项目根目录下的
  独立脚本。`Dockerfile` 的 `HEALTHCHECK` 指令现在直接调用
  `python healthcheck.py`，不依赖 `monitor.py` 的 import / 配置热加载
  / 日志初始化链路。`monitor.py` CLI 简化为单进程长驻主循环（仅
  `--config` / `--servers` 两个参数）。脚本检查 SQLite 连通性 +
  fping 探活 `healthcheck.gateway`（默认 `1.1.1.1`），0/1 退出码 + stderr
  失败原因，行为与原实现一致。

### 重构

- **删除 HTTP health server**：原来用 `ThreadingHTTPServer` 跑 9090 端口
  暴露 `/health` `/ready` `/status` 三 endpoint。改为单进程 CLI 子命令
  `python monitor.py healthcheck`，只做"打开 SQLite + fping 探活"
  两项检查，0/1 退出码。Dockerfile HEALTHCHECK 直接调子命令。
- **数据目录改为 `data/`**：state.db 从根目录挪到 `data/state.db`，
  docker-compose 挂载 `./data:/app/data`，`.gitignore` 屏蔽。
- **配置文件全部入 `conf/` 目录**：原来散落的两个 yaml 移到
  `conf/config.yaml` + `conf/server.yaml`，docker 单目录挂载
  `./conf:/app/conf:ro`。新增环境/本地覆盖不用改 compose。
- **删除非 ping 探活扩展点**：`Detector` Protocol 和 `merge_results`
  函数删除，scheduler.detector 类型改 `FpingDetector`。`detector.py`
  只剩 FpingDetector。README/architecture 里的"加 TCP/HTTP/SSH 探活"
  章节全部删除。
- **依赖拆分**：`requirements.txt` 只保留 prod 依赖（PyYAML + requests），
  新增 `requirements-dev.txt` 用于本地测试（pytest + pytest-mock）。
- **日志格式 JSON 化**：默认 `logging.format: json`，每行 JSON 便于
  logstash `codec => json_lines` 直接吃。业务代码关键日志加 `extra=`
  字段结构化（host/tags/from_status/to_status/cycle_changes/signal
  /channel/errcode 等）。
- **新增 .dockerignore**：减少 Docker build context，加快 build。

### 新增功能

- **配置 + 主机列表热加载**（`util.ConfigWatcher`）：每轮循环开头检查
  config.yaml / server.yaml 的 mtime，变了就重读并重建组件。SIGHUP
  信号可强制立即重载。`interval` `failure_threshold` `recovery_threshold`
  `fping.*` `notify.channels` `logging.level` `server.yaml` 的 hosts
  全部支持热改。
- **Host tags**：`models.Host.tags: List[str]`，DB 用逗号分隔存储。
  钉钉告警消息里展示 tags 方便定位。upsert_hosts 同时更新 tags。
- **IP 简写批量设置**（`util.expand_ip_spec`）：`server.yaml` 的
  `ip` 字段支持 4 种形式：单 IP / CIDR / 完整范围 / 短范围。还支持
  list 形式混合多种 spec。展开后 `name` 自动追加 `-<ip>` 后缀。
  单条 spec 最多展开 1024 个主机（防 `0.0.0.0/0` 误操作）。
- **健康检查**（`monitor.py:run_healthcheck`）：CLI 子命令形式，
  给 docker HEALTHCHECK 用。检查 SQLite 可达 + fping 探活
  `healthcheck.gateway`。
- **钉钉通知完整实现**：`DingTalkChannel` 支持 markdown 消息、
  `@atMobiles` / `@atAll`、可选加签（HMAC-SHA256 拼 timestamp +
  sign 到 URL）。
- **CuckooChannel 占位**：布谷鸟告警平台，内部平台后续接入时直接
  替换实现即可，配置层已经预留。
- **README.cn.md**：中文版使用说明。

### 文档

- **`docs/architecture.html`**：可视化架构总览，6+ 章节
  （模块分层 / 数据流 / 时序 / 状态机 / 日志格式 / 热加载 /
  IP 简写 / conf 目录 / 源码清单 / 扩展点）。
- **CHANGELOG.md**（本文件）：记录每次重要改动。
- **Makefile**：补 `help` / `dev` / `test-cov` / `clean` / `docker-up`
  / `docker-down`。

### 测试

- 全模块覆盖测试（`make test` 跑整套，`pytest --collect-only -q` 看完整列表）；
  - `test_parser.py` — fping 输出解析
  - `test_database.py` — SQLite 增删改查 + tags 持久化
  - `test_state_machine.py` — 状态机全部转换路径
  - `test_scheduler.py` — 端到端
  - `test_notifier.py` — 钉钉负载校验 + 加签 + env 注入
  - `test_config_watcher.py` — 热加载 mtime 检测 + 强 reload
  - `test_json_formatter.py` — JSON 日志格式
  - `test_ip_expand.py` — IP 简写展开所有 case
  - `test_healthcheck.py` — CLI 子命令行为
- GitHub Actions 在 Python 3.11 / 3.12 上跑全套测试。
