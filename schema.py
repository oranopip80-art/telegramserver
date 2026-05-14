"""
schema.py — Pydantic models for API I/O and the SQLite session registry.
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import Optional

import aiosqlite
from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════════════════════
#  Enums
# ═══════════════════════════════════════════════════════════════════════════════

class SessionStatus(str, Enum):
    PENDING_CODE = "pending_code"
    PENDING_2FA = "pending_2fa"
    ACTIVE = "active"
    TERMINATED = "terminated"
    ERROR = "error"


class AuthPhase(str, Enum):
    IDLE = "idle"
    CODE_SENT = "code_sent"
    AWAITING_2FA = "awaiting_2fa"
    COMPLETE = "complete"


# ═══════════════════════════════════════════════════════════════════════════════
#  API Request Models
# ═══════════════════════════════════════════════════════════════════════════════

class PhoneRequest(BaseModel):
    """Initiate registration — submit a phone number."""
    phone: str = Field(..., description="Phone number in international format, e.g. +12025551234")


class CodeSubmit(BaseModel):
    """Submit the login code received via Telegram/SMS."""
    phone: str
    code: str = Field(..., description="The 5-digit login code")


class TwoFASubmit(BaseModel):
    """Submit the 2FA password (if enabled on the account)."""
    phone: str
    password: str


class PasswordSetRequest(BaseModel):
    """Set or update the 2FA password on an account."""
    phone: str
    new_password: str
    current_password: Optional[str] = None


class MessageQuery(BaseModel):
    """Query messages from a session."""
    phone: str
    chat: Optional[str] = Field(
        default="Telegram",
        description="Chat/channel name or ID to query. Defaults to 'Telegram' service channel.",
    )
    limit: int = Field(default=15, ge=1, le=100)
    keyword: Optional[str] = Field(
        default=None,
        description="Optional keyword filter (e.g. 'Login code', 'OTP').",
    )


class SearchQuery(BaseModel):
    """Search across all chats or a specific chat for keywords."""
    phone: str
    keyword: str
    chat: Optional[str] = None
    limit: int = Field(default=20, ge=1, le=100)


# ═══════════════════════════════════════════════════════════════════════════════
#  API Response Models
# ═══════════════════════════════════════════════════════════════════════════════

class StatusResponse(BaseModel):
    success: bool
    message: str
    phase: Optional[AuthPhase] = None
    data: Optional[dict] = None


class SessionInfo(BaseModel):
    phone: str
    status: str
    device_model: str
    app_version: str
    created_at: Optional[str] = None
    last_active: Optional[str] = None
    has_2fa: bool = False
    two_fa_password: Optional[str] = None
    spam_status: Optional[str] = None
    spam_response: Optional[str] = None


class MessageItem(BaseModel):
    message_id: int
    chat_name: str
    sender: Optional[str] = None
    text: Optional[str] = None
    date: str


class MessageResponse(BaseModel):
    success: bool
    phone: str
    messages: list[MessageItem] = []
    count: int = 0


# ═══════════════════════════════════════════════════════════════════════════════
#  SQLite Session Registry
# ═══════════════════════════════════════════════════════════════════════════════

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    phone           TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'pending_code',
    session_file    TEXT,
    device_model    TEXT,
    app_version     TEXT,
    system_version  TEXT,
    has_2fa         INTEGER DEFAULT 0,
    spam_status     TEXT,
    spam_response   TEXT,
    created_at      TEXT NOT NULL,
    last_active     TEXT,
    notes           TEXT
);
"""

MIGRATE_SPAM_COLS = [
    "ALTER TABLE sessions ADD COLUMN spam_status TEXT",
    "ALTER TABLE sessions ADD COLUMN spam_response TEXT",
]


class SessionDB:
    """Async SQLite wrapper for session registry."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(CREATE_TABLE_SQL)
        for sql in MIGRATE_SPAM_COLS:
            try:
                await self._db.execute(sql)
            except Exception:
                pass
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    async def upsert_session(
        self,
        phone: str,
        status: SessionStatus,
        session_file: str = "",
        device_model: str = "",
        app_version: str = "",
        system_version: str = "",
        has_2fa: bool = False,
    ):
        now = datetime.datetime.utcnow().isoformat()
        await self._db.execute(
            """
            INSERT INTO sessions (phone, status, session_file, device_model, app_version, system_version, has_2fa, created_at, last_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                status       = excluded.status,
                session_file = COALESCE(NULLIF(excluded.session_file, ''), sessions.session_file),
                device_model = COALESCE(NULLIF(excluded.device_model, ''), sessions.device_model),
                app_version  = COALESCE(NULLIF(excluded.app_version, ''), sessions.app_version),
                system_version = COALESCE(NULLIF(excluded.system_version, ''), sessions.system_version),
                has_2fa      = excluded.has_2fa,
                last_active  = excluded.last_active
            """,
            (phone, status.value, session_file, device_model, app_version, system_version, int(has_2fa), now, now),
        )
        await self._db.commit()

    async def get_session(self, phone: str) -> Optional[dict]:
        cursor = await self._db.execute("SELECT * FROM sessions WHERE phone = ?", (phone,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_sessions(self) -> list[dict]:
        cursor = await self._db.execute("SELECT * FROM sessions ORDER BY last_active DESC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_status(self, phone: str, status: SessionStatus):
        now = datetime.datetime.utcnow().isoformat()
        await self._db.execute(
            "UPDATE sessions SET status = ?, last_active = ? WHERE phone = ?",
            (status.value, now, phone),
        )
        await self._db.commit()

    async def delete_session(self, phone: str):
        await self._db.execute("DELETE FROM sessions WHERE phone = ?", (phone,))
        await self._db.commit()

    async def update_spam_status(self, phone: str, spam_status: str, spam_response: str = ""):
        now = datetime.datetime.utcnow().isoformat()
        await self._db.execute(
            "UPDATE sessions SET spam_status = ?, spam_response = ?, last_active = ? WHERE phone = ?",
            (spam_status, spam_response[:1000], now, phone),
        )
        await self._db.commit()
