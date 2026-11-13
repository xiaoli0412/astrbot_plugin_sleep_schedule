"""夜间留言落库（SQLite）。

为什么必须自己落库：在 ``on_llm_request`` 里 ``event.stop_event()`` 会让
``internal.py`` 直接 return，连带跳过 ``_save_to_history``，所以这些消息不会
进对话历史。这里单独存一份，早上回放时再拼进 prompt。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  umo TEXT NOT NULL, ts REAL NOT NULL,
  text TEXT, kind TEXT DEFAULT 'text',
  replayed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pending_umo ON pending (umo, replayed);
CREATE INDEX IF NOT EXISTS idx_pending_ts ON pending (ts);
"""


class PendingStore:
    """极简 SQLite 存储；每次操作独立连接，所有阻塞调用丢到线程池。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._init_lock = asyncio.Lock()
        self._ready = False

    # ------------------------------------------------------------------ 基础
    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_sync(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    async def init(self) -> None:
        async with self._init_lock:
            if self._ready:
                return
            await asyncio.to_thread(self._init_sync)
            self._ready = True

    # ------------------------------------------------------------------ 写入
    def _add_sync(self, umo: str, ts: float, text: str, kind: str) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO pending (umo, ts, text, kind, replayed) VALUES (?, ?, ?, ?, 0)",
                (umo, ts, text, kind),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()

    async def add(self, umo: str, ts: float, text: str, kind: str = "text") -> int:
        await self.init()
        return await asyncio.to_thread(self._add_sync, umo, ts, text, kind)

    def _mark_sync(self, ids: list[int]) -> int:
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        conn = self._connect()
        try:
            cur = conn.execute(
                f"UPDATE pending SET replayed = 1 WHERE id IN ({placeholders})",
                ids,
            )
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()

    async def mark_replayed(self, ids: list[int]) -> int:
        await self.init()
        return await asyncio.to_thread(self._mark_sync, list(ids))

    # ------------------------------------------------------------------ 查询
    def _pending_umos_sync(self) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT umo FROM pending WHERE replayed = 0 GROUP BY umo ORDER BY MIN(ts)"
            ).fetchall()
            return [str(r["umo"]) for r in rows]
        finally:
            conn.close()

    async def pending_umos(self) -> list[str]:
        await self.init()
        return await asyncio.to_thread(self._pending_umos_sync)

    def _fetch_sync(self, umo: str, limit: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, umo, ts, text, kind FROM pending "
                "WHERE umo = ? AND replayed = 0 ORDER BY ts ASC LIMIT ?",
                (umo, int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def fetch_pending(self, umo: str, limit: int = 20) -> list[dict[str, Any]]:
        await self.init()
        return await asyncio.to_thread(self._fetch_sync, umo, limit)

    def _count_sync(self, umo: str | None) -> int:
        conn = self._connect()
        try:
            if umo:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM pending WHERE replayed = 0 AND umo = ?",
                    (umo,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM pending WHERE replayed = 0"
                ).fetchone()
            return int(row["c"] if row else 0)
        finally:
            conn.close()

    async def count(self, umo: str | None = None) -> int:
        await self.init()
        return await asyncio.to_thread(self._count_sync, umo)

    def _recent_sync(self, limit: int, include_replayed: bool) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            where = "" if include_replayed else "WHERE replayed = 0"
            rows = conn.execute(
                f"SELECT id, umo, ts, text, kind, replayed FROM pending {where} "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def recent(self, limit: int = 10, include_replayed: bool = False) -> list[dict[str, Any]]:
        await self.init()
        return await asyncio.to_thread(self._recent_sync, limit, include_replayed)

    # ------------------------------------------------------------------ 清理
    def _purge_sync(self, older_than_ts: float) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM pending WHERE replayed = 1 AND ts < ?", (older_than_ts,)
            )
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()

    async def purge_replayed(self, keep_days: int = 30) -> int:
        await self.init()
        cutoff = time.time() - max(1, int(keep_days)) * 86400
        return await asyncio.to_thread(self._purge_sync, cutoff)
