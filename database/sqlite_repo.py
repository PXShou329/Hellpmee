"""
SQLite 實作（v2.1）
"""
import json
from datetime import datetime
from typing import Optional

import aiosqlite

from database.base import BaseRepository
from database.models import FoodSearchHistory, ReminderEntry


class SQLiteRepository(BaseRepository):

    def __init__(self, path: str):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            # /吃什麼 紀錄
            await db.execute('''
                CREATE TABLE IF NOT EXISTS food_search_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL, food_keyword TEXT NOT NULL,
                    lat REAL, lng REAL, results TEXT,
                    searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )''')
            await db.execute(
                'CREATE INDEX IF NOT EXISTS idx_food_user ON food_search_history(user_id, searched_at DESC)'
            )

            # /提醒醒
            await db.execute('''
                CREATE TABLE IF NOT EXISTS reminders (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    channel_id  INTEGER NOT NULL,
                    task        TEXT NOT NULL,
                    target_time TIMESTAMP NOT NULL
                )''')
            await db.execute(
                'CREATE INDEX IF NOT EXISTS idx_reminders_time ON reminders(target_time)'
            )
            await db.commit()

    async def close(self):
        pass

    # ── /吃什麼 ──
    async def save_food_search(self, history: FoodSearchHistory) -> int:
        results_json = json.dumps(history.results, ensure_ascii=False)
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                'INSERT INTO food_search_history (user_id, food_keyword, lat, lng, results) VALUES (?,?,?,?,?)',
                (history.user_id, history.food_keyword, history.lat, history.lng, results_json)
            )
            await db.commit()
            return cur.lastrowid

    async def get_food_history(self, user_id: str, limit: int = 10) -> list[FoodSearchHistory]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM food_search_history WHERE user_id = ? ORDER BY searched_at DESC LIMIT ?',
                (user_id, limit)
            ) as cur:
                rows = await cur.fetchall()
        return [FoodSearchHistory(
            id=r['id'], user_id=r['user_id'], food_keyword=r['food_keyword'],
            lat=r['lat'], lng=r['lng'],
            results=json.loads(r['results']) if r['results'] else [],
            searched_at=_parse_dt(r['searched_at'])
        ) for r in rows]

    # ── /提醒醒 ──
    async def add_reminder(self, reminder: ReminderEntry) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                'INSERT INTO reminders (user_id, channel_id, task, target_time) VALUES (?,?,?,?)',
                (reminder.user_id, reminder.channel_id, reminder.task, reminder.target_time)
            )
            await db.commit()
            return cur.lastrowid

    async def get_due_reminders(self, now: datetime) -> list[ReminderEntry]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM reminders WHERE target_time <= ?', (now,)
            ) as cur:
                rows = await cur.fetchall()
        return [ReminderEntry(
            id=r['id'], user_id=r['user_id'], channel_id=r['channel_id'],
            task=r['task'], target_time=_parse_dt(r['target_time'])
        ) for r in rows]

    async def delete_reminder(self, reminder_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute('DELETE FROM reminders WHERE id = ?', (reminder_id,))
            await db.commit()


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
