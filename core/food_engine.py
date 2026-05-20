"""
附近餐廳搜尋引擎（Google Places API v1）—— v2 核心新功能
─────────────────────────────────────────────────────────────────
這是 /吃什麼 指令的後台。

⚠️ 重要：使用「新版」Google Places API（v1）
    舊版：https://maps.googleapis.com/maps/api/place/textsearch/...（已過時）
    新版：https://places.googleapis.com/v1/places:searchText（本檔案使用）

新版差別：
    1. RESTful 設計，用 POST + JSON body
    2. 用 FieldMask header 指定要回傳哪些欄位（省流量，降低成本）
    3. priceLevel 改成字串列舉（PRICE_LEVEL_MODERATE）而不是 0-4 數字
    4. 結構更乾淨，回傳 places[] 陣列

費用：每 1000 次 Text Search 約 $17 USD
免費額度：每月 $200，等於約 11,700 次/月，個人使用絕對夠
"""
import httpx
from typing import Optional

from config import Config


# ════════════════════════════════════════════════════════════════
#  Google Places API 的價位代碼 → 整數對照表
#  新版 API 回傳的是字串列舉（為了未來擴展），我們轉成 0-4 整數方便處理
# ════════════════════════════════════════════════════════════════
_PRICE_LEVEL_MAP = {
    "PRICE_LEVEL_FREE":           0,
    "PRICE_LEVEL_INEXPENSIVE":    1,   # $
    "PRICE_LEVEL_MODERATE":       2,   # $$
    "PRICE_LEVEL_EXPENSIVE":      3,   # $$$
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,   # $$$$
}


class GooglePlacesNotConfiguredError(Exception):
    """沒設定 GOOGLE_PLACES_API_KEY"""


class GooglePlacesPermissionError(Exception):
    """Google Places 回 403 — 通常是 API 沒啟用或 key restrictions 不允許"""


class FoodEngine:
    """
    附近餐廳搜尋引擎。
    """

    # ── Google Places API v1 endpoint ────────────────────────
    _PLACES_URL  = "https://places.googleapis.com/v1/places:searchText"

    # ── Open-Meteo geocoding（順便用，免費的）─────────────────
    # 用來把「台北車站」這種文字地點轉成 (lat, lng)
    _GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

    # FieldMask 指定要回傳的欄位 —— 越少越省錢
    # 完整欄位列表：https://developers.google.com/maps/documentation/places/web-service/text-search
    _FIELD_MASK = (
        "places.displayName,"
        "places.formattedAddress,"
        "places.rating,"
        "places.userRatingCount,"
        "places.priceLevel,"
        "places.currentOpeningHours,"
        "places.id"
    )

    # ════════════════════════════════════════════════════════
    #  主要方法：搜尋附近餐廳
    # ════════════════════════════════════════════════════════

    async def search_nearby(self, food_keyword: str,
                            lat: float, lng: float,
                            radius: Optional[int] = None) -> list[dict]:
        """
        在指定座標的附近搜尋餐廳（v2.1.9 強化）。

        - radius 由 caller 給，clamp 到 100~5000
        - 403 → 拋 GooglePlacesPermissionError 讓 caller 顯示 Google Maps fallback
        - debug log 不洩漏完整 key
        """
        if not Config.GOOGLE_PLACES_API_KEY:
            raise GooglePlacesNotConfiguredError(
                "GOOGLE_PLACES_API_KEY 未設定"
            )

        radius = radius or Config.FOOD_SEARCH_RADIUS
        radius = max(100, min(int(radius), 10000))

        headers = {
            "Content-Type":     "application/json",
            "X-Goog-Api-Key":   Config.GOOGLE_PLACES_API_KEY,
            "X-Goog-FieldMask": self._FIELD_MASK,
        }
        body = {
            "textQuery": food_keyword,
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": float(radius),
                }
            },
            "maxResultCount": Config.FOOD_MAX_RESULTS,
            "languageCode":   "zh-TW",
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.post(self._PLACES_URL, headers=headers, json=body)
                if res.status_code == 403:
                    self._log_403(res, food_keyword, lat, lng, radius)
                    raise GooglePlacesPermissionError(
                        "Google Places 403 PERMISSION_DENIED"
                    )
                res.raise_for_status()
                data = res.json()
        except GooglePlacesPermissionError:
            raise
        except httpx.HTTPStatusError as e:
            print(f"[Food] Places API HTTP {e.response.status_code}: {e.response.text[:200]}")
            return []
        except Exception as e:
            print(f"[Food] Places API 呼叫失敗：{e}")
            return []

        places = data.get("places", [])
        print(f"[Food] Places API OK: query={food_keyword!r} radius={radius}m → {len(places)} 筆")
        return [self._parse_place(p) for p in places]

    def _log_403(self, res, query, lat, lng, radius):
        """403 完整 debug log（不洩漏完整 key）"""
        key = Config.GOOGLE_PLACES_API_KEY or ""
        key_prefix = key[:6] + "..." + key[-4:] if len(key) > 12 else "(too short)"
        print(f"""
[Food] ❌ Google Places 403 PERMISSION_DENIED
  endpoint:          {self._PLACES_URL}
  api_mode:          new (places.googleapis.com/v1)
  has_key:           {bool(key)}
  key_prefix:        {key_prefix}
  query:             {query!r}
  center:            ({lat}, {lng})
  radius_m:          {radius}
  response_status:   {res.status_code}
  response_body:     {res.text[:400]}

  enabled_hint:               GCP 需啟用「Places API (New)」（不是舊版 Places API）
  application_restriction_hint: API key 若有 IP / referrer 限制，目前環境可能不允許
  billing_hint:               GCP project 需綁定有效 billing account
""")


    # ════════════════════════════════════════════════════════
    #  Geocoding：城市名 → 座標
    # ════════════════════════════════════════════════════════

    async def geocode(self, city_name: str) -> Optional[tuple[float, float]]:
        """
        把「台北車站」這種文字地點轉成 (lat, lng)。
        失敗回傳 None。

        用 Open-Meteo 的 geocoding（免費、不需要 Key）
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                res = await client.get(self._GEOCODE_URL, params={
                    "name": city_name, "count": 1,
                    "language": "zh", "format": "json"
                })
                data = res.json()
            if not data.get("results"):
                return None
            r = data["results"][0]
            return (r["latitude"], r["longitude"])
        except Exception as e:
            print(f"[Food] Geocoding 失敗：{e}")
            return None

    # ════════════════════════════════════════════════════════
    #  輔助方法
    # ════════════════════════════════════════════════════════

    def build_google_maps_url(self, place_id: str) -> str:
        """
        產生點擊就會跳轉 Google Maps 的網址。
        Format：https://www.google.com/maps/place/?q=place_id:{place_id}
        """
        return f"https://www.google.com/maps/place/?q=place_id:{place_id}"

    def format_price_level(self, level: Optional[int]) -> str:
        """
        把 0-4 的整數轉成顯示用字串。
            0    → '免費'
            1    → '$'
            2    → '$$'
            3    → '$$$'
            4    → '$$$$'
            None → '不明'
        """
        if level is None:
            return "不明"
        symbols = ["免費", "$", "$$", "$$$", "$$$$"]
        if 0 <= level <= 4:
            return symbols[level]
        return "不明"

    def format_result_embed(self, results: list[dict], food_keyword: str) -> dict:
        """
        把搜尋結果整理成方便 Discord Embed 顯示的格式。
        回傳：
            {
                'title':       str,
                'description': str,   # 一段簡單說明
                'fields': [           # 每間店一個 field
                    {'name': str, 'value': str, 'inline': False},
                    ...
                ],
            }

        ⚠️ 注意：實際的 Embed 物件由 Discord adapter 建立，
                這裡只負責「準備資料」，保持 core 跟 discord 解耦。
        """
        title = f"🍜 牢大想吃「{food_keyword}」？讓本喵幫你找！"

        if not results:
            return {
                'title':       title,
                'description': f"本喵翻遍附近都沒找到「{food_keyword}」喵...換個關鍵字試試？♡",
                'fields':      [],
            }

        fields = []
        for i, r in enumerate(results, 1):
            # 評分顯示
            rating = r.get('rating', 0)
            rating_str = f"⭐ {rating}" if rating else "⭐ 暫無評分"
            if r.get('total_ratings'):
                rating_str += f" ({r['total_ratings']:,} 則評論)"

            # 價位
            price_str = f"💰 {self.format_price_level(r.get('price_level'))}"

            # 營業狀態
            opening = r.get('opening_now')
            if opening is True:
                open_str = "🟢 營業中"
            elif opening is False:
                open_str = "🔴 已休息"
            else:
                open_str = "❓ 營業狀態不明"

            # 地址
            address = r.get('address', '地址未知')

            # Google Maps 連結
            maps_url = r.get('google_maps_url', '')

            field_value = (
                f"{rating_str}　{price_str}　{open_str}\n"
                f"📍 {address}\n"
                f"🗺️ [在 Google Maps 開啟]({maps_url})"
            )

            fields.append({
                'name':   f"{i}. {r['name']}",
                'value':  field_value,
                'inline': False,
            })

        return {
            'title':       title,
            'description': f"找到 **{len(results)}** 間附近的「{food_keyword}」店家，挑一間吧喵♡",
            'fields':      fields,
        }

    # ════════════════════════════════════════════════════════
    #  內部：API 回應解析
    # ════════════════════════════════════════════════════════

    def _parse_place(self, place: dict) -> dict:
        """
        把 Google Places API 的原始回應，轉成我們的標準 dict 格式。
        """
        # priceLevel 是字串列舉，轉成 0-4 整數
        price_str = place.get("priceLevel")
        price_int = _PRICE_LEVEL_MAP.get(price_str) if price_str else None

        # 營業時間：currentOpeningHours.openNow（True/False/None）
        opening = place.get("currentOpeningHours") or {}
        open_now = opening.get("openNow")

        place_id = place.get("id", "")

        return {
            'name':            place.get("displayName", {}).get("text", "未知店家"),
            'address':         place.get("formattedAddress", ""),
            'rating':          place.get("rating", 0),
            'total_ratings':   place.get("userRatingCount", 0),
            'price_level':     price_int,
            'opening_now':     open_now,
            'place_id':        place_id,
            'google_maps_url': self.build_google_maps_url(place_id),
        }
