"""
資料庫工廠（Factory）
─────────────────────────────────────────────────────────────────
根據 .env 的 DB_BACKEND 設定，動態選擇 SQLite 或 PostgreSQL。

使用方式（在 main.py）：
    from database import create_repository
    repo = create_repository()
    await repo.init()
    # 之後用 repo.save_sheet(...) 等方法，完全不需要管底層是誰

為什麼用工廠模式？
    1. 切換資料庫只要改一個環境變數，程式碼完全不用動
    2. import 延遲到真正需要時才做（lazy import）
       —— 例如本機用 SQLite 時，根本不會 import asyncpg，省記憶體
"""
from config import Config
from database.base import BaseRepository


def create_repository() -> BaseRepository:
    """
    根據環境變數選擇資料庫後端：
        DB_BACKEND=sqlite   → SQLiteRepository（本機測試 / 小型部署）
        DB_BACKEND=postgres → PostgresRepository（正式環境 / 高並發）

    回傳：BaseRepository 介面（呼叫端不需要知道具體實作）
    """
    if Config.DB_BACKEND == 'postgres' and Config.DATABASE_URL:
        # 延遲 import：只有真的要用 postgres 才會載入 asyncpg
        from database.postgres_repo import PostgresRepository
        print("🗄️  資料庫：PostgreSQL")
        return PostgresRepository(Config.DATABASE_URL)

    # 預設 fallback：SQLite
    from database.sqlite_repo import SQLiteRepository
    print(f"🗄️  資料庫：SQLite（檔案：{Config.SQLITE_PATH}）")
    return SQLiteRepository(Config.SQLITE_PATH)
