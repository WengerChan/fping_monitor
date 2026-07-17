FROM python:3.11-slim

# fping 是唯一的系统依赖
RUN apt-get update \
    && apt-get install -y --no-install-recommends fping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装 Python 依赖以利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY monitor.py scheduler.py detector.py notifier.py database.py models.py util.py ./
COPY sql/ ./sql/

# 运行时产物目录（data/state.db / logs 通过 volume 挂载）
RUN mkdir -p data logs

# Docker HEALTHCHECK：调用 healthcheck 子命令做两项检查（连 DB + fping 探活）
#   * start-period=30s  留给首次检测跑完
#   * interval=30s       与检测周期一致
#   * timeout=10s        fping + DB IO 上限
#   * retries=3          容忍偶发抖动
HEALTHCHECK --start-period=30s --interval=30s --timeout=10s --retries=3 \
    CMD ["python", "monitor.py", "healthcheck"]

# 容器默认就是常驻模式
CMD ["python", "monitor.py", "run"]
