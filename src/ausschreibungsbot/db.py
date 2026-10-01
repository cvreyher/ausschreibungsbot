import json
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guid TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    place TEXT,
    authority TEXT,
    deadline TEXT,
    published TEXT,
    search_term TEXT,
    details TEXT,
    notice_url TEXT,
    status TEXT NOT NULL DEFAULT 'neu',
    score INTEGER,
    summary TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    thread_id TEXT NOT NULL,
    interrupt_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    tender_id INTEGER,
    message_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Status-Lebenszyklus einer Ausschreibung
STATUSES = (
    "neu",  # gefunden, noch nicht bewertet
    "irrelevant",  # vom Analyst aussortiert
    "gemeldet",  # per Telegram vorgeschlagen
    "ignoriert",  # vom Nutzer verworfen
    "in_bearbeitung",  # Delegate arbeitet an der Bewerbung
    "freigegeben",  # Nutzer hat Angebot freigegeben (nur per Button!)
    "bereit_zur_abgabe",  # Unterlagen fertig, manuelle Abgabe nötig
    "abgegeben",
    "verworfen",
)


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # --- Ausschreibungen ---

    async def insert_tender(self, t: dict[str, Any]) -> int | None:
        """Legt eine Ausschreibung an. Gibt die neue ID zurück oder None, falls schon bekannt."""
        cur = await self.conn.execute(
            """INSERT OR IGNORE INTO tenders (guid, title, url, place, authority, deadline, published, search_term)
               VALUES (:guid, :title, :url, :place, :authority, :deadline, :published, :search_term)""",
            t,
        )
        await self.conn.commit()
        return cur.lastrowid if cur.rowcount else None

    async def update_tender(self, tender_id: int, **fields: Any) -> None:
        if "details" in fields and not isinstance(fields["details"], str):
            fields["details"] = json.dumps(fields["details"], ensure_ascii=False)
        cols = ", ".join(f"{k} = :{k}" for k in fields)
        await self.conn.execute(f"UPDATE tenders SET {cols} WHERE id = :id", {**fields, "id": tender_id})
        await self.conn.commit()

    async def get_tender(self, tender_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM tenders WHERE id = ?", (tender_id,))
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["details"] = json.loads(d["details"]) if d["details"] else {}
        return d

    async def list_tenders(self, status: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        if status:
            cur = await self.conn.execute(
                "SELECT * FROM tenders WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM tenders WHERE status != 'irrelevant' ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in await cur.fetchall()]

    # --- Offene Rückfragen (LangGraph-Interrupts) ---

    async def add_pending(
        self, chat_id: int, thread_id: str, interrupt_id: str, kind: str, tender_id: int | None, message_id: int
    ) -> None:
        await self.conn.execute(
            "INSERT INTO pending (chat_id, thread_id, interrupt_id, kind, tender_id, message_id) VALUES (?,?,?,?,?,?)",
            (chat_id, thread_id, interrupt_id, kind, tender_id, message_id),
        )
        await self.conn.commit()

    async def pending_by_message(self, chat_id: int, message_id: int) -> dict | None:
        cur = await self.conn.execute(
            "SELECT * FROM pending WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def latest_pending(self, chat_id: int, kinds: tuple[str, ...]) -> dict | None:
        marks = ",".join("?" * len(kinds))
        cur = await self.conn.execute(
            f"SELECT * FROM pending WHERE chat_id = ? AND kind IN ({marks}) ORDER BY id DESC LIMIT 1",
            (chat_id, *kinds),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def pending_by_id(self, pending_id: int) -> dict | None:
        cur = await self.conn.execute("SELECT * FROM pending WHERE id = ?", (pending_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_pending_kind(self, pending_id: int, kind: str) -> None:
        await self.conn.execute("UPDATE pending SET kind = ? WHERE id = ?", (kind, pending_id))
        await self.conn.commit()

    async def clear_pending(self, thread_id: str) -> None:
        await self.conn.execute("DELETE FROM pending WHERE thread_id = ?", (thread_id,))
        await self.conn.commit()

    async def clear_all_pending(self, chat_id: int) -> None:
        await self.conn.execute("DELETE FROM pending WHERE chat_id = ?", (chat_id,))
        await self.conn.commit()

    # --- Key/Value ---

    async def kv_get(self, key: str) -> str | None:
        cur = await self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def kv_set(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()
