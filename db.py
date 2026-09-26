from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._initialized = False

    async def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS flows (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    step TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS status_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    jalali_date TEXT NOT NULL,
                    gregorian_date TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    summary_json TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS schedules (
                    chat_id INTEGER PRIMARY KEY,
                    hour INTEGER NOT NULL,
                    minute INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_date TEXT,
                    updated_at INTEGER NOT NULL
                )
            """)
            await db.commit()
        self._initialized = True

    async def set_flow(self, chat_id: int, user_id: int, step: str, data: dict[str, Any]) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO flows(chat_id,user_id,step,data_json,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(chat_id,user_id) DO UPDATE SET step=excluded.step,data_json=excluded.data_json,updated_at=excluded.updated_at",
                (chat_id, user_id, step, json.dumps(data, ensure_ascii=False), int(time.time())),
            )
            await db.commit()

    async def get_flow(self, chat_id: int, user_id: int) -> tuple[str, dict[str, Any]] | None:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT step,data_json FROM flows WHERE chat_id=? AND user_id=?", (chat_id, user_id)) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return row[0], json.loads(row[1])

    async def clear_flow(self, chat_id: int, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM flows WHERE chat_id=? AND user_id=?", (chat_id, user_id))
            await db.commit()

    async def save_status_run(self, chat_id: int, user_id: int, jalali_date: str, gregorian_date: str, summary: dict[str, Any]) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO status_runs(chat_id,user_id,jalali_date,gregorian_date,created_at,summary_json) VALUES(?,?,?,?,?,?)",
                (chat_id, user_id, jalali_date, gregorian_date, int(time.time()), json.dumps(summary, ensure_ascii=False)),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def get_last_status_run(self, chat_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT id,jalali_date,gregorian_date,created_at,summary_json FROM status_runs WHERE chat_id=? ORDER BY id DESC LIMIT 1",
                (chat_id,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "jalali_date": row[1],
            "gregorian_date": row[2],
            "created_at": row[3],
            "summary": json.loads(row[4]),
        }
    async def get_recent_status_runs(self, chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT id,jalali_date,gregorian_date,created_at,summary_json FROM status_runs WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                "id": row[0],
                "jalali_date": row[1],
                "gregorian_date": row[2],
                "created_at": row[3],
                "summary": json.loads(row[4]),
            }
            for row in rows
        ]

    async def set_schedule(self, chat_id: int, hour: int, minute: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO schedules(chat_id,hour,minute,enabled,last_run_date,updated_at) "
                "VALUES(?,?,?,1,NULL,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET hour=excluded.hour,minute=excluded.minute,"
                "enabled=1,last_run_date=NULL,updated_at=excluded.updated_at",
                (chat_id, hour, minute, int(time.time())),
            )
            await db.commit()

    async def disable_schedule(self, chat_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE schedules SET enabled=0, updated_at=? WHERE chat_id=?",
                (int(time.time()), chat_id),
            )
            await db.commit()

    async def get_schedule(self, chat_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT hour,minute,enabled,last_run_date FROM schedules WHERE chat_id=?",
                (chat_id,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return {"hour": row[0], "minute": row[1], "enabled": bool(row[2]), "last_run_date": row[3]}

    async def get_enabled_schedules(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT chat_id,hour,minute,last_run_date FROM schedules WHERE enabled=1"
            ) as cur:
                rows = await cur.fetchall()
        return [
            {"chat_id": row[0], "hour": row[1], "minute": row[2], "last_run_date": row[3]}
            for row in rows
        ]

    async def mark_schedule_run(self, chat_id: int, date_str: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE schedules SET last_run_date=? WHERE chat_id=?",
                (date_str, chat_id),
            )
            await db.commit()

