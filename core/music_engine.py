"""
音樂引擎（v2.1）
─────────────────────────────────────────────────────────────────
功能：
    detect_input_type(query)          → 'youtube_url' / 'spotify_url' / 'text'
    resolve_youtube_url(url)          → 直接解析 YT 影片
    resolve_spotify(url, ...)         → Spotify metadata → YouTube 音源
    search_youtube(query, exclude...) → 純文字搜尋 + confidence scoring
    get_stream_url(url)               → 取得實際音訊串流網址
    get_discord_audio_source(url)     → FFmpeg PCM Audio
    check_ffmpeg()                    → 啟動時檢查 ffmpeg

新增：YouTube 搜尋結果 confidence score
    ≥80 → 自動播放
    50~79 → 顯示候選讓使用者選
    <50 → 提示貼 URL
"""
import asyncio
import math
import os
import re
import shutil
import subprocess
import unicodedata
from typing import Optional

import yt_dlp

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    _SPOTIPY_AVAILABLE = True
except ImportError:
    _SPOTIPY_AVAILABLE = False

from config import Config


# ════════════════════════════════════════════════════════════════
#  yt-dlp 設定
# ════════════════════════════════════════════════════════════════
# ⭐ 雲端穩定化：YouTube player client 順序
#    手動實測在 DigitalOcean 機房 IP 上，android_vr / web 較容易避開 bot check，
#    android / ios 當後備（可能有 PO Token warning，但不一定致命）。
_YOUTUBE_PLAYER_CLIENTS = ['android_vr', 'web', 'android', 'ios']


def _base_ydl_opts() -> dict:
    """所有 yt-dlp 呼叫共用的基底設定（每次動態建立，才能即時吃到 cookies 檔）。"""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'source_address': '0.0.0.0',
        'retries': 3,
        'fragment_retries': 3,
        'nocheckcertificate': True,
        # ⭐ 給 yt-dlp 指定 YouTube client 順序，提升雲端解析成功率
        'extractor_args': {'youtube': {'player_client': _YOUTUBE_PLAYER_CLIENTS}},
    }
    # ⭐ 選填 cookies（不硬編碼、不進 git）：YTDLP_COOKIES_FILE 指到檔案才會用
    cookies = getattr(Config, 'YTDLP_COOKIES_FILE', '') or ''
    if cookies and os.path.exists(cookies):
        opts['cookiefile'] = cookies
    return opts


def _search_opts() -> dict:
    return {
        **_base_ydl_opts(),
        'format': 'bestaudio/best',
        'extract_flat': True,
        'default_search': 'ytsearch',
    }


def _stream_opts() -> dict:
    return {
        **_base_ydl_opts(),
        'format': 'bestaudio/best',
        'noplaylist': True,
    }


_FFMPEG_OPTS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}


# ════════════════════════════════════════════════════════════════
#  YouTube 錯誤分類（疑難雜症 7.8）
# ════════════════════════════════════════════════════════════════
def classify_ytdlp_error(error) -> str:
    """把 yt-dlp 的例外訊息歸類，方便決定要不要 fallback、要怎麼提示使用者。"""
    msg = str(error).lower()
    if 'sign in to confirm' in msg or 'not a bot' in msg or 'confirm you' in msg:
        return 'youtube_bot_check'
    if 'age' in msg and 'restrict' in msg:
        return 'age_restricted'
    if 'private video' in msg:
        return 'private_video'
    if 'video unavailable' in msg or 'removed' in msg or 'terminated' in msg:
        return 'video_unavailable'
    if 'not available in your country' in msg or 'geo' in msg:
        return 'geo_blocked'
    if 'requested format is not available' in msg or 'no video formats' in msg:
        return 'no_playable_format'
    if 'unable to extract' in msg:
        return 'extract_failed'
    if 'http error 403' in msg or '403' in msg:
        return 'http_403'
    return 'unknown'


# 這些分類代表「這個版本被擋」→ 值得嘗試同一首歌的其他版本
_FALLBACK_WORTHY = {
    'youtube_bot_check', 'age_restricted', 'private_video',
    'video_unavailable', 'geo_blocked', 'no_playable_format',
    'http_403', 'extract_failed',
}


# ════════════════════════════════════════════════════════════════
#  同曲辨識（疑難雜症 7.4~7.7 / 指令文件 §6）
#  原則：deterministic 規則為主，寧可不播也不要播錯。
# ════════════════════════════════════════════════════════════════
_CJK_RE     = re.compile(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]')
_CJK_RUN_RE = re.compile(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+')

# 標題清理時要拿掉的雜訊詞（給 fallback 搜尋字串用，避免把髒標題帶進搜尋）
_TITLE_NOISE = [
    'official music video', 'official audio', 'official video', 'official mv',
    'lyric video', 'lyrics', 'lyric', 'official', 'audio', 'mv', 'm/v',
    'hd', 'hq', '4k', '高音質', '高画質', '高清', '完整版', '無損', '字幕',
    'full version', 'full', 'visualizer', 'video',
]
# 版本差異關鍵字（候選有、但使用者沒要 → 視為不同版本）
_VERSION_MARKERS = [
    'cover', '翻唱', 'remix', 'nightcore', 'karaoke', '伴奏', 'instrumental',
    'live', 'reaction', 'tutorial', 'piano', 'violin', 'mashup',
    'sped up', 'slowed', 'acoustic', '8d', 'loop',
]


def _norm(s: str) -> str:
    """正規化：NFKC（全形→半形）、小寫、去頭尾空白。"""
    if not s:
        return ''
    return unicodedata.normalize('NFKC', s).lower().strip()


def _cjk_chars(s: str) -> set:
    return set(''.join(_CJK_RUN_RE.findall(_norm(s))))


def is_cjk_short_query(q: str) -> bool:
    """
    判斷使用者輸入是不是「短中/日/韓文歌名」（沒空格、CJK 為主、≤6 字）。
    例：遇見 / 淒美地 / アイドル → True
        孫燕姿 遇見 / never gonna give you up → False
    """
    qn = _norm(q).replace(' ', '')
    if not qn or not _CJK_RE.search(qn):
        return False
    if ' ' in _norm(q).strip():
        return False
    return len(qn) <= 6


def clean_song_title(title: str) -> str:
    """把 YouTube 標題清成乾淨的『歌名核心』，給 fallback 搜尋用（疑難雜症 7.7）。"""
    t = title or ''
    # 1) 圓括號 / 方括號 / 【】〔〕 內通常是雜訊（Official Video 之類）→ 連內容一起拿掉
    t = re.sub(r'[\(\（\[\［【〔].*?[\)\）\]\］】〕]', ' ', t)
    # 2) 書名號 / 引號（「」『』 等）只去符號、保留內容
    #    —— 日文歌名常放在「」內，內容不能刪
    for ch in '「」『』“”"＂\'':
        t = t.replace(ch, ' ')
    # 拿掉 10小時 / 1 hour 之類
    t = re.sub(r'\d+\s*(小時|hours?|分鐘|min)', ' ', t, flags=re.I)
    low = t.lower()
    for w in _TITLE_NOISE:
        low = low.replace(w, ' ')
    # 用清理後的小寫位置砍回原字串長度（簡化：直接對原字串做不分大小寫替換）
    for w in _TITLE_NOISE:
        t = re.sub(re.escape(w), ' ', t, flags=re.I)
    t = re.sub(r'[\|/]+', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip(' -–—_')
    return t.strip()


def build_fallback_queries(requested_query: str, title: str,
                            uploader: str) -> list[str]:
    """
    產生 fallback 搜尋字串。優先用使用者原始 query（最乾淨），
    再用清理後的歌名 + 歌手，最後才用原始 query 補強。
    """
    queries: list[str] = []

    def _add(q: str):
        q = (q or '').strip()
        if q and q.lower() not in [x.lower() for x in queries]:
            queries.append(q)

    rq = (requested_query or '').strip()
    # requested_query 是 URL 就不拿來當搜尋字
    if rq and not rq.lower().startswith('http'):
        _add(f"{rq} official audio")
        _add(f"{rq} audio")
        _add(rq)

    core = clean_song_title(title)
    artist = (uploader or '').strip()
    # 把 "- Topic" / "VEVO" 這種非歌手雜訊拿掉
    artist_clean = re.sub(r'(?i)\s*-\s*topic|vevo|official', '', artist).strip()
    if core:
        if artist_clean and artist_clean.lower() not in core.lower():
            _add(f"{artist_clean} {core} official audio")
            _add(f"{artist_clean} {core}")
        _add(f"{core} official audio")
        _add(f"{core} audio")
        _add(f"{core} lyrics")

    return queries[:6]


def score_same_song(requested_query: str,
                     ref_title: str,
                     candidate_title: str,
                     candidate_uploader: str = '',
                     duration=None) -> tuple[int, list[str]]:
    """
    判斷『候選』跟『使用者真正想要的那首歌』是不是同一首。
    回傳 (score, reasons)。分數越高越像同一首。

    deterministic 為主：
      - 短 CJK query（如「遇見」）：候選標題沒完整包含就重扣（避免播成「遇到」）
      - 命中歌名 / 歌手 → 加分
      - cover / remix / live 等版本標記（使用者沒要）→ 扣分
    """
    reasons: list[str] = []
    score = 0

    rq        = _norm(requested_query)
    cand      = _norm(candidate_title)
    ref       = _norm(ref_title)
    uploader  = _norm(candidate_uploader)

    # ── 短 CJK query：硬規則（疑難雜症 7.6）──
    if is_cjk_short_query(requested_query):
        if rq and rq in cand:
            score += 50
            reasons.append(f"短CJK歌名「{requested_query}」完整命中 +50")
        else:
            score -= 80
            reasons.append(f"短CJK歌名「{requested_query}」未完整出現 -80")
    else:
        # ── 一般 query：整串命中 / 個別詞命中 ──
        if rq and rq in cand:
            score += 40
            reasons.append("query 整串命中 +40")
        else:
            words = [w for w in rq.split() if len(w) > 1]
            if words:
                hit = sum(1 for w in words if w in cand)
                ratio = hit / len(words)
                add = int(ratio * 35)
                score += add
                reasons.append(f"query 詞命中 {hit}/{len(words)} +{add}")
                if ratio < 0.5:
                    score -= 30
                    reasons.append("命中比例過低 -30")

    # ── 跟原始（已選定）標題的 CJK 字重疊（fallback 時 ref_title 有值）──
    ref_cjk = _cjk_chars(ref)
    if ref_cjk:
        cand_cjk = _cjk_chars(cand)
        if cand_cjk:
            coverage = len(ref_cjk & cand_cjk) / len(ref_cjk)
            if coverage >= 0.99:
                score += 25
                reasons.append("原標題CJK完全涵蓋 +25")
            elif coverage >= 0.7:
                score += 10
                reasons.append(f"原標題CJK涵蓋 {coverage:.0%} +10")
            else:
                score -= 40
                reasons.append(f"原標題CJK涵蓋僅 {coverage:.0%} -40")

    # ── 歌手命中 ──
    artist_tokens = [t for t in re.split(r'[\s\-,，、]+', ref) if len(t) > 1]
    if artist_tokens and any(t in uploader or t in cand for t in artist_tokens[:1]):
        score += 15
        reasons.append("疑似歌手命中 +15")

    # ── 官方頻道 ──
    if any(h in uploader for h in ('vevo', 'topic', 'official', 'records')):
        score += 15
        reasons.append("官方頻道 +15")

    # ── 版本標記（使用者沒要 cover/remix/live...）──
    for marker in _VERSION_MARKERS:
        if marker in cand and marker not in rq:
            score -= 40
            reasons.append(f"含版本標記「{marker}」(使用者未要求) -40")
            break

    # ── 長度合理性 ──
    if duration is not None:
        try:
            d = int(float(duration))
            if 90 <= d <= 600:
                score += 10
                reasons.append("長度合理 +10")
            elif d > 900 and 'live' not in rq:
                score -= 30
                reasons.append("長度過長(>15min) -30")
        except (TypeError, ValueError):
            pass

    return score, reasons


# ════════════════════════════════════════════════════════════════
#  Confidence Scoring 規則表
# ════════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════
# v2.1.8 規格化分數（官方 +30 / 限制類型 -50）
# ════════════════════════════════════════════════════════════════
_POSITIVE_KEYWORDS = {
    # 三組「官方」 → 統一 +30（規格 II.4）
    'official audio':       30,
    'official music video': 30,
    'official video':       30,
    'official mv':          30,
    'official':             30,    # 含「official」字也視為官方
    # 弱訊號（不算官方但有加分）
    'mv':                   10,
    'audio':                5,
}

_NEGATIVE_KEYWORDS = {
    # 限制類型（若關鍵字未含則 -50）
    'cover':       -50,
    'remix':       -50,
    'nightcore':   -50,
    'karaoke':     -50,
    'instrumental':-50,
    'live':        -50,
    'reaction':    -50,
    'tutorial':    -50,
    'piano':       -50,
    'piano cover': -50,
    'violin cover':-50,
    'mashup':      -50,
    'sped up':     -30,
    'slowed':      -30,
    # 應該已被過濾，這裡再補保險
    '1 hour':      -100,
    '10 hours':    -100,
    '10 hour':     -100,
    'loop':        -80,
    'shorts':      -100,
}
_OFFICIAL_CHANNEL_HINTS = ['VEVO', 'Topic', 'Official', 'Records']


# ════════════════════════════════════════════════════════════════
#  播放結果過濾（規格一）
# ════════════════════════════════════════════════════════════════
def _is_playable_video(entry: dict) -> bool:
    """
    判斷 yt-dlp 回傳的 entry 是不是可播放的單一影片。
    過濾掉：channel / playlist / shorts / live stream / 沒 id 的結果

    回傳 True 代表是可播放影片；False 應該被過濾。
    """
    if not entry:
        return False

    # ── _type 過濾 ──
    _type = entry.get('_type')
    # yt-dlp 的 _type:
    #   None / 'video' / 'url' → 單一影片（OK）
    #   'playlist'             → 播放清單（拒）
    #   'channel'              → 頻道（拒）
    #   'url_transparent'      → 通常是影片
    if _type in ('playlist', 'channel'):
        return False

    # ── 沒 id 拒 ──
    if not (entry.get('id') or entry.get('url')):
        return False

    # ── 沒 title 拒 ──
    if not entry.get('title'):
        return False

    # ── ie_key 過濾（channel/playlist extractor）──
    ie_key = (entry.get('ie_key') or '').lower()
    if 'channel' in ie_key or 'playlist' in ie_key:
        return False

    # ── URL 形態檢查 ──
    url = (entry.get('webpage_url') or entry.get('url') or '').lower()
    if any(bad in url for bad in ['/channel/', '/playlist?', '/@', '/c/', '/shorts/', '/user/']):
        return False

    # ── live stream 拒 ──
    if entry.get('is_live') or entry.get('was_live'):
        return False
    live_status = (entry.get('live_status') or '').lower()
    if live_status in ('is_live', 'is_upcoming', 'post_live'):
        return False

        # ── 標題關鍵字過濾（規格一）──
    title_lower = (entry.get('title') or '').lower()
    _BAD_TITLE_KEYWORDS = ('shorts', '#shorts', ' live ', 'live stream', '🔴',
                            ' mix ', 'best mix', 'top mix', 'playlist',
                            'channel trailer', 'reaction', 'tutorial')
    if any(kw in title_lower for kw in _BAD_TITLE_KEYWORDS):
        return False

    # ── duration 太短（shorts，<60s）或太長（>15min）拒 ──
    duration = entry.get('duration')
    if duration is not None:
        try:
            d = int(float(duration))
            if d < 60:        # 1 分鐘以內視為 shorts，拒
                return False
            if d > 900:       # 15 分鐘以上拒（reaction / 1 hour loop / 演講…）
                return False
        except (TypeError, ValueError):
            pass
    # duration 是 None 不擋，但會在 confidence 降權

    return True


class MusicEngine:

    def __init__(self):
        self.ffmpeg_path = Config.FFMPEG_PATH or "ffmpeg"
        self.ffmpeg_ok   = self._check_ffmpeg()

        self._spotify = None
        if (_SPOTIPY_AVAILABLE and Config.SPOTIFY_CLIENT_ID
                and Config.SPOTIFY_CLIENT_SECRET):
            try:
                auth = SpotifyClientCredentials(
                    client_id=Config.SPOTIFY_CLIENT_ID,
                    client_secret=Config.SPOTIFY_CLIENT_SECRET,
                )
                self._spotify = spotipy.Spotify(auth_manager=auth)
                print("✅ Spotify 連線成功")
            except Exception as e:
                print(f"⚠️  Spotify 初始化失敗：{e}")

    @property
    def spotify_available(self) -> bool:
        return self._spotify is not None

    # ════════════════════════════════════════════════════════
    #  FFmpeg 啟動檢查
    # ════════════════════════════════════════════════════════
    def _check_ffmpeg(self) -> bool:
        if '/' not in self.ffmpeg_path and '\\' not in self.ffmpeg_path:
            found = shutil.which(self.ffmpeg_path)
            if not found:
                self._print_ffmpeg_warning()
                return False
            real = found
        else:
            real = self.ffmpeg_path

        try:
            r = subprocess.run([real, '-version'], capture_output=True, timeout=3)
            if r.returncode == 0:
                print(f"✅ FFmpeg 已找到：{real}")
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            print(f"⚠️  FFmpeg 測試失敗：{e}")

        self._print_ffmpeg_warning()
        return False

    def _print_ffmpeg_warning(self):
        print("⚠️  ════════════════════════════════════════════════════")
        print(f"⚠️  找不到 ffmpeg！(嘗試路徑：{self.ffmpeg_path})")
        print("⚠️  /歌姬啦 無法播放音樂！")
        print("⚠️  Windows：下載 https://www.gyan.dev/ffmpeg/builds/")
        print("⚠️           解壓後在 .env 設 FFMPEG_PATH=C:\\ffmpeg\\bin\\ffmpeg.exe")
        print("⚠️  Mac:    brew install ffmpeg")
        print("⚠️  Linux:  sudo apt install ffmpeg")
        print("⚠️  ════════════════════════════════════════════════════")

    # ════════════════════════════════════════════════════════
    #  輸入類型判斷
    # ════════════════════════════════════════════════════════
    @staticmethod
    def detect_input_type(query: str) -> str:
        q = query.strip().lower()
        if 'youtube.com' in q or 'youtu.be' in q:
            return 'youtube_url'
        if 'spotify.com' in q or q.startswith('spotify:'):
            return 'spotify_url'
        return 'text'

    @staticmethod
    def parse_spotify_url(url: str) -> Optional[tuple[str, str]]:
        """回傳 (resource_type, id)，resource_type ∈ {track, playlist, album}"""
        # 同時支援：spotify:track:xxx 和 open.spotify.com/track/xxx
        m = re.search(r'(track|playlist|album)[:/]([A-Za-z0-9]+)', url)
        if not m:
            return None
        return (m.group(1), m.group(2))

    # ════════════════════════════════════════════════════════
    #  YouTube URL 直接解析
    # ════════════════════════════════════════════════════════
    async def resolve_youtube_url(self, url: str) -> Optional[dict]:
        """
        把 YouTube URL 解析成歌曲資訊（不下載）。
        回傳 dict 或 None。
        """
        def _do():
            with yt_dlp.YoutubeDL(_stream_opts()) as ydl:
                info = ydl.extract_info(url, download=False)
                if 'entries' in info and info['entries']:
                    info = info['entries'][0]
                return info

        try:
            info = await asyncio.get_event_loop().run_in_executor(None, _do)
        except Exception as e:
            print(f"[Music] YouTube URL 解析失敗：{e}")
            return None

        if not info:
            return None
        return {
            'title':       info.get('title', '未知曲目'),
            'url':         info.get('webpage_url', url),
            'webpage_url': info.get('webpage_url', url),
            'duration':    info.get('duration', 0) or 0,
            'uploader':    info.get('uploader', '未知頻道'),
            'thumbnail':   info.get('thumbnail', ''),
            'source_type': 'youtube_url',
            'display_source': 'YouTube',
        }

    # ════════════════════════════════════════════════════════
    #  Spotify 解析
    # ════════════════════════════════════════════════════════
    async def resolve_spotify(self, url: str) -> Optional[list[dict]]:
        """
        Spotify URL → 取 metadata → 用「歌手 + 歌名」搜尋 YouTube。
        回傳 list of song info（playlist 會有多筆，single 一筆）。
        每筆已經是可加入佇列的格式。
        """
        if not self._spotify:
            return None

        parsed = self.parse_spotify_url(url)
        if not parsed:
            return None
        kind, sid = parsed

        try:
            if kind == 'track':
                meta = await asyncio.to_thread(self._spotify.track, sid)
                metas = [meta]
            elif kind == 'playlist':
                pl = await asyncio.to_thread(self._spotify.playlist_items, sid,
                                              limit=Config.SPOTIFY_PLAYLIST_MAX)
                metas = [it['track'] for it in pl['items'] if it.get('track')]
            elif kind == 'album':
                al = await asyncio.to_thread(self._spotify.album_tracks, sid,
                                              limit=Config.SPOTIFY_PLAYLIST_MAX)
                metas = al['items']
            else:
                return None
        except Exception as e:
            print(f"[Music] Spotify 解析失敗：{e}")
            return None

        # 對每筆 metadata，去 YouTube 找音源
        results = []
        for m in metas[:Config.SPOTIFY_PLAYLIST_MAX]:
            if not m:
                continue
            track_name = m.get('name', '')
            artists    = ', '.join(a['name'] for a in m.get('artists', []))
            search_q   = f"{artists} {track_name}".strip()
            if not search_q:
                continue

            yt_results = await self.search_youtube(search_q, max_results=1, scored=False)
            if not yt_results:
                continue
            first = yt_results[0]

            # Spotify 提供更好的標題 + 封面
            sp_thumb = ''
            if m.get('album', {}).get('images'):
                sp_thumb = m['album']['images'][0]['url']

            results.append({
                'title':       f"{artists} - {track_name}",
                'url':         first['url'],
                'webpage_url': first['webpage_url'],
                'duration':    (m.get('duration_ms', 0) // 1000) or first.get('duration', 0),
                'uploader':    artists or first.get('uploader', '未知'),
                'thumbnail':   sp_thumb or first.get('thumbnail', ''),
                'source_type': 'spotify_url',
                'display_source': 'Spotify',
            })
        return results

    # ════════════════════════════════════════════════════════
    #  YouTube 搜尋 + Confidence Scoring
    # ════════════════════════════════════════════════════════
    async def search_youtube(self, query: str,
                              max_results: int = 5,
                              scored: bool = True) -> list[dict]:
        """
        搜尋 YouTube。
        scored=True 時，每筆會附 confidence 0-100 分數，並依分數排序。

        ⭐ v2.1.2 修正：extract_flat=True 模式下 yt-dlp 回傳的 url 可能是 video ID
        而不是完整 URL，要在這裡補完整，否則 get_stream_url 會失敗。
        """
        search_query = f'ytsearch{max_results}:{query}'

        def _do():
            with yt_dlp.YoutubeDL(_search_opts()) as ydl:
                info = ydl.extract_info(search_query, download=False)
                return info.get('entries', []) if 'entries' in info else [info]

        try:
            entries = await asyncio.get_event_loop().run_in_executor(None, _do)
        except Exception as e:
            print(f"[Music] YouTube 搜尋失敗：{e}")
            import traceback
            traceback.print_exc()
            return []

        results = []
        for e in entries:
            if not e:
                continue

            # ⭐ 規格 1：過濾掉非播放影片結果（channel / playlist / shorts / live / 無 duration）
            if not _is_playable_video(e):
                print(f"[Music] 略過非播放影片：type={e.get('_type')} title={e.get('title')!r}")
                continue

            # ⭐ 修 bug：補完整 URL
            video_id    = e.get('id') or ''
            webpage_url = e.get('webpage_url') or e.get('url') or ''
            # extract_flat 模式下 webpage_url 可能還是 video ID
            if webpage_url and not webpage_url.startswith('http'):
                webpage_url = f"https://www.youtube.com/watch?v={webpage_url}"
            # 如果還是沒有，用 video_id 組
            if not webpage_url and video_id:
                webpage_url = f"https://www.youtube.com/watch?v={video_id}"

            if not webpage_url:
                print(f"[Music] 略過無 URL 的搜尋結果：{e.get('title')}")
                continue

            item = {
                'title':       e.get('title') or '未知曲目',
                'url':         webpage_url,
                'webpage_url': webpage_url,
                'duration':    e.get('duration') or 0,
                'uploader':    e.get('uploader') or '未知頻道',
                'thumbnail':   e.get('thumbnail') or '',
                'view_count':  e.get('view_count') or 0,
                'source_type': 'youtube_search',
                'display_source': 'YouTube',
                'video_id':    video_id,
            }
            if scored:
                item['confidence'] = self._score(item, query)
            results.append(item)

        if scored:
            # ⭐ v2.2.0 規格四.2：官方群組 / 非官方群組，各自 view_count 高→低
            def _is_official(item):
                title = (item.get('title') or '').lower()
                uploader = (item.get('uploader') or '').lower()
                if any(h.lower() in uploader for h in _OFFICIAL_CHANNEL_HINTS):
                    return True
                for kw in ('official', '官方', 'vevo'):
                    if kw in title or kw in uploader:
                        return True
                return False

            def _sort_key(item):
                # 先官方(0)/非官方(1)，再 confidence 高→低，再 view_count 高→低
                official_rank = 0 if _is_official(item) else 1
                return (official_rank,
                        -item.get('confidence', 0),
                        -(item.get('view_count') or 0))
            results.sort(key=_sort_key)
        return results

    # ── Confidence Score 演算法 ───────────────────────────
    def _score(self, item: dict, query: str) -> int:
        """
        計算搜尋結果跟 query 的匹配度（v2.1.8 規格化版）。
        官方 +30、限制類型 -50（若關鍵字未含）、觀看數做 tiebreaker。
        """
        title    = (item.get('title') or '').lower()
        uploader = (item.get('uploader') or '').lower()
        views    = item.get('view_count') or 0
        q_lower  = query.lower()

        score = 50

        # ── 標題包含 query 整體 ──
        if q_lower in title:
            score += 20

        # ── 標題包含 query 個別字詞 ──
        q_words = [w for w in q_lower.split() if len(w) > 1]
        if q_words:
            hit = sum(1 for w in q_words if w in title)
            score += int(hit / len(q_words) * 15)

        # ── 加分關鍵字（標題含 official）──
        for kw, pts in _POSITIVE_KEYWORDS.items():
            if kw in title:
                score += pts
                break  # 只計一次

        # ── 官方頻道（VEVO / Topic / Official / Records）+30 ──
        for hint in _OFFICIAL_CHANNEL_HINTS:
            if hint.lower() in uploader:
                score += 30
                break

        # ── 扣分關鍵字（規格 II.4：限制類型 -50，除非使用者明確要）──
        for kw, pts in _NEGATIVE_KEYWORDS.items():
            if kw in title and kw not in q_lower:
                score += pts

        # ── 觀看數做 tiebreaker（log scale，最多 +20）──
        if views > 0:
            log_views = math.log10(views)         # 1萬→4 / 100萬→6 / 1億→8
            score += min(int(log_views * 2.5), 20)

        return max(0, min(score, 100))

    # ════════════════════════════════════════════════════════
    #  ⭐ 歌手 Top Tracks（規格 1+2）
    # ════════════════════════════════════════════════════════
    async def search_top_tracks_for_artist(self, artist: str,
                                            max_results: int = 15) -> list[dict]:
        """
        為「歌手 / 樂團」搜尋熱門歌曲（規格 1+2）。
        為了確保拿到足夠可播放結果，先抓 20 筆再過濾。
        """
        query = f"{artist} popular songs"
        all_results = await self.search_youtube(query, max_results=25, scored=True)
        return all_results[:max_results]

    # ════════════════════════════════════════════════════════
    #  Spotify 文字搜尋（依規格六：來源 = Spotify 時用）
    # ════════════════════════════════════════════════════════
    async def search_spotify_then_youtube(self, query: str,
                                            max_results: int = 5) -> list[dict]:
        """
        用 Spotify API 文字搜尋取得 metadata，再去 YouTube 找對應音源。
        失敗回 []。
        """
        if not self._spotify:
            return []

        # Step 1：Spotify 搜尋
        try:
            sp = await asyncio.to_thread(
                self._spotify.search, q=query, type='track', limit=max_results,
            )
            tracks = sp.get('tracks', {}).get('items', [])
        except Exception as e:
            print(f"[Music] Spotify 文字搜尋失敗：{e}")
            return []

        # Step 2：對每個 Spotify 結果用 YouTube 找音源
        results = []
        for t in tracks:
            track_name = t.get('name', '')
            artists    = ', '.join(a['name'] for a in t.get('artists', []))
            yt_q       = f"{artists} {track_name}".strip()

            yt_results = await self.search_youtube(yt_q, max_results=1, scored=False)
            if not yt_results:
                continue
            yt = yt_results[0]

            sp_thumb = ''
            if t.get('album', {}).get('images'):
                sp_thumb = t['album']['images'][0]['url']

            results.append({
                'title':       f"{artists} - {track_name}",
                'url':         yt['url'],
                'webpage_url': yt['webpage_url'],
                'duration':    (t.get('duration_ms', 0) // 1000) or yt.get('duration', 0),
                'uploader':    artists or yt.get('uploader', '未知'),
                'thumbnail':   sp_thumb or yt.get('thumbnail', ''),
                'source_type': 'spotify_search',
                'display_source': 'Spotify',
            })
        return results

    # ════════════════════════════════════════════════════════
    #  取得實際串流 URL（含錯誤分類）
    # ════════════════════════════════════════════════════════
    async def _extract_stream(self, youtube_url: str) -> tuple[Optional[str], Optional[str]]:
        """
        回傳 (stream_url, error_category)。
        成功：(url, None)；失敗：(None, 分類字串)
        """
        def _do():
            with yt_dlp.YoutubeDL(_stream_opts()) as ydl:
                info = ydl.extract_info(youtube_url, download=False)
                if info and 'entries' in info and info['entries']:
                    info = info['entries'][0]
                return (info or {}).get('url')
        try:
            url = await asyncio.get_event_loop().run_in_executor(None, _do)
            if url:
                return url, None
            return None, 'no_playable_format'
        except Exception as e:
            cat = classify_ytdlp_error(e)
            print(f"[Music] 取得串流失敗（{youtube_url}）分類={cat}：{str(e)[:160]}")
            return None, cat

    async def get_stream_url(self, youtube_url: str) -> Optional[str]:
        """相容舊呼叫：只回 stream_url（失敗回 None）。"""
        url, _ = await self._extract_stream(youtube_url)
        return url

    async def verify_playable(self, item: dict) -> tuple[bool, str]:
        """
        ⭐ 選歌階段「輕量驗證」（疑難雜症 5.3 / 問題 A）：
        只檢查結構（有合法 URL、是單一影片），
        **不**在這裡預抓 stream URL —— 預抓會在雲端提早觸發 YouTube bot check，
        導致還沒播就被判失敗。實際能不能播放，留到 resolve_playable_stream。

        回傳 (ok, reason)。
        """
        webpage_url = item.get('webpage_url') or item.get('url') or ''
        if not webpage_url or not webpage_url.startswith('http'):
            return False, "結果沒有完整可播放的網址"

        if not _is_playable_video({
            **item,
            'webpage_url': webpage_url,
            'id': item.get('video_id') or item.get('id'),
        }):
            return False, "這個結果是頻道或播放清單，不是單一影片"

        return True, ""

    # ════════════════════════════════════════════════════════
    #  ⭐ 解析可播放串流（雲端核心：原版 → 同曲 fallback）
    # ════════════════════════════════════════════════════════
    async def resolve_playable_stream(self, song) -> dict:
        """
        把一首歌（QueuedSong 或 dict）解析成「真的能播的 stream URL」。

        流程（疑難雜症 §8）：
          1. 先試原始 URL
          2. 原始被擋（bot check / 不可用 / 無格式）→ 用『同一首歌』的
             其他版本當 fallback，但每個候選都要通過 same-song 檢查，
             寧可不播也不要播成別首歌
          3. 全部失敗 → 回傳清楚的 reason

        回傳 dict：
          {
            ok, stream_url,
            actual_title, actual_url, actual_uploader,
            fallback_used, fallback_reason,
            failure_category, failure_message,
            attempts,           # list[dict]
          }
        """
        def _g(obj, key, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        title           = _g(song, 'title') or ''
        webpage_url      = _g(song, 'webpage_url') or _g(song, 'url') or ''
        uploader        = _g(song, 'uploader') or ''
        requested_query = _g(song, 'requested_query') or ''
        duration        = _g(song, 'duration')

        result = {
            'ok': False, 'stream_url': None,
            'actual_title': title, 'actual_url': webpage_url,
            'actual_uploader': uploader,
            'fallback_used': False, 'fallback_reason': None,
            'failure_category': None, 'failure_message': None,
            'attempts': [],
        }

        # ── Step 1：原始 URL ──
        if webpage_url:
            stream_url, cat = await self._extract_stream(webpage_url)
            result['attempts'].append(
                {'stage': 'original', 'url': webpage_url, 'category': cat})
            if stream_url:
                result.update(ok=True, stream_url=stream_url)
                return result
            result['failure_category'] = cat
            # 不值得 fallback 的錯誤（例如未知）就不再亂找
            if cat not in _FALLBACK_WORTHY:
                result['failure_message'] = "這個版本無法取得音源"
                return result

        # ── Step 2：同曲 fallback ──
        queries = build_fallback_queries(requested_query, title, uploader)
        # 比對基準：使用者打字的歌名最準；若來源是 URL/空白，改用清乾淨的歌名
        if requested_query and not requested_query.lower().startswith('http'):
            match_query = requested_query
        else:
            match_query = clean_song_title(title)
        print(f"[Music] resolve fallback queries={queries} match_query={match_query!r}")

        seen_urls = {webpage_url}
        for q in queries:
            candidates = await self.search_youtube(q, max_results=5, scored=False)
            # 依 same-song 分數排序
            scored = []
            for c in candidates:
                c_url = c.get('webpage_url') or c.get('url') or ''
                if not c_url or c_url in seen_urls:
                    continue
                sc, reasons = score_same_song(
                    match_query, title,
                    c.get('title') or '', c.get('uploader') or '',
                    c.get('duration'),
                )
                scored.append((sc, c, reasons))
            scored.sort(key=lambda x: -x[0])

            for sc, c, reasons in scored:
                c_url = c.get('webpage_url') or c.get('url') or ''
                # 門檻：同曲分數 < 70 一律不採用（疑難雜症 7.5）
                if sc < 70:
                    result['attempts'].append(
                        {'stage': 'fallback_reject', 'url': c_url,
                         'score': sc, 'reasons': reasons})
                    continue
                seen_urls.add(c_url)
                stream_url, cat = await self._extract_stream(c_url)
                result['attempts'].append(
                    {'stage': 'fallback_try', 'url': c_url,
                     'score': sc, 'category': cat})
                if stream_url:
                    result.update(
                        ok=True, stream_url=stream_url,
                        actual_title=c.get('title') or title,
                        actual_url=c_url,
                        actual_uploader=c.get('uploader') or '',
                        fallback_used=True,
                        fallback_reason=result['failure_category'] or 'original_failed',
                    )
                    return result

        # ── 全部失敗 ──
        if not result['failure_category']:
            result['failure_category'] = 'no_candidate'
        result['failure_message'] = (
            "找不到夠吻合且可播放的版本"
        )
        return result

    # ════════════════════════════════════════════════════════
    #  Discord 音訊來源
    # ════════════════════════════════════════════════════════
    def get_discord_audio_source(self, stream_url: str):
        import discord
        return discord.FFmpegPCMAudio(
            stream_url, executable=self.ffmpeg_path, **_FFMPEG_OPTS,
        )
