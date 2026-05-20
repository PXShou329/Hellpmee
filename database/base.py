"""
資料庫抽象介面（v2.1）
─────────────────────────────────────────────────────────────────
只保留：
    /吃什麼 紀錄（food_search_history）
    /提醒醒  （reminders）
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from database.models import FoodSearchHistory, ReminderEntry


class BaseRepository(ABC):

    @abstractmethod
    async def init(self): ...
    @abstractmethod
    async def close(self): ...

    # ── /吃什麼 紀錄 ───────────────────────────────────────
    @abstractmethod
    async def save_food_search(self, history: FoodSearchHistory) -> int: ...
    @abstractmethod
    async def get_food_history(self, user_id: str, limit: int = 10) -> list[FoodSearchHistory]: ...

    # ── /提醒醒 ────────────────────────────────────────────
    @abstractmethod
    async def add_reminder(self, reminder: ReminderEntry) -> int: ...
    @abstractmethod
    async def get_due_reminders(self, now: datetime) -> list[ReminderEntry]: ...
    @abstractmethod
    async def delete_reminder(self, reminder_id: int): ...
