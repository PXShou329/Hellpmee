"""
資料庫模型定義（v2.1 Clean Core Edition）
─────────────────────────────────────────────────────────────────
只保留兩張表：
    FoodSearchHistory  ── /吃什麼 用
    ReminderEntry      ── /提醒醒 用
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class FoodSearchHistory:
    user_id:      str
    food_keyword: str
    lat:          Optional[float] = None
    lng:          Optional[float] = None
    results:      list[dict] = field(default_factory=list)
    searched_at:  Optional[datetime] = None
    id:           Optional[int] = None


@dataclass
class ReminderEntry:
    user_id:     int
    channel_id:  int
    task:        str
    target_time: datetime
    id:          Optional[int] = None
