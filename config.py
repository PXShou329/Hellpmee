"""
全域設定檔（v2.1 Clean Core Edition）
─────────────────────────────────────────────────────────────────
重要原則：
    ✅ 只有 DISCORD_TOKEN 必填
    ✅ 所有 AI Key、Places、CWA、Spotify、Redis 都「沒設定也能啟動」
    ✅ Utility AI 預設關閉（UTILITY_AI_ENABLED=false）
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes", "on")


class Config:
    # ════════════════════════════════════════════════════════
    #  Discord（必填）
    # ════════════════════════════════════════════════════════
    DISCORD_TOKEN: str = os.getenv('DISCORD_TOKEN', '')

    # ════════════════════════════════════════════════════════
    #  Optional Utility AI（選填，預設關閉）
    # ════════════════════════════════════════════════════════
    UTILITY_AI_ENABLED: bool = _bool('UTILITY_AI_ENABLED', 'false')
    UTILITY_AI_BACKEND: str  = os.getenv('UTILITY_AI_BACKEND', 'openai').lower()

    OPENAI_API_KEY: str = os.getenv('OPENAI_API_KEY', '')
    OPENAI_MODEL:   str = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')

    GEMINI_API_KEY: str = os.getenv('GEMINI_API_KEY', '')
    GEMINI_MODEL:   str = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')

    # 限額（每天最多呼叫 N 次）
    UTILITY_AI_DAILY_LIMIT:          int = int(os.getenv('UTILITY_AI_DAILY_LIMIT', '100'))
    UTILITY_AI_PER_USER_DAILY_LIMIT: int = int(os.getenv('UTILITY_AI_PER_USER_DAILY_LIMIT', '10'))

    # ════════════════════════════════════════════════════════
    #  Google Places（/吃什麼）
    # ════════════════════════════════════════════════════════
    GOOGLE_PLACES_API_KEY: str = os.getenv('GOOGLE_PLACES_API_KEY', '')

    # ════════════════════════════════════════════════════════
    #  CWA 中央氣象署（/天氣 補充）
    # ════════════════════════════════════════════════════════
    CWA_API_KEY: str = os.getenv('CWA_API_KEY', '')

    # ════════════════════════════════════════════════════════
    #  Spotify（/歌姬啦 metadata，選填）
    # ════════════════════════════════════════════════════════
    SPOTIFY_CLIENT_ID:     str = os.getenv('SPOTIFY_CLIENT_ID', '')
    SPOTIFY_CLIENT_SECRET: str = os.getenv('SPOTIFY_CLIENT_SECRET', '')

    # ════════════════════════════════════════════════════════
    #  資料庫
    # ════════════════════════════════════════════════════════
    DB_BACKEND:   str = os.getenv('DB_BACKEND', 'sqlite')
    SQLITE_PATH:  str = os.getenv('SQLITE_PATH', 'helpmee.db')
    DATABASE_URL: str = os.getenv('DATABASE_URL', '')

    # ════════════════════════════════════════════════════════
    #  Redis（選填）
    # ════════════════════════════════════════════════════════
    REDIS_URL:         str = os.getenv('REDIS_URL', '')
    MAX_CACHE_ENTRIES: int = int(os.getenv('MAX_CACHE_ENTRIES', '500'))

    # ════════════════════════════════════════════════════════
    #  /吃什麼
    # ════════════════════════════════════════════════════════
    FOOD_SEARCH_RADIUS:   int   = int(os.getenv('FOOD_SEARCH_RADIUS', '1000'))
    FOOD_MAX_RESULTS:     int   = int(os.getenv('FOOD_MAX_RESULTS', '5'))
    DEFAULT_LOCATION_LAT: float = float(os.getenv('DEFAULT_LOCATION_LAT', '25.0330'))
    DEFAULT_LOCATION_LNG: float = float(os.getenv('DEFAULT_LOCATION_LNG', '121.5654'))

    # ════════════════════════════════════════════════════════
    #  Debug（v2.2.0）：DEBUG_COMMANDS_ENABLED=true 才註冊 /黑優浦蜜 狀態
    # ════════════════════════════════════════════════════════
    DEBUG_COMMANDS_ENABLED: bool = _bool('DEBUG_COMMANDS_ENABLED', 'false')

    # ════════════════════════════════════════════════════════
    #  音樂
    # ════════════════════════════════════════════════════════
    FFMPEG_PATH:        str = os.getenv('FFMPEG_PATH', 'ffmpeg')
    MUSIC_QUEUE_MAX:    int = int(os.getenv('MUSIC_QUEUE_MAX', '100'))   # 佇列上限
    SPOTIFY_PLAYLIST_MAX: int = int(os.getenv('SPOTIFY_PLAYLIST_MAX', '25'))
