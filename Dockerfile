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
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt \
    && pip install --no-cache-dir --prefix=/install "yt-dlp[default]"


# ── 正式階段 ────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# 系統套件：
#   ffmpeg       → 音樂播放
#   libpq5       → asyncpg
#   curl/unzip   → 安裝 Deno
#   ca-certificates → HTTPS 憑證（雲端 SSL）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libpq5 \
    curl \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ⭐ 安裝 Deno（給 yt-dlp 一個 JS runtime，改善 YouTube 簽名 / challenge 解析）
#    注意：Deno 解決的是「No supported JavaScript runtime」這類問題，
#    不能解決 YouTube 的 bot check（那要靠 player_client / cookies / 同曲 fallback）
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh \
    && deno --version

# 複製 Python 套件
COPY --from=builder /install /usr/local
ENV PATH=/usr/local/bin:$PATH

# 複製原始碼
COPY . .

# 建立資料目錄
RUN mkdir -p /app/data
ENV SQLITE_PATH=/app/data/helpmee.db

# 非 root 使用者（安全性）
RUN useradd -m helpmee && chown -R helpmee:helpmee /app
USER helpmee

CMD ["python", "main.py"]
