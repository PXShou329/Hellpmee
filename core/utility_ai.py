"""
Optional Utility AI（內部輔助大腦）
─────────────────────────────────────────────────────────────────
重要原則（依需求 Section 3-6）：
    ❌ 不提供自由聊天入口
    ❌ 不接受使用者自由 prompt
    ❌ 不保存對話 session
    ❌ 不做長期記憶
    ✅ 只被功能內部以「受控資料」呼叫
    ✅ 每次呼叫都檢查全域 + 每使用者每天的限額
    ✅ 失敗時靜默 fallback（呼叫端用 try/except 或檢查 is_enabled）

當以下任一條件成立就直接視為 disabled：
    - UTILITY_AI_ENABLED = false
    - 對應後端沒有 API Key
    - 全域日限額用完
    - 該使用者今日限額用完
    - API 呼叫拋出例外

對外只暴露 3 個方法：
    is_available()              ── 同步檢查，給 /幫助 顯示用
    generate_food_reason(food, user_id)
    polish_translation(original, base_translation, target_lang, user_id)
"""
import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

# Backend 套件（兩個都 optional）
try:
    import google.generativeai as genai
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False

try:
    from openai import AsyncOpenAI
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False

from config import Config
from database.redis_cache import RedisCache


class UtilityAI:
    """
    Optional Utility AI 包裝層。
    所有公開方法失敗都會回 None（不 raise），呼叫端只要寫 fallback。
    """

    def __init__(self, cache: RedisCache):
        self.cache = cache
        self.enabled = Config.UTILITY_AI_ENABLED
        self.backend = Config.UTILITY_AI_BACKEND   # 'openai' / 'gemini'

        self._openai = None
        self._gemini = None

        if self.enabled:
            self._init_backend()
            if not self._has_any_backend():
                print("⚠️  UTILITY_AI_ENABLED=true 但沒有可用 backend，自動 disable")
                self.enabled = False

        if self.enabled:
            print(f"✅ Utility AI 已啟用（backend: {self.backend}）")
        else:
            print("ℹ️  Utility AI 未啟用，所有功能會用本地 fallback")

    def _init_backend(self):
        # OpenAI
        if (self.backend == 'openai' and _OPENAI_AVAILABLE
                and Config.OPENAI_API_KEY):
            try:
                self._openai = AsyncOpenAI(api_key=Config.OPENAI_API_KEY)
            except Exception as e:
                print(f"⚠️  OpenAI 初始化失敗：{e}")
        # Gemini
        if (self.backend == 'gemini' and _GEMINI_AVAILABLE
                and Config.GEMINI_API_KEY):
            try:
                genai.configure(api_key=Config.GEMINI_API_KEY)
                self._gemini = genai.GenerativeModel(Config.GEMINI_MODEL)
            except Exception as e:
                print(f"⚠️  Gemini 初始化失敗：{e}")

    def _has_any_backend(self) -> bool:
        return self._openai is not None or self._gemini is not None

    def is_available(self) -> bool:
        """給 /幫助 等地方做 UI 判斷用，不檢查當日限額"""
        return self.enabled and self._has_any_backend()

    # ════════════════════════════════════════════════════════
    #  限額檢查
    # ════════════════════════════════════════════════════════
    async def _check_limit(self, user_id: int) -> bool:
        """
        檢查全域 + 該使用者今日限額。
        True = 還可以用，False = 已超限
        """
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        ttl   = 86400  # 一天

        global_key = f"util_ai:global:{today}"
        user_key   = f"util_ai:user:{user_id}:{today}"

        global_count = await self.cache.incr_with_ttl(global_key, ttl)
        if global_count > Config.UTILITY_AI_DAILY_LIMIT:
            return False

        user_count = await self.cache.incr_with_ttl(user_key, ttl)
        if user_count > Config.UTILITY_AI_PER_USER_DAILY_LIMIT:
            return False

        return True

    # ════════════════════════════════════════════════════════
    #  /吃飯飯 用：產生短推薦理由
    # ════════════════════════════════════════════════════════
    async def generate_food_reason(self, food: str,
                                    user_id: int) -> Optional[str]:
        """
        為「已經由本地清單抽到的菜色」產生一句可愛短理由。
        失敗回 None，呼叫端用本地 fallback。
        """
        if not self.is_available():
            return None
        if not await self._check_limit(user_id):
            return None

        prompt = (
            f"請根據「{food}」這道食物，產生一句 30 字內、"
            f"以黑優浦蜜口吻（活潑、傲嬌、稱呼牢大、偶爾加喵♡）的推薦理由。"
            f"只輸出那句話，不要其他解釋。"
        )
        return await self._generate_safe(prompt, max_tokens=80)

    # ════════════════════════════════════════════════════════
    #  /喝什麼 用：產生短推薦理由
    # ════════════════════════════════════════════════════════
    async def generate_drink_reason(self, drink: str,
                                     user_id: int) -> Optional[str]:
        """
        為「已經由本地清單抽到的飲料」產生一句可愛短理由。
        """
        if not self.is_available():
            return None
        if not await self._check_limit(user_id):
            return None

        prompt = (
            f"請根據「{drink}」這杯飲料，產生一句 30 字內、"
            f"以黑優浦蜜口吻（活潑、傲嬌、稱呼牢大、偶爾加喵♡、可以毒舌）的推薦理由。"
            f"只輸出那句話，不要其他解釋、不要引號。"
        )
        return await self._generate_safe(prompt, max_tokens=80)

    # ════════════════════════════════════════════════════════
    #  /天氣 用：地名正規化（v2.1.6 新增）
    # ════════════════════════════════════════════════════════
    async def normalize_location(self, query: str,
                                   user_id: int = 0) -> Optional[str]:
        """
        把任意語言的地名轉成標準英文「City, Country」格式。
        ⚠️ AI 只做翻譯/正規化，禁止編造天氣資料。
        """
        if not self.is_available():
            return None
        if not await self._check_limit(user_id):
            return None

        prompt = (
            "把以下地名正規化成英文「City, Country」格式，方便我去查 geocoding API。\n"
            "規則：\n"
            "1. 只輸出『City, Country』或『City, State, Country』格式，不加引號、不加說明\n"
            "2. 如果是台灣地名，請用「Kaohsiung, Taiwan」這種格式\n"
            "3. 如果有可能對應多個城市，選最常見的一個\n"
            "4. 如果完全看不出來，輸出 UNKNOWN（大寫四個字母）\n"
            f"地名：{query}\n"
            "正規化結果："
        )
        result = await self._generate_safe(prompt, max_tokens=50)
        if not result:
            return None
        cleaned = result.strip().strip("「」\"'").split("\n")[0].strip()
        if not cleaned or cleaned == "UNKNOWN":
            return None
        return cleaned

    # ════════════════════════════════════════════════════════
    #  /天氣 用：地名正規化（v2.1.8 多候選版）
    # ════════════════════════════════════════════════════════
    async def normalize_location_candidates(self, query: str,
                                              user_id: int = 0) -> list:
        """
        把任意地名轉成 1-5 個英文「City, Country」候選。
        例：
            日本 青森縣 → [
              "Aomori, Japan", "Aomori Prefecture, Japan",
              "Aomori City, Japan"
            ]
            瑞士 日內瓦 → ["Geneva, Switzerland", "Genève, Switzerland"]

        失敗回空 list。
        """
        if not self.is_available():
            return []
        if not await self._check_limit(user_id):
            return []

        prompt = (
            "請把以下地名翻譯 / 正規化成英文「City, Country」格式，產生 1-5 個候選查詢字串。\n"
            "規則：\n"
            "1. 只輸出 JSON array of strings，沒有任何其他文字、沒有 markdown 代碼塊\n"
            "2. 每個字串都是「City, Country」或「City, State, Country」格式\n"
            "3. 從最可能到最不可能排序\n"
            "4. 如果是台灣地名，用「Kaohsiung, Taiwan」格式\n"
            "5. 如果有多種拼法（例如有 / 沒 Prefecture），都列出來\n"
            "6. 完全看不出來輸出 []\n\n"
            f"地名：{query}\n"
            "JSON 輸出："
        )
        result = await self._generate_safe(prompt, max_tokens=200)
        if not result:
            return []

        # 嘗試 parse JSON
        import json as _json
        cleaned = result.strip()
        # 移除可能的 markdown 代碼塊
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()

        try:
            arr = _json.loads(cleaned)
            if isinstance(arr, list):
                return [s.strip() for s in arr if isinstance(s, str) and s.strip()]
        except Exception as e:
            print(f"[UtilityAI] normalize_location_candidates JSON parse 失敗：{e}")
            # 退而求其次：把整段當單一字串
            return [cleaned] if cleaned else []
        return []

    # ════════════════════════════════════════════════════════
    #  /翻譯姬 自然語氣模式
    # ════════════════════════════════════════════════════════
    async def polish_translation(self, original: str, base_translation: str,
                                  target_lang: str,
                                  user_id: int) -> Optional[str]:
        """
        把基礎直譯結果潤飾成更自然的目標語言。
        失敗回 None，呼叫端用直譯結果 fallback。

        ⚠️ prompt 強化（v2.1.1）：依目標語言給「具體任務」而非籠統指示，
           讓自然語氣模式真的跟直譯有明顯差異。
        """
        if not self.is_available():
            return None
        if not await self._check_limit(user_id):
            return None

        # 依目標語言給「具體任務」+ 具體範例
        task_hint = self._task_hint_for(target_lang)

        prompt = (
            "你是專業母語潤飾師。任務是把『機器翻譯』改寫成自然、像母語者真的會說的句子。\n\n"
            "嚴格規則：\n"
            "1. 保持原意，不增刪資訊\n"
            "2. 不要逐字對應，要重新組句\n"
            "3. 改掉生硬的機翻腔（例：「我」用法、奇怪的語序、過度書面）\n"
            "4. 只輸出潤飾後的翻譯，不要加說明、引號、前綴、後綴\n"
            f"5. {task_hint}\n\n"
            f"【原文】\n{original}\n\n"
            f"【機翻初稿】\n{base_translation}\n\n"
            f"【目標語言】{target_lang}\n\n"
            "潤飾後："
        )
        result = await self._generate_safe(prompt, max_tokens=512)
        if not result:
            return None
        # 清理：去頭尾空白、可能殘留的引號和前綴
        cleaned = result.strip().strip('「」"\'')
        # 去掉「潤飾後：」這種前綴
        for prefix in ["潤飾後：", "潤飾後:", "Polished:", "翻譯：", "Translation:"]:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
        return cleaned if cleaned else None

    @staticmethod
    def _task_hint_for(target_lang: str) -> str:
        """
        針對每種目標語言給「具體要做什麼」，不要籠統說「自然」。
        """
        hints = {
            '日文':
                "目標：日文母語自然程度。"
                "適合用在跟熟識但需要尊重的人（例如 homestay 爸媽、學長）。"
                "用ですます型，但去掉過於僵硬的書面表達。"
                "助詞自然、節奏像日常會話。避免直接照抄漢字詞，"
                "如有更地道的和語表達，請使用（例：『理解しました』→『わかりました』）",
            '英文':
                "目標：母語者日常表達。"
                "避免逐字翻譯、避免中式英文語序。"
                "短句優先、用 contractions（don't, I'm 等）。"
                "正式度跟原文一致：原文輕鬆就口語，原文正式就半正式。",
            '韓文':
                "目標：自然韓文。"
                "使用適當禮貌等級（해요體為主），避免過度硬翻。"
                "助詞自然、語順像韓國人會說的方式。",
            '繁體中文':
                "目標：自然台灣繁體中文。"
                "不要簡體字、不要中國用語（例：視頻→影片、信息→訊息、軟件→軟體）。"
                "去除機翻腔，讓句子像台灣人會說的話。",
            '簡體中文':
                "目標：自然簡體中文。"
                "符合大陸用語習慣。去除機翻腔。",
        }
        return hints.get(
            target_lang,
            "目標：母語者自然程度。重新組句，去除機翻腔。",
        )

    # 舊方法保留為相容（部分舊呼叫可能還在用）
    @staticmethod
    def _tone_hint_for(target_lang: str) -> str:
        return UtilityAI._task_hint_for(target_lang)

    # ════════════════════════════════════════════════════════
    #  內部：實際呼叫 AI（兩個 backend 都包成同一個介面）
    # ════════════════════════════════════════════════════════
    async def _generate_safe(self, prompt: str,
                              max_tokens: int = 100) -> Optional[str]:
        """呼叫實際 backend，任何錯誤都靜默吞掉回 None"""
        try:
            if self.backend == 'openai' and self._openai:
                return await self._call_openai(prompt, max_tokens)
            if self.backend == 'gemini' and self._gemini:
                return await self._call_gemini(prompt, max_tokens)
        except Exception as e:
            print(f"[UtilityAI] 呼叫失敗（已靜默 fallback）：{e}")
        return None

    async def _call_openai(self, prompt: str, max_tokens: int) -> Optional[str]:
        res = await self._openai.chat.completions.create(
            model=Config.OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.7,
        )
        return res.choices[0].message.content

    async def _call_gemini(self, prompt: str, max_tokens: int) -> Optional[str]:
        res = await asyncio.to_thread(
            self._gemini.generate_content, prompt,
            generation_config={'max_output_tokens': max_tokens, 'temperature': 0.7},
        )
        return res.text
