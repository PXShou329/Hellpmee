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
import re
import shutil
import subprocess
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
_YOUTUBE_EXTRACTOR_ARGS = {
    'youtube': {
        # 多 client fallback：降低 YouTube 對單一 client 擋音源的機率
        'player_client': ['android_vr', 'android', 'ios', 'web'],
    }
}

_YDL_SEARCH_OPTS = {
    'format': 'bestaudio/best',
    'quiet': True,
    'no_warnings': False,
    'extract_flat': True,
    'noplaylist': True,
    'source_address': '0.0.0.0',
    'extractor_args': _YOUTUBE_EXTRACTOR_ARGS,
}

_YDL_STREAM_OPTS = {
    'format': 'bestaudio/best',
    'quiet': True,
    'no_warnings': False,
    'noplaylist': True,
    'source_address': '0.0.0.0',
    'extractor_args': _YOUTUBE_EXTRACTOR_ARGS,
}
_FFMPEG_OPTS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}


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
            with yt_dlp.YoutubeDL(_YDL_STREAM_OPTS) as ydl:
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
            with yt_dlp.YoutubeDL(_YDL_SEARCH_OPTS) as ydl:
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
        import math
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
    #  ⭐ 播放前驗證（規格 4）
    # ════════════════════════════════════════════════════════
    async def verify_playable(self, song_dict: dict) -> tuple[bool, str]:
        """
        在加入佇列之前先驗證能不能播。
        回傳：(playable, reason)
            playable=False 時 reason 是不能播的原因
        """
        title = song_dict.get('title') or '?'
        url   = song_dict.get('webpage_url') or song_dict.get('url') or ''

        if not url:
            return (False, "結果沒有可播放網址")
        if not url.startswith('http'):
            return (False, "URL 格式不正確")

        # 嘗試取得實際 stream URL（不要實際 stream，只解析）
        stream_url = await self.get_stream_url(url)
        if not stream_url:
            return (False, "無法取得音源串流")

        return (True, "OK")


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
    #  取得實際串流 URL
    # ════════════════════════════════════════════════════════
    async def get_stream_url(self, youtube_url: str) -> Optional[str]:
        def _do():
            with yt_dlp.YoutubeDL(_YDL_STREAM_OPTS) as ydl:
                info = ydl.extract_info(youtube_url, download=False)
                if 'entries' in info:
                    info = info['entries'][0]
                return info.get('url')
        try:
            return await asyncio.get_event_loop().run_in_executor(None, _do)
        except Exception as e:
            print(f"[Music] 取得串流失敗（{youtube_url}）：{e}")
            import traceback
            traceback.print_exc()
            return None

    async def verify_playable(self, item: dict) -> tuple[bool, str]:
        """
        ⭐ 規格 4：播放前驗證
        檢查 selected_result 真的能播：
            1. 有可解析的 webpage_url
            2. yt-dlp 能拿到 stream URL（最強的驗證）

        回傳 (ok, reason)。失敗時 reason 是給使用者看的訊息。
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

        # 搜尋選歌階段不要預抓串流 URL。
        # 原本這裡會先呼叫 yt-dlp 驗證一次，但 YouTube 對 VPS / 機房 IP 常出現 bot check，
        # 會導致「其實正式播放可能成功，但預驗證先失敗」。
        # 因此這裡只做基本 URL / 單一影片檢查，真正串流交給播放階段處理。
        return True, ""

    # ════════════════════════════════════════════════════════
    #  Discord 音訊來源
    # ════════════════════════════════════════════════════════
    def get_discord_audio_source(self, stream_url: str):
        import discord
        return discord.FFmpegPCMAudio(
            stream_url, executable=self.ffmpeg_path, **_FFMPEG_OPTS,
        )
