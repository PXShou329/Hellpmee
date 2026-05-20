"""
翻譯引擎
─────────────────────────────────────────────────────────────────
使用 deep-translator 的 GoogleTranslator backend。

優點：
    1. 不需要 API Key（透過 Google 翻譯網頁介面）
    2. 完全免費
    3. 不會被 AI 二次語境對齊，直接逐字翻譯

注意：
    Google 偶爾會微調網頁介面，造成翻譯短暫失效。
    deep-translator 社群會更新，但「production critical」不建議用這個方法。
    對個人 Discord Bot 來說完全夠用。
"""
import asyncio
from typing import Optional

from deep_translator import GoogleTranslator


class TranslateEngine:
    """
    直譯引擎。
    完全不經過 AI，避免任何「語境對齊」。
    """

    # 中文語言名稱 → ISO 代碼
    # deep-translator 用 ISO 639-1 代碼或語言全名都認得
    _LANG_MAP = {
        '繁體中文': 'zh-TW',
        '簡體中文': 'zh-CN',
        '中文':     'zh-CN',
        '英文':     'en',
        '日文':     'ja',
        '韓文':     'ko',
        '西班牙文': 'es',
        '法文':     'fr',
        '德文':     'de',
        '義大利文': 'it',
        '俄文':     'ru',
        '葡萄牙文': 'pt',
        '阿拉伯文': 'ar',
        '泰文':     'th',
        '越南文':   'vi',
        '自動偵測': 'auto',
    }

    SUPPORTED_LANGS = list(_LANG_MAP.keys())

    async def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """
        把 text 從 source_lang 翻譯到 target_lang。
        失敗會 raise，呼叫端要處理。
        """
        src = self._LANG_MAP.get(source_lang, 'auto')
        tgt = self._LANG_MAP.get(target_lang, 'en')

        if src == tgt:
            return text  # 不用翻譯

        # GoogleTranslator 是同步的，用 executor 包起來
        def _do_translate():
            translator = GoogleTranslator(source=src, target=tgt)
            return translator.translate(text)

        return await asyncio.get_event_loop().run_in_executor(None, _do_translate)
