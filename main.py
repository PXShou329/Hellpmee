"""
入口點（v2.1.3）
─────────────────────────────────────────────────────────────────
⭐ 啟動時就 inject_into_ssl，讓所有 SSL 連線改用系統信任的憑證
   這是規格 VIII 第二層保險（第一層是 weather_engine 用 certifi.where()）

只有 DISCORD_TOKEN 必填，其他全部選填。
"""
import asyncio

# ⭐ 必須在所有網路相關 import 之前注入
try:
    import truststore
    truststore.inject_into_ssl()
    print("✅ truststore 已注入（使用系統信任憑證）")
except ImportError:
    print("ℹ️  truststore 未安裝（pip install truststore 可解決部分 SSL 問題）")
except Exception as e:
    print(f"⚠️  truststore inject 失敗：{e}")

from config import Config
from core.music_engine import MusicEngine
from core.weather_engine import WeatherEngine
from core.food_engine import FoodEngine
from core.translate_engine import TranslateEngine
from core.utility_ai import UtilityAI
from database import create_repository
from database.redis_cache import RedisCache
from adapters.discord_adapter import create_discord_bot


async def main():
    if not Config.DISCORD_TOKEN:
        print("❌ 找不到 DISCORD_TOKEN，請確認 .env 設定！")
        return

    if not Config.GOOGLE_PLACES_API_KEY:
        print("ℹ️  GOOGLE_PLACES_API_KEY 未設定，/吃什麼 會回提示訊息")
    if not Config.CWA_API_KEY:
        print("ℹ️  CWA_API_KEY 未設定，/天氣 台灣地區會少 CWA 補充資訊")
    if not (Config.SPOTIFY_CLIENT_ID and Config.SPOTIFY_CLIENT_SECRET):
        print("ℹ️  Spotify 未設定，/歌姬啦 貼 Spotify 連結時會提示改用 YouTube")

    print("🚀 正在啟動黑優浦蜜（v2.1.3）...")

    cache = RedisCache()
    await cache.init()
    repo = create_repository()
    await repo.init()

    music     = MusicEngine()
    weather   = WeatherEngine(cwa_api_key=Config.CWA_API_KEY)
    food      = FoodEngine()
    translate = TranslateEngine()
    utility_ai = UtilityAI(cache=cache)

    bot = create_discord_bot(
        music=music, weather=weather, food=food,
        translate=translate, utility_ai=utility_ai,
        repo=repo, cache=cache,
    )

    try:
        await bot.start(Config.DISCORD_TOKEN)
    except KeyboardInterrupt:
        print("\n⏹️  收到中斷訊號，正在關閉...")
    finally:
        await bot.close()
        await repo.close()
        await cache.close()
        print("✅ 黑優浦蜜：已安全關閉")


if __name__ == "__main__":
    asyncio.run(main())
