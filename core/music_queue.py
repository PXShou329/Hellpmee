"""
音樂佇列管理（v2.1）
─────────────────────────────────────────────────────────────────
每個伺服器一個 GuildMusicQueue。
特性：
    1. 佇列管理（add / pop / clear / list）
    2. 循環模式（off / single / queue）
    3. 點歌者資訊（requester_id, requester_name）
    4. 上限保護（MUSIC_QUEUE_MAX）

不負責：
    ❌ 實際播放（那是 Discord adapter 的工作）
    ❌ 取串流網址（那是 MusicEngine 的工作）

只是純粹的資料結構 + 流程控制。
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from config import Config


# ════════════════════════════════════════════════════════════════
#  ⭐ 全域 duration 格式化 helper（v2.1.4 新增）
#  根因：yt-dlp 有時回傳 float duration（例如 225.0），用 :02d 會炸
#         → Unknown format code 'd' for object of type 'float'
#  解法：所有顯示時長的地方都呼叫這個，不要在各處手寫 :02d
# ════════════════════════════════════════════════════════════════
def format_duration(duration) -> str:
    """
    把任何形式的 duration 轉成 'M:SS' 或 'H:MM:SS' 字串。

    支援：
        None         → '未知'
        int / float  → 正常格式化
        str          → 嘗試轉 float，失敗回 '未知'
        負數 / 0     → '0:00'
    """
    if duration is None:
        return "未知"
    try:
        total_seconds = int(float(duration))
    except (TypeError, ValueError):
        return "未知"
    if total_seconds < 0:
        total_seconds = 0

    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class LoopMode(Enum):
    OFF    = "off"      # 不循環
    SINGLE = "single"   # 單曲循環
    QUEUE  = "queue"    # 佇列循環

    def emoji(self) -> str:
        return {"off": "➡️", "single": "🔂", "queue": "🔁"}[self.value]

    def display_name(self) -> str:
        return {"off": "關閉", "single": "單曲循環", "queue": "歌單循環"}[self.value]


@dataclass
class QueuedSong:
    """佇列中的一首歌"""
    title:           str
    url:             str
    webpage_url:     str
    duration:        int                       # 秒（可能是 float，display 用 format_duration）
    uploader:        str
    requester_id:    int
    requester_name:  str
    source_type:     str
    thumbnail:       str = ""
    display_source:  str = "YouTube"
    # ⭐ 雲端音樂重構：保留「使用者真正想要的」與「實際播放的」（疑難雜症 7.3）
    requested_query:     str  = ""    # 使用者原始輸入（歌名或 URL）
    actual_played_title: str  = ""    # fallback 後實際播放的版本標題
    actual_played_url:   str  = ""    # fallback 後實際播放的 URL
    fallback_used:       bool = False
    fallback_reason:     str  = ""
    created_at:      datetime = field(default_factory=datetime.now)

    def duration_str(self) -> str:
        """轉介給全域 format_duration（避免 float :02d 炸）"""
        return format_duration(self.duration)

    @classmethod
    def from_dict(cls, d: dict, requester_id: int, requester_name: str) -> "QueuedSong":
        """從 music_engine 回傳的 dict 建立 QueuedSong（全欄位防 None）"""
        return cls(
            title=d.get('title') or '未知曲目',
            url=d.get('url') or '',
            webpage_url=d.get('webpage_url') or d.get('url') or '',
            duration=d.get('duration') or 0,         # ⚠️ 可能是 float，但 format_duration 會處理
            uploader=d.get('uploader') or '未知頻道',
            requester_id=requester_id,
            requester_name=requester_name or '匿名',
            source_type=d.get('source_type') or 'youtube_search',
            thumbnail=d.get('thumbnail') or '',
            display_source=d.get('display_source') or 'YouTube',
            requested_query=d.get('requested_query') or '',
        )


class GuildMusicQueue:
    """單一伺服器的音樂佇列狀態"""

    def __init__(self, guild_id: int):
        self.guild_id     = guild_id
        self._queue: deque[QueuedSong] = deque()
        self.current: Optional[QueuedSong] = None
        self.loop_mode    = LoopMode.OFF
        self.vc           = None   # discord.VoiceClient（由 adapter 設定）
        # ⭐ v2.2.0：本回合播放歷史（從首播到佇列清空算一回合）
        self.played_history: list = []
        self._MAX_HISTORY = 50

    def record_played(self, song):
        """成功開始播放一首時呼叫（規格六.1）"""
        # 連續相同合併（規格六.2）
        if self.played_history and self.played_history[-1].get("title") == (song.title if hasattr(song, "title") else song.get("title")):
            self.played_history[-1]["count"] += 1
            return
        title = song.title if hasattr(song, "title") else song.get("title", "未知曲目")
        self.played_history.append({"title": title, "count": 1})
        # 上限 50
        if len(self.played_history) > self._MAX_HISTORY:
            self.played_history = self.played_history[-self._MAX_HISTORY:]

    def reset_history(self):
        self.played_history = []

    # ════════════════════════════════════════════════════════
    #  佇列操作
    # ════════════════════════════════════════════════════════
    def add(self, song: QueuedSong) -> bool:
        """加入佇列最尾端。回傳 False 代表佇列已滿"""
        if len(self._queue) >= Config.MUSIC_QUEUE_MAX:
            return False
        self._queue.append(song)
        return True

    def add_front(self, song: QueuedSong) -> bool:
        """⭐ 插播：把歌曲塞到佇列最前面（v2.1.7 新增）"""
        if len(self._queue) >= Config.MUSIC_QUEUE_MAX:
            return False
        self._queue.appendleft(song)
        return True

    def add_many(self, songs: list[QueuedSong]) -> int:
        """批次加入。回傳實際加入的數量"""
        added = 0
        for s in songs:
            if self.add(s):
                added += 1
            else:
                break
        return added

    def pop_next(self) -> Optional[QueuedSong]:
        """取出下一首要播放的歌"""
        if not self._queue:
            return None
        return self._queue.popleft()

    def clear(self):
        self._queue.clear()

    def remove_at(self, index: int) -> Optional[QueuedSong]:
        """移除佇列中指定位置（1-based）"""
        if 1 <= index <= len(self._queue):
            song = self._queue[index - 1]
            del self._queue[index - 1]
            return song
        return None

    # ════════════════════════════════════════════════════════
    #  查詢
    # ════════════════════════════════════════════════════════
    @property
    def is_empty(self) -> bool:
        return len(self._queue) == 0 and self.current is None

    def is_idle(self) -> bool:
        """
        播放器是不是 idle（沒在播、沒暫停、current 也是 None）。
        ⭐ 規格 II：判斷『該不該開播』時用這個，不要用 queue 長度。
                   而且要在 q.add() 之前呼叫。
        """
        if self.current is not None:
            return False
        if self.vc is None:
            return True
        if not self.vc.is_connected():
            return True
        if self.vc.is_playing():
            return False
        if self.vc.is_paused():
            return False
        return True

    @property
    def total(self) -> int:
        return len(self._queue) + (1 if self.current else 0)

    @property
    def upcoming(self) -> list[QueuedSong]:
        return list(self._queue)

    def __len__(self):
        return len(self._queue)

    # ════════════════════════════════════════════════════════
    #  循環模式
    # ════════════════════════════════════════════════════════
    def cycle_loop_mode(self) -> LoopMode:
        """切換到下一個循環模式：off → single → queue → off"""
        order = [LoopMode.OFF, LoopMode.SINGLE, LoopMode.QUEUE]
        idx   = order.index(self.loop_mode)
        self.loop_mode = order[(idx + 1) % len(order)]
        return self.loop_mode

    def get_next_song(self) -> Optional[QueuedSong]:
        """
        根據循環模式決定下一首要播什麼。
        adapter 在 after_play callback 裡會呼叫這個。
        """
        if self.loop_mode == LoopMode.SINGLE and self.current:
            return self.current
        if self.loop_mode == LoopMode.QUEUE and self.current:
            self._queue.append(self.current)
        return self.pop_next()


# ════════════════════════════════════════════════════════════════
#  全域管理器：guild_id → GuildMusicQueue
# ════════════════════════════════════════════════════════════════
class QueueManager:
    def __init__(self):
        self._queues: dict[int, GuildMusicQueue] = {}

    def get_or_create(self, guild_id: int) -> GuildMusicQueue:
        if guild_id not in self._queues:
            self._queues[guild_id] = GuildMusicQueue(guild_id)
        return self._queues[guild_id]

    def remove(self, guild_id: int):
        self._queues.pop(guild_id, None)
