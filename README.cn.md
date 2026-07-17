# fping-monitor

一个轻量级的服务器存活监控系统，基于 `fping` + SQLite + Python。
每个检测周期只调用一次 `fping` 完成全部主机的批量化探活，扩展性好，开销与
主机数无关、只与探测包数线性相关。

**只支持容器长驻模式**：容器内进程持续循环，不暴露单次执行或 cron 调度。
下次循环会热加载 `server.yaml` 的变更，重启时通过持久化的 SQLite 和日志
卷保留历史。

## 特性

- 单次批量化 `fping` 调用（不是逐台 ping）。
- 状态机带防抖：连续 `failure_threshold` 次失败才判定 DOWN，连续
  `recovery_threshold` 次成功才恢复 UP。
- 状态持久化用 SQLite，配置用 YAML。
- 主机支持**业务标签**（如 `prod` / `db` / `shanghai`），告警消息中
  展示方便快速定位。
- `Detector` / `Channel` 都是接口协议，新增 TCP / HTTP / SSH 探活或
  新通知渠道不用改核心逻辑。
- 日志用 `logging` 库按天滚动，全项目不允许 `print`。
- 内置钉钉渠道（markdown + 加签），布谷鸟作为占位渠道预留扩展点。

## 目录结构

    fping-monitor/
    ├── monitor.py          # 入口：长驻主循环
    ├── scheduler.py        # 状态机 + 单次调度
    ├── detector.py         # FpingDetector（协议：detect -> {name: bool}）
    ├── notifier.py         # 通知器 + DingTalkChannel + CuckooChannel 占位
    ├── database.py         # SQLite 持久化层，DDL 在 sql/schema.sql
    ├── models.py           # Host（含 tags）/ Event / HostStatus / EventType
    ├── util.py             # YAML 加载、日志初始化、fping 输出解析
    ├── config.yaml         # 全局配置（间隔、阈值、fping 参数、通知渠道）
    ├── server.yaml         # 主机列表（name / ip / tags）
    ├── state.db            # SQLite 数据库（运行时创建，挂载为 volume）
    ├── sql/schema.sql      # 建表 DDL
    ├── logs/               # 按天滚动的日志（挂载为 volume）
    ├── docs/architecture.html  # 架构总览可视化页面
    ├── tests/              # pytest 测试集
    ├── Dockerfile
    ├── docker-compose.yml
    └── Makefile

## 快速开始（容器）

```bash
# 1. 构建镜像
make docker

# 2. 设置钉钉 Webhook（密钥不要写进 YAML）
export DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=..."

# 3. 编辑 server.yaml，填入要监控的主机
cat server.yaml

# 4. 启动长驻容器
make docker-up

# 5. 跟踪日志
docker compose logs -f monitor
```

容器行为：
- 配置文件从宿主机目录挂载（只读）
- `state.db` 和 `logs/` 持久化在宿主机，重启不丢历史
- 异常退出自动重启（`restart: unless-stopped`），停止用 `make docker-down`

## 配置热加载

**修改 `server.yaml` / `config.yaml` 后无需重启容器**。两种触发方式：

1. **被动（默认）**：每轮检测开头检查两个 YAML 的 mtime，变了就重读并在下一轮生效
   - `server.yaml` 变更 → 立即把新主机列表同步进 SQLite
   - `config.yaml` 变更 → 重建 `FpingDetector` / `Notifier` / `StateMachine`（新阈值、新通知渠道、新 fping 参数立刻生效）
   - `interval` 变化 → 下一轮 sleep 时长按新值
   - `logging.level` 变化 → logger 立即更新级别

2. **主动**：发 `SIGHUP` 给容器，立刻强制 reload（不等下一轮）

```bash
# 容器里手动触发
docker kill -s HUP fping-monitor

# 主机进程（开发态）
kill -HUP <pid>
```

完整支持热改的字段：

| 字段 | 行为 |
|---|---|
| `interval` | 下一轮 sleep 用新值 |
| `failure_threshold` / `recovery_threshold` | 新一轮状态机使用 |
| `fping.*` | 重建 FpingDetector |
| `notify.channels` | 重建 Notifier（包括 Webhook URL、加签密钥、@配置） |
| `logging.level` | 立即生效 |
| `server.yaml` 的 `hosts` | 立即 upsert 进 SQLite |
| `database` 路径 | **不支持**（DB 连接在启动时建立，需重启） |

## 状态机

```
UNKNOWN + 可达    -> UP        （不通知）
UNKNOWN + 不可达  -> UNKNOWN   （只累加失败计数，不通知）
UP      + 可达    -> UP        （计数器重置）
UP      + 不可达  -> UP        （失败计数 +1；达到 F 则 DOWN + 通知）
DOWN    + 可达    -> DOWN      （恢复计数 +1；达到 R 则 UP + 通知）
DOWN    + 不可达  -> DOWN      （失败计数 +1，不通知）
```

其中 `F` = `failure_threshold`，`R` = `recovery_threshold`。

## 主机标签（tags）

`server.yaml` 里每台主机可附带 `tags` 列表。标签会持久化到 SQLite，
告警时一起展示：

```yaml
hosts:
  - name: db-prod-sh-1
    ip: 10.1.2.3
    tags: [prod, db, shanghai]
```

钉钉告警消息示例：

```
### 🔴 DOWN: db-prod-sh-1 (10.1.2.3)

- 状态：**DOWN**
- tags: `prod`,`db`,`shanghai`
- 时间：2026-07-17T15:30:00
- 连续失败：3  连续成功：0
```

## 新增探活方式（TCP / HTTP / SSH）

实现 `Detector` 协议后注入 `Scheduler` 即可：

```python
class HttpsDetector:
    def detect(self, hosts): ...

# monitor.py build_components 里
scheduler = Scheduler(
    cfg=cfg, db=db,
    detector=CompositeDetector(FpingDetector(...), HttpsDetector(...)),
    notifier=notifier,
)
```

`detector.py` 里的 `merge_results(*maps)` 会把所有检测器结果做 AND 合并，
因此可以配置成"ping 通 AND https 通"才视为 UP，无需改动核心代码。

## 新增通知渠道（布谷鸟、邮件、Slack…）

1. 在 `notifier.py` 里仿照 `DingTalkChannel` 实现一个新类。
2. 在 `_CHANNELS` 字典中注册：

   ```python
   _CHANNELS = {..., "cuckoo": CuckooChannel}
   ```

3. 在 `config.yaml` 中启用：

   ```yaml
   notify:
     channels:
       - type: cuckoo
         endpoint: https://cuckoo.internal/api/alert
         token: ${CUCKOO_TOKEN}
   ```

布谷鸟目前是占位类，构造时立即抛 `NotImplementedError`——在 `_CHANNELS`
里有名字所以配置可以校验，但 `Notifier.from_config` 会跳过它。等你实现
完直接启用即可。

## 测试

```bash
make test
```

覆盖范围（共 33 个测试）：

- `test_parser.py`     — fping 输出解析
- `test_state_machine.py` — 状态机全部转换路径
- `test_database.py`   — SQLite 增删改查 + tags 持久化
- `test_notifier.py`   — 钉钉负载校验、加签、env 注入（基于 Mock）
- `test_scheduler.py`  — 端到端，注入伪检测器

## 架构总览

打开 `docs/architecture.html` 看模块分层、数据流、时序、状态机、扩展点
的可视化图。

## 常见问题

**Q：怎么改主机列表？**
A：直接编辑宿主机上的 `server.yaml`。容器会在下一个检测周期（约
`config.interval` 秒）自动从 YAML 重新同步。无需重启。

**Q：怎么改阈值或周期？**
A：编辑 `config.yaml`，重启容器生效（`docker compose restart monitor`）。
如果希望热加载，需要在 `Scheduler` 里订阅文件变更事件——目前未实现。

**Q：fping 输出格式变了怎么办？**
A：所有解析逻辑集中在 `util.py:parse_fping_output`，按需调整正则即可，
其他模块不受影响。
