# FPing Monitor 开发需求（给 Codex）

## 目标

开发一个基于 fping 的轻量级服务器存活监控系统。

### 技术栈

-   Python 3.11+
-   fping
-   SQLite
-   PyYAML
-   requests

### 目录

    fping-monitor/
    ├── monitor.py
    ├── scheduler.py
    ├── detector.py
    ├── notifier.py
    ├── database.py
    ├── models.py
    ├── util.py
    ├── config.yaml
    ├── server.yaml
    ├── state.db
    ├── logs/
    ├── notify/
    └── tests/

### 配置

-   interval: 30 秒
-   failure_threshold: 3
-   recovery_threshold: 2
-   SQLite 保存状态
-   YAML 保存配置

### 数据库

hosts: - name - ip - status(UNKNOWN/UP/DOWN) - fail_count -
recover_count - last_check - last_change

events: - host_id - event(DOWN/RECOVER) - time - message

### 检测

-   使用一次 fping 批量检测所有主机
-   禁止逐台 ping
-   解析输出更新数据库

### 状态机

UNKNOWN→UP：不报警 UNKNOWN→DOWN：不报警 UP→DOWN：告警 DOWN→UP：恢复通知

### 防抖

-   连续失败 failure_threshold 次才判定 DOWN
-   连续成功 recovery_threshold 次才恢复

### 告警

统一接口： - notify_down(host) - notify_recover(host)

支持企业微信 Webhook，并预留钉钉、Slack、邮件扩展。

### 日志

使用 logging，按天滚动，不使用 print。

### 调度

提供 run_once() 与 main()，方便 systemd timer 或 cron 调用，不使用 while
True 死循环。

### 测试

覆盖： - fping 输出解析 - 状态机 - SQLite - Webhook(Mock)

### 交付

-   完整源码
-   README
-   requirements.txt
-   初始化 SQL
-   Dockerfile
-   docker-compose.yml
-   systemd service/timer
-   Makefile
-   GitHub Actions(pytest)

### 架构

Detector → StateMachine → SQLite → Notifier → Scheduler
要求模块解耦，后续可扩展 TCP、HTTP、SSH 检测而无需修改核心逻辑。
