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

# 运行时产物目录（state.db / logs 通过 volume 挂载）
RUN mkdir -p logs

# 容器默认就是常驻模式
CMD ["python", "monitor.py"]
