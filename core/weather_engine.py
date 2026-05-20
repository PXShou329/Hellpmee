"""
天氣引擎（v2.1.6：中英日地名 + 多候選 select menu + Utility AI 正規化）
─────────────────────────────────────────────────────────────────
查詢流程（規格 一.3）：
    1. 原始輸入直接查本地 mapping（taiwan_cities / japan_cities / international_cities）
    2. 命中台灣縣市 → CWA
    3. 命中國外 → Open-Meteo geocoding（用本地 mapping 標準化後的字串去查）
    4. 沒命中 → 嘗試 Utility AI 把地名正規化成「City, Country」
    5. 拿正規化結果再丟 Open-Meteo geocoding
    6. 都沒結果才提示找不到

Geocoding 多候選（規格 三 + 答案 3）：
    Open-Meteo geocoding 拉 count=5
    >= 2 候選 → 回傳所有候選讓 adapter 顯示 select menu
    = 1 候選   → 直接查
    = 0 候選   → 提示找不到

對外 API：
    resolve_location(query, ai=None)  → 回 LocationResolution
    fetch_taiwan_weather(cwa_city)    → CWA 預報文字
    fetch_overseas_weather(lat, lng, city, country) → Open-Meteo 預報文字
"""
import json
import os
from dataclasses import dataclass, field
from typing import Optional

import certifi
import httpx

# OpenCC 簡轉繁（規格 I.5）：失敗時 fallback 到原文
try:
    from opencc import OpenCC
    _s2t = OpenCC("s2twp")    # 簡 → 繁（台灣慣用）
    def s2tw(text: str) -> str:
        try:
            return _s2t.convert(text)
        except Exception:
            return text
    print("[weather] OpenCC 簡轉繁就緒")
except Exception as e:
    print(f"[weather] OpenCC 載入失敗，簡轉繁將跳過：{e}")
    def s2tw(text: str) -> str:
        return text


_ALIAS_PATH = os.path.join(os.path.dirname(__file__), "data", "location_aliases.json")


def _load_aliases() -> tuple[dict, dict, dict, dict]:
    """讀 location_aliases.json → 四個 dict"""
    try:
        with open(_ALIAS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[weather] location_aliases.json 讀取失敗：{e}")
        return {}, {}, {}, {}

    def _clean(d):
        return {k: v for k, v in d.items() if not k.startswith("_")}

    return (
        _clean(data.get("taiwan_cities", {})),
        _clean(data.get("japan_cities", {})),
        _clean(data.get("international_cities", {})),
        _clean(data.get("country_aliases", {})),
    )


_TAIWAN_ALIASES, _JAPAN_ALIASES, _INTL_ALIASES, _COUNTRY_ALIASES = _load_aliases()

# ════════════════════════════════════════════════════════════════
#  區名 → (縣市, 區全名)（v2.3.1：保留區級 display_name）
#  CWA 目前只有縣市級預報，所以 forecast_location 用縣市，但顯示保留區。
# ════════════════════════════════════════════════════════════════
def _build_district_detail() -> dict:
    raw = {
        "臺北市": ["北投", "士林", "內湖", "南港", "松山", "信義", "大安",
                  "中山", "中正", "大同", "萬華", "文山"],
        "新北市": ["板橋", "三重", "中和", "永和", "新莊", "新店", "土城",
                  "蘆洲", "汐止", "樹林", "淡水", "三峽", "鶯歌", "林口"],
        "高雄市": ["左營", "三民", "苓雅", "前鎮", "鼓山", "楠梓", "鳳山",
                  "小港", "前金", "鹽埕"],
        "臺中市": ["西屯", "北屯", "南屯", "豐原", "大里", "太平"],
    }
    out = {}
    for city, districts in raw.items():
        for d in districts:
            out[d] = (city, f"{d}區")
    return out

_DISTRICT_DETAIL = _build_district_detail()


# ════════════════════════════════════════════════════════════════
#  資料類別
# ════════════════════════════════════════════════════════════════
@dataclass
class GeoCandidate:
    """Open-Meteo geocoding 回傳的單筆候選"""
    name:        str
    country:     str
    admin1:      str = ""    # 行政區一級（州 / 道府縣）
    latitude:    float = 0.0
    longitude:   float = 0.0

    def display(self) -> str:
        parts = [self.name]
        if self.admin1 and self.admin1 != self.name:
            parts.append(self.admin1)
        if self.country:
            parts.append(self.country)
        return ", ".join(parts)


@dataclass
class LocationResolution:
    """
    resolve_location() 的結果。

    is_taiwan=True  → 用 cwa_city 去查 CWA
    is_taiwan=False:
      candidates=[]     → 沒找到，使用者要換寫法
      candidates=[1]    → 直接查那一個
      candidates=[2+]   → 讓使用者選
    """
    is_taiwan:  bool = False
    cwa_city:   Optional[str] = None
    candidates: list[GeoCandidate] = field(default_factory=list)
    # ⭐ v2.3.1：台灣區級顯示
    display_name:      Optional[str] = None   # 給使用者看：「臺北市 北投區，台灣」
    forecast_location: Optional[str] = None   # 拿去查 CWA：「臺北市」
    district:          Optional[str] = None   # 「北投區」（可能 None）
    city:              Optional[str] = None   # 「臺北市」
    forecast_level:    str = "city"           # 目前都是縣市級
    # debug
    resolved_via: str = ""    # 'local_alias_tw' / 'local_alias_intl' / 'ai_normalize' / 'raw_geocoding'


# ════════════════════════════════════════════════════════════════
#  天氣描述對照（emoji）
# ════════════════════════════════════════════════════════════════
def cwa_wx_emoji(wx: str) -> str:
    if "雷" in wx: return "⛈️"
    if "雨" in wx: return "🌧️"
    if "雪" in wx: return "🌨️"
    if "霧" in wx: return "🌫️"
    if "陰" in wx: return "☁️"
    if "雲" in wx and "晴" in wx: return "🌤️"
    if "雲" in wx: return "🌥️"
    if "晴" in wx: return "☀️"
    return "🌈"


def wmo_emoji(code: int) -> str:
    if code == 0:           return "☀️"
    if code in (1, 2):      return "🌤️"
    if code == 3:           return "☁️"
    if 45 <= code < 50:     return "🌫️"
    if 51 <= code < 68:     return "🌧️"
    if 71 <= code < 78:     return "🌨️"
    if 80 <= code < 83:     return "🌦️"
    if 95 <= code < 100:    return "⛈️"
    return "🌈"


def wmo_desc(code: int) -> str:
    if code == 0:           return "晴天"
    if code in (1, 2):      return "多雲時晴"
    if code == 3:           return "陰天"
    if 45 <= code < 50:     return "有霧"
    if 51 <= code < 58:     return "毛毛雨"
    if 61 <= code < 68:     return "降雨"
    if 71 <= code < 78:     return "下雪"
    if 80 <= code < 83:     return "陣雨"
    if 95 <= code < 100:    return "雷雨"
    return "天氣多變"


# ════════════════════════════════════════════════════════════════
#  地名解析器
# ════════════════════════════════════════════════════════════════
class WeatherEngine:

    _CWA_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001"
    _GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
    _OM_URL  = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, cwa_api_key: str = ""):
        self.cwa_api_key = cwa_api_key

    # ════════════════════════════════════════════════════════
    #  ⭐ 主要入口：地名解析（規格 一.3 多層 fallback）
    # ════════════════════════════════════════════════════════
    async def resolve_location(self, query: str, ai=None) -> LocationResolution:
        """
        統一地名解析（v2.1.8 完整 fallback 流程）：

        1. 原始輸入清理 + suffix 正規化
        2. 本地 alias 整串 match（台灣 / 日本 / 國際）
        3. 「國家 + 城市」格式拆解
        4. Utility AI 多候選 query（如果有）
        5. 對每個 candidate 依序丟 geocoding
        6. 都失敗 → 用原始輸入再試一次 geocoding
        7. 還是失敗 → 找不到
        """
        q_raw = query.strip()
        if not q_raw:
            return LocationResolution()

        # ── Layer 0: 清理 + suffix 正規化 ──
        cleaned_variants = self._clean_and_normalize(q_raw)
        print(f"[weather] cleaned variants for {q_raw!r}: {cleaned_variants[:4]}{'...' if len(cleaned_variants) > 4 else ''}")

        # ── Layer 1: 本地 alias 直接命中（試每個 variant）──
        for variant in cleaned_variants:
            detail = self._match_taiwan_detail(variant)
            if detail:
                city, district = detail
                return self._make_tw_resolution(city, district,
                                                 via=f"local_alias_tw:{variant}")
            normalized = self._match_international(variant)
            if normalized:
                print(f"[weather] '{variant}' → 本地 mapping → '{normalized}'")
                cands = await self._geocode(normalized)
                if cands:
                    return LocationResolution(
                        is_taiwan=False, candidates=cands,
                        resolved_via=f"local_alias_intl:{variant}",
                    )

        # ── Layer 2: 「國家 + 城市」拆解 ──
        for variant in cleaned_variants:
            cc_result = self._try_country_city_split(variant)
            if cc_result:
                kind, value = cc_result
                print(f"[weather] '{variant}' → 國家+城市拆解 → {kind}={value!r}")
                if kind == "taiwan":
                    detail = self._match_taiwan_detail(value) or (value, None)
                    return self._make_tw_resolution(detail[0], detail[1],
                                                     via="country_city_split_tw")
                cands = await self._geocode(value)
                if cands:
                    return LocationResolution(
                        is_taiwan=False, candidates=cands,
                        resolved_via="country_city_split",
                    )

        # ── Layer 3: Utility AI 多候選 query ──
        ai_candidates: list[str] = []
        if ai is not None and ai.is_available():
            ai_candidates = await self._ai_normalize_candidates(q_raw, ai)
            if ai_candidates:
                print(f"[weather] AI 產生候選 query: {ai_candidates}")
                for cand_query in ai_candidates:
                    # AI 候選裡可能是台灣
                    detail = self._match_taiwan_detail(cand_query)
                    if detail:
                        return self._make_tw_resolution(detail[0], detail[1],
                                                         via=f"ai_to_tw:{cand_query}")
                    cands = await self._geocode(cand_query)
                    if cands:
                        return LocationResolution(
                            is_taiwan=False, candidates=cands,
                            resolved_via=f"ai_candidate:{cand_query}",
                        )

        # ── Layer 4: 對每個 cleaned variant 都 raw geocoding ──
        for variant in cleaned_variants:
            print(f"[weather] '{variant}' → raw geocoding fallback")
            cands = await self._geocode(variant)
            if cands:
                return LocationResolution(
                    is_taiwan=False, candidates=cands,
                    resolved_via=f"raw_geocoding:{variant}",
                )

        return LocationResolution()

    # ── 輸入清理 + suffix 正規化（規格 IV）──
    def _clean_and_normalize(self, q: str) -> list[str]:
        """
        產生多個 query variant 嘗試。
        例如 '青森縣' → ['青森縣', '青森', '青森県', '青森市']
        例如 '日本 青森縣' → ['日本 青森縣', '青森', '青森縣', '青森県', ...]
        """
        out = []
        # 原樣保留
        out.append(q.strip())

        # 拆 tokens（空格分隔）
        tokens = q.split()
        # 移除國家詞，剩下的當作純地名
        non_country_tokens = []
        for t in tokens:
            if t not in _COUNTRY_ALIASES:
                non_country_tokens.append(t)
        if non_country_tokens and len(non_country_tokens) != len(tokens):
            out.append(" ".join(non_country_tokens))

        # suffix 正規化：每個 token 都試移除常見 suffix
        # 中文：縣、市、區、鄉、鎮、州、省
        # 日文：県、都、府、市、区、町、村
        # 英文：Prefecture, City, District, County, State
        suffixes_zh_jp = ("縣", "県", "都", "府", "市", "区", "區", "町", "村", "鄉", "鎮")
        suffixes_en = (" Prefecture", " City", " District", " County", " State")

        for variant in list(out):
            stripped = variant
            for sfx in suffixes_zh_jp:
                if stripped.endswith(sfx) and len(stripped) > len(sfx):
                    new_v = stripped[:-len(sfx)]
                    if new_v not in out:
                        out.append(new_v)
            for sfx in suffixes_en:
                if stripped.endswith(sfx):
                    new_v = stripped[:-len(sfx)]
                    if new_v not in out:
                        out.append(new_v)

        # 對每個 token 也單獨去 suffix（處理「日本 青森縣」這種）
        for t in non_country_tokens:
            for sfx in suffixes_zh_jp:
                if t.endswith(sfx) and len(t) > len(sfx):
                    new_t = t[:-len(sfx)]
                    if new_t not in out:
                        out.append(new_t)
            for sfx in suffixes_en:
                if t.endswith(sfx):
                    new_t = t[:-len(sfx)]
                    if new_t not in out:
                        out.append(new_t)

        return out

    # ── AI 多候選 query 產生（規格 VI）──
    async def _ai_normalize_candidates(self, query: str, ai) -> list[str]:
        """
        新版 AI 介面：回傳 1-5 個候選 query 字串（list）
        """
        try:
            if hasattr(ai, "normalize_location_candidates"):
                return await ai.normalize_location_candidates(query, user_id=0) or []
            # 舊版 fallback：單一字串
            if hasattr(ai, "normalize_location"):
                single = await ai.normalize_location(query, user_id=0)
                return [single] if single else []
        except Exception as e:
            print(f"[weather] AI 正規化失敗：{e}")
        return []

    # 舊 API 保留給其他人用（沒人用就刪）
    async def _ai_normalize(self, query: str, ai) -> Optional[str]:
        cands = await self._ai_normalize_candidates(query, ai)
        return cands[0] if cands else None

    # ── 「國家 + 城市」拆分解析（規格 I.2）──
    def _try_country_city_split(self, q: str) -> Optional[tuple[str, str]]:
        """
        嘗試把「國家 + 城市」拆解。
        回傳 (kind, value):
            kind="taiwan", value=CWA city name
            kind="overseas", value="City, Country" geocoding query
            None = 不是這格式
        """
        tokens = q.split()
        if len(tokens) < 2:
            return None

        # 兩個 token 一組嘗試：A=國家+B=城市 或 A=城市+B=國家
        for a, b in [(tokens[0], " ".join(tokens[1:])),
                     (" ".join(tokens[:-1]), tokens[-1])]:
            # 情境 1：a 是國家、b 是城市
            country_en_a = _COUNTRY_ALIASES.get(a)
            # 情境 2：b 是國家、a 是城市
            country_en_b = _COUNTRY_ALIASES.get(b)

            if country_en_a:
                # a=國家 / b=城市
                if country_en_a == "Taiwan":
                    tw = self._match_taiwan(b)
                    if tw: return ("taiwan", tw)
                # 城市可能也是本地 mapping
                norm_b = self._match_international(b)
                if norm_b: return ("overseas", norm_b)
                return ("overseas", f"{b}, {country_en_a}")

            if country_en_b:
                # b=國家 / a=城市
                if country_en_b == "Taiwan":
                    tw = self._match_taiwan(a)
                    if tw: return ("taiwan", tw)
                norm_a = self._match_international(a)
                if norm_a: return ("overseas", norm_a)
                return ("overseas", f"{a}, {country_en_b}")

        return None

    # ── 建立台灣解析結果（含 display_name）──
    @staticmethod
    def _make_tw_resolution(city: str, district: Optional[str],
                            via: str) -> "LocationResolution":
        if district:
            display = f"{city} {district}，台灣"
        else:
            display = f"{city}，台灣"
        return LocationResolution(
            is_taiwan=True,
            cwa_city=city,            # 向後相容
            forecast_location=city,   # 查 CWA 用
            display_name=display,     # 顯示用
            district=district,
            city=city,
            forecast_level="city",
            resolved_via=via,
        )

    # ── 本地 mapping 比對 ──
    @staticmethod
    def _match_taiwan(q: str) -> Optional[str]:
        if q in _TAIWAN_ALIASES:
            return _TAIWAN_ALIASES[q]
        # 台↔臺 容錯
        q2 = q.replace("台", "臺")
        if q2 in _TAIWAN_ALIASES:
            return _TAIWAN_ALIASES[q2]
        q3 = q.replace("臺", "台")
        if q3 in _TAIWAN_ALIASES:
            return _TAIWAN_ALIASES[q3]

        # 複合詞：如「臺北北投」「高雄左營」→ 找尾端區名
        # 對每個已知 alias key，看 q 是否「以它結尾」或「包含它」
        for variant in (q, q2, q3):
            # 先試結尾比對（更精確）
            for key, city in _TAIWAN_ALIASES.items():
                if len(key) >= 2 and variant.endswith(key):
                    return city
            # 再試包含
            for key, city in _TAIWAN_ALIASES.items():
                if len(key) >= 2 and key in variant:
                    return city
        return None

    @staticmethod
    def _match_taiwan_detail(q: str) -> Optional[tuple]:
        """
        v2.3.1：回傳 (city, district)。
        - 命中區級 → ('臺北市', '北投區')
        - 命中縣市級 → ('臺北市', None)
        - 沒命中 → None
        """
        # 先試區級：把輸入正規化成臺，逐個區名核心比對
        for variant in (q, q.replace("台", "臺"), q.replace("臺", "台")):
            v = variant.strip()
            for core, (city, district) in _DISTRICT_DETAIL.items():
                # 完整命中（北投 / 北投區 / 臺北市北投區 / 臺北北投…）
                if v == core or v == district or v.endswith(core) or v.endswith(district):
                    return (city, district)
                if core in v and (city.replace("臺", "台") in v or city in v or len(v) <= len(district) + 3):
                    return (city, district)
        # 退而求其次：縣市級
        city = WeatherEngine._match_taiwan(q)
        if city:
            return (city, None)
        return None

    @staticmethod
    def _match_international(q: str) -> Optional[str]:
        if q in _JAPAN_ALIASES:
            return _JAPAN_ALIASES[q]
        if q in _INTL_ALIASES:
            return _INTL_ALIASES[q]
        return None

    # ── Open-Meteo geocoding ──
    async def _geocode(self, query: str) -> list[GeoCandidate]:
        """回傳 0~5 個候選"""
        try:
            async with httpx.AsyncClient(
                timeout=10, verify=certifi.where(),
            ) as client:
                res = await client.get(self._GEO_URL, params={
                    "name": query, "count": 5,
                    "language": "zh", "format": "json",
                })
                data = res.json()
        except Exception as e:
            print(f"[weather] geocoding 失敗 query={query!r}: {e}")
            return []

        results = data.get("results") or []
        cands = []
        for r in results:
            country = r.get("country", "")
            admin1  = s2tw(r.get("admin1", "") or "")
            # 規格：台灣地名絕不能標成中國
            if country in ("Taiwan", "臺灣", "台灣", "Republic of China"):
                country = "台灣"
            else:
                country = s2tw(country)
            # ⭐ v2.2.0 規格一.2：禁止顯示「臺灣省 / 台灣省」
            for bad in ("臺灣省", "台灣省", "台湾省", "臺灣", "台灣"):
                if admin1 == bad:
                    admin1 = ""   # 省級資訊無意義，清掉
            cands.append(GeoCandidate(
                name=s2tw(r.get("name", query)),
                country=country,
                admin1=admin1,
                latitude=r.get("latitude", 0.0),
                longitude=r.get("longitude", 0.0),
            ))
        return cands

    # ════════════════════════════════════════════════════════
    #  CWA 台灣天氣
    # ════════════════════════════════════════════════════════
    async def fetch_taiwan_weather(self, cwa_city: str,
                                   display_name: Optional[str] = None,
                                   district: Optional[str] = None) -> str:
        """
        cwa_city     : 拿去查 CWA 的縣市名（forecast_location）
        display_name : 給使用者看的標題（含區）。None 則用 cwa_city。
        district     : 若有，且 CWA 只有縣市級 → 補一句「目前使用 X 整體預報」
        """
        title = display_name or f"{cwa_city}，台灣"
        header = f"☀️ 天氣查詢：{title}"

        if not self.cwa_api_key:
            return (
                f"{header}\n\n"
                f"出錯了喵...\n本喵需要 CWA 授權碼才能查台灣天氣。\n"
                f"請在 `.env` 設定 `CWA_API_KEY` 後再試。\n"
                f"申請網址：https://opendata.cwa.gov.tw/"
            )

        try:
            data = await self._fetch_cwa_raw(cwa_city)
        except Exception as e:
            return (
                f"{header}\n\n"
                f"出錯了喵...\n中央氣象署回應異常（{str(e)[:50]}）。\n"
                f"牢大稍後再試一次喵。"
            )

        slots = self._parse_cwa_slots(data)
        if not slots:
            return (
                f"{header}\n\n"
                f"出錯了喵...\nCWA 沒有回傳預報資料。"
            )

        lines = [header]
        # ⭐ v2.3.1：區級輸入但 CWA 只有縣市級 → 誠實標示
        if district:
            lines.append(f"_資料來源：中央氣象署縣市級預報（目前使用{cwa_city}整體預報）_")
        lines.append("")
        for s in slots:
            lines.append(f"**{s['label']}**　{s['start']} ~ {s['end']}")
            lines.append(f"{s['emoji']} {s['wx']}")
            lines.append(f"溫度：{s['min_t']}°C ～ {s['max_t']}°C")
            lines.append(f"降雨機率：{s['pop']}%")
            lines.append(f"體感：{s['ci']}")
            lines.append("")
        lines.append(self._taiwan_friendly_tip(slots))
        return "\n".join(lines)

    async def _fetch_cwa_raw(self, city: str) -> dict:
        async with httpx.AsyncClient(timeout=10, verify=certifi.where()) as client:
            res = await client.get(self._CWA_URL, params={
                "Authorization": self.cwa_api_key,
                "locationName":  city,
            })
            res.raise_for_status()
            return res.json()

    def _parse_cwa_slots(self, data: dict) -> list[dict]:
        records   = data.get("records", {})
        locations = records.get("location") or []
        if not locations:
            return []
        elements = {e["elementName"]: e["time"] for e in locations[0].get("weatherElement", [])}
        wx_list  = elements.get("Wx",   [])
        pop_list = elements.get("PoP",  [])
        min_list = elements.get("MinT", [])
        max_list = elements.get("MaxT", [])
        ci_list  = elements.get("CI",   [])
        out = []
        for i in range(min(3, len(wx_list))):
            try:
                start = wx_list[i]["startTime"]
                end   = wx_list[i]["endTime"]
                wx    = wx_list[i]["parameter"]["parameterName"]
                pop   = pop_list[i]["parameter"]["parameterName"] if i < len(pop_list) else "?"
                tmin  = min_list[i]["parameter"]["parameterName"] if i < len(min_list) else "?"
                tmax  = max_list[i]["parameter"]["parameterName"] if i < len(max_list) else "?"
                ci    = ci_list[i]["parameter"]["parameterName"]  if i < len(ci_list)  else "?"
                start_short = start[5:16] if len(start) >= 16 else start
                end_short   = end[5:16]   if len(end)   >= 16 else end
                label = self._slot_label(start)
                out.append({
                    'label':  label,
                    'start':  start_short, 'end': end_short,
                    'wx':     wx, 'emoji': cwa_wx_emoji(wx),
                    'pop':    pop, 'min_t': tmin, 'max_t': tmax, 'ci': ci,
                })
            except (KeyError, IndexError):
                continue
        return out

    @staticmethod
    def _slot_label(start_time: str) -> str:
        """
        依 CWA startTime 產生時段名（v2.3.1）。
        今天/明天/後天 + 白天/晚上。不再用「今晚」簡寫。
        start_time 例：'2026-05-21 06:00:00'
        """
        from datetime import datetime, date
        try:
            dt = datetime.strptime(start_time[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                dt = datetime.fromisoformat(start_time[:19])
            except Exception:
                return "時段"
        today = date.today()
        day_diff = (dt.date() - today).days
        day_word = {0: "今天", 1: "明天", 2: "後天"}.get(day_diff,
                    dt.strftime("%m/%d"))
        # 白天 06:00~18:00，其餘晚上
        period = "白天" if 6 <= dt.hour < 18 else "晚上"
        return f"{day_word}{period}"

    def _taiwan_friendly_tip(self, slots: list[dict]) -> str:
        try:
            max_temp = max(int(s['max_t']) for s in slots if s['max_t'].isdigit())
            max_pop  = max(int(s['pop'])   for s in slots if s['pop'].isdigit())
        except (ValueError, TypeError):
            return "黑優浦蜜提醒：牢大記得看狀況穿衣服喵♡"
        if max_pop >= 60:  return "黑優浦蜜提醒：牢大記得帶傘喵，會下雨。"
        if max_temp >= 32: return "黑優浦蜜提醒：牢大今天偏熱，記得多補水喵♡"
        if max_temp <= 15: return "黑優浦蜜提醒：牢大今天偏冷，多穿一件再出門喵♡"
        return "黑優浦蜜提醒：今天天氣還算舒服喵～"

    # ════════════════════════════════════════════════════════
    #  Open-Meteo 國外天氣
    # ════════════════════════════════════════════════════════
    async def fetch_overseas_weather(self, cand: GeoCandidate) -> str:
        header = f"☀️ 天氣查詢：{cand.display()}"
        try:
            async with httpx.AsyncClient(timeout=10, verify=certifi.where()) as client:
                w_res = await client.get(self._OM_URL, params={
                    "latitude":  cand.latitude,
                    "longitude": cand.longitude,
                    "current": ["temperature_2m", "apparent_temperature", "weather_code"],
                    "daily":   ["temperature_2m_max", "temperature_2m_min",
                                "precipitation_probability_max", "weather_code"],
                    "timezone": "auto", "forecast_days": 4,
                })
                data = w_res.json()
        except Exception as e:
            return f"{header}\n\n出錯了喵...\n查天氣 API 失敗（{str(e)[:60]}）。"

        cur   = data["current"]
        daily = data["daily"]

        lines = [
            header, "",
            f"{wmo_emoji(cur['weather_code'])} 目前 {wmo_desc(cur['weather_code'])}",
            f"溫度：{cur['temperature_2m']}°C（體感 {cur['apparent_temperature']}°C）",
            "",
        ]
        day_labels = ["今天", "明天", "後天"]
        for i in range(min(3, len(daily.get("time", [])))):
            date = daily["time"][i]
            code = daily["weather_code"][i]
            tmin = daily["temperature_2m_min"][i]
            tmax = daily["temperature_2m_max"][i]
            pop  = daily["precipitation_probability_max"][i]
            label = day_labels[i] if i < len(day_labels) else date
            lines.append(f"**{label}** {date}")
            lines.append(f"{wmo_emoji(code)} {wmo_desc(code)}")
            lines.append(f"溫度：{tmin}°C ～ {tmax}°C")
            lines.append(f"降雨機率：{pop}%")
            lines.append("")
        lines.append("黑優浦蜜提醒：牢大要注意身體喵♡")
        return "\n".join(lines)


class LocationNotFoundError(Exception):
    pass
