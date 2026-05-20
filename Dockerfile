# ── 建置階段 ────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# 安裝系統依賴（ffmpeg 給音樂用、gcc/libpq 給 asyncpg）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# ── 正式階段 ────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# 複製系統套件（ffmpeg 等）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# 複製 Python 套件
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# 複製原始碼
COPY . .

# 建立資料目錄
RUN mkdir -p /app/data
ENV SQLITE_PATH=/app/data/helpmee.db

# 非 root 使用者（安全性）
RUN useradd -m helpmee && chown -R helpmee:helpmee /app
USER helpmee

CMD ["python", "main.py"]
