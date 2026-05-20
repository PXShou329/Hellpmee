"""
PostgreSQL 實作（v2.1）
"""
import json
from datetime import datetime
from typing import Optional

import asyncpg

from database.base import BaseRepository
from database.models import FoodSearchHistory, ReminderEntry


class PostgresRepository(BaseRepository):

    def __init__(self, dsn: str):
        self.dsn   = dsn
        self._pool: Optional[asyncpg.Pool] = None

    async def init(self):
        self._pool = await asyncpg.create_pool(self.dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS food_search_history (
                    id SERIAL PRIMARY KEY, user_id TEXT NOT NULL,
                    food_keyword TEXT NOT NULL, lat REAL, lng REAL, results TEXT,
                    searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )''')
            await conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_food_user ON food_search_history(user_id, searched_at DESC)'
            )
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS reminders (
                    id          SERIAL PRIMARY KEY,
                    user_id     BIGINT NOT NULL,
                    channel_id  BIGINT NOT NULL,
                    task        TEXT NOT NULL,
                    target_time TIMESTAMP NOT NULL
                )''')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_reminders_time ON reminders(target_time)')

    async def close(self):
        if self._pool:
            await self._pool.close()

    async def save_food_search(self, history: FoodSearchHistory) -> int:
        results_json = json.dumps(history.results, ensure_ascii=False)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO food_search_history (user_id, food_keyword, lat, lng, results) VALUES ($1,$2,$3,$4,$5) RETURNING id',
                history.user_id, history.food_keyword, history.lat, history.lng, results_json
            )
            return row['id']

    async def get_food_history(self, user_id: str, limit: int = 10) -> list[FoodSearchHistory]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT * FROM food_search_history WHERE user_id = $1 ORDER BY searched_at DESC LIMIT $2',
                user_id, limit
            )
        return [FoodSearchHistory(
            id=r['id'], user_id=r['user_id'], food_keyword=r['food_keyword'],
            lat=r['lat'], lng=r['lng'],
            results=json.loads(r['results']) if r['results'] else [],
            searched_at=r['searched_at']
        ) for r in rows]

    async def add_reminder(self, reminder: ReminderEntry) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO reminders (user_id, channel_id, task, target_time) VALUES ($1,$2,$3,$4) RETURNING id',
                reminder.user_id, reminder.channel_id, reminder.task, reminder.target_time
            )
            return row['id']

    async def get_due_reminders(self, now: datetime) -> list[ReminderEntry]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch('SELECT * FROM reminders WHERE target_time <= $1', now)
        return [ReminderEntry(
            id=r['id'], user_id=r['user_id'], channel_id=r['channel_id'],
            task=r['task'], target_time=r['target_time']
        ) for r in rows]

    async def delete_reminder(self, reminder_id: int):
        async with self._pool.acquire() as conn:
            await conn.execute('DELETE FROM reminders WHERE id = $1', reminder_id)
