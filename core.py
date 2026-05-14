"""
core.py — Telethon-based engine for registration, session management,
           and message retrieval across multiple Telegram accounts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

import redis.asyncio as aioredis
from telethon import TelegramClient, errors, functions, types, events
from telethon.tl.functions.account import GetPasswordRequest

import config
from schema import (
    AuthPhase,
    MessageItem,
    SessionDB,
    SessionStatus,
)

logger = logging.getLogger("telegram_engine")


# ═══════════════════════════════════════════════════════════════════════════════
#  Redis Helper — stores transient auth state (phone_code_hash, phase, etc.)
# ═══════════════════════════════════════════════════════════════════════════════

class AuthStateStore:
    """Thin wrapper around Redis for per-phone auth state."""

    def __init__(self, redis_url: str):
        self._redis: Optional[aioredis.Redis] = None
        self._url = redis_url

    async def connect(self):
        self._redis = aioredis.from_url(self._url, decode_responses=True)

    async def close(self):
        if self._redis:
            await self._redis.aclose()

    def _key(self, phone: str) -> str:
        return f"tg:auth:{phone}"

    async def set_state(self, phone: str, data: dict, ttl: int = config.CODE_EXPIRY_SECONDS):
        await self._redis.set(self._key(phone), json.dumps(data), ex=ttl)

    async def get_state(self, phone: str) -> Optional[dict]:
        raw = await self._redis.get(self._key(phone))
        return json.loads(raw) if raw else None

    async def delete_state(self, phone: str):
        await self._redis.delete(self._key(phone))


# ═══════════════════════════════════════════════════════════════════════════════
#  Client Factory — creates Telethon clients with the spoofed device profile
# ═══════════════════════════════════════════════════════════════════════════════

def _session_path(phone: str) -> str:
    """Return the path (without extension) for a phone's .session file."""
    clean = phone.replace("+", "").replace(" ", "")
    return os.path.join(config.SESSIONS_DIR, clean)


def create_client(phone: str) -> TelegramClient:
    """Create a TelegramClient with the Latitude E4300 / TDesktop 5.1.4 identity."""
    session = _session_path(phone)
    client = TelegramClient(
        session,
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        device_model=config.DEVICE_MODEL,
        system_version=config.SYSTEM_VERSION,
        app_version=config.APP_VERSION,
        lang_code=config.LANG_CODE,
        system_lang_code=config.SYSTEM_LANG_CODE,
    )
    return client


# ═══════════════════════════════════════════════════════════════════════════════
#  Module A: Registration Engine
# ═══════════════════════════════════════════════════════════════════════════════

class RegistrationEngine:
    """Handles the full auth lifecycle: code request → sign-in → 2FA → persistence."""

    def __init__(self, db: SessionDB, store: AuthStateStore):
        self.db = db
        self.store = store
        self._clients: dict[str, TelegramClient] = {}  # phone → live client

    # ── Phase 1: Send Code ───────────────────────────────────────────────────

    async def request_code(self, phone: str) -> dict:
        """Initiate login — sends the Telegram auth code to the phone."""
        client = create_client(phone)
        await client.connect()
        self._clients[phone] = client

        try:
            sent = await client.send_code_request(phone)
        except errors.FloodWaitError as e:
            logger.warning(f"[{phone}] FloodWait {e.seconds}s on send_code_request")
            await client.disconnect()
            del self._clients[phone]
            return {
                "success": False,
                "message": f"Rate-limited. Retry after {e.seconds} seconds.",
                "retry_after": e.seconds,
            }
        except errors.PhoneNumberBannedError:
            await client.disconnect()
            del self._clients[phone]
            await self.db.upsert_session(phone, SessionStatus.ERROR)
            return {"success": False, "message": "Phone number is banned by Telegram."}
        except Exception as exc:
            await client.disconnect()
            del self._clients[phone]
            logger.exception(f"[{phone}] Unexpected error in send_code_request")
            return {"success": False, "message": str(exc)}

        # Persist transient state in Redis
        await self.store.set_state(phone, {
            "phone_code_hash": sent.phone_code_hash,
            "phase": AuthPhase.CODE_SENT.value,
        })

        await self.db.upsert_session(
            phone,
            SessionStatus.PENDING_CODE,
            device_model=config.DEVICE_MODEL,
            app_version=config.APP_VERSION,
            system_version=config.SYSTEM_VERSION,
        )

        logger.info(f"[{phone}] Code sent — awaiting user input")
        return {
            "success": True,
            "message": "Login code sent. Submit the code to proceed.",
            "phase": AuthPhase.CODE_SENT.value,
        }

    # ── Phase 3a: Submit Code ────────────────────────────────────────────────

    async def submit_code(self, phone: str, code: str) -> dict:
        """Complete sign-in with the received login code."""
        state = await self.store.get_state(phone)
        if not state or state.get("phase") != AuthPhase.CODE_SENT.value:
            return {"success": False, "message": "No pending code request found. Start over."}

        client = self._clients.get(phone)
        if not client or not client.is_connected():
            # Reconnect from saved session
            client = create_client(phone)
            await client.connect()
            self._clients[phone] = client

        phone_code_hash = state["phone_code_hash"]

        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        except errors.SessionPasswordNeededError:
            # 2FA is active — transition to 2FA phase
            await self.store.set_state(phone, {
                "phone_code_hash": phone_code_hash,
                "phase": AuthPhase.AWAITING_2FA.value,
            })
            await self.db.update_status(phone, SessionStatus.PENDING_2FA)
            logger.info(f"[{phone}] 2FA required")
            return {
                "success": True,
                "message": "Two-factor authentication required. Submit your 2FA password.",
                "phase": AuthPhase.AWAITING_2FA.value,
            }
        except errors.PhoneCodeInvalidError:
            return {"success": False, "message": "Invalid code. Please try again."}
        except errors.PhoneCodeExpiredError:
            await self.store.delete_state(phone)
            return {"success": False, "message": "Code expired. Request a new one."}
        except errors.FloodWaitError as e:
            return {
                "success": False,
                "message": f"Rate-limited. Retry after {e.seconds}s.",
                "retry_after": e.seconds,
            }

        # Success — finalize
        return await self._finalize_auth(phone, client)

    # ── Phase 3b: Submit 2FA Password ────────────────────────────────────────

    async def submit_2fa(self, phone: str, password: str) -> dict:
        """Complete sign-in with the 2FA password."""
        state = await self.store.get_state(phone)
        if not state or state.get("phase") != AuthPhase.AWAITING_2FA.value:
            return {"success": False, "message": "No pending 2FA request found."}

        client = self._clients.get(phone)
        if not client or not client.is_connected():
            client = create_client(phone)
            await client.connect()
            self._clients[phone] = client

        try:
            await client.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            return {"success": False, "message": "Incorrect 2FA password."}
        except errors.FloodWaitError as e:
            return {
                "success": False,
                "message": f"Rate-limited. Retry after {e.seconds}s.",
                "retry_after": e.seconds,
            }

        return await self._finalize_auth(phone, client)

    # ── Phase 4: Finalize — set 2FA, save session, update DB ─────────────────

    async def _finalize_auth(self, phone: str, client: TelegramClient) -> dict:
        """Post-login: set/update 2FA, persist session, update registry."""
        me = await client.get_me()
        logger.info(f"[{phone}] Authenticated as {me.first_name} (ID: {me.id})")

        # Attempt to set/update 2FA password
        two_fa_set = await self._set_2fa(client, phone)

        # Session file is automatically saved by Telethon
        session_file = _session_path(phone) + ".session"

        await self.db.upsert_session(
            phone,
            SessionStatus.ACTIVE,
            session_file=session_file,
            device_model=config.DEVICE_MODEL,
            app_version=config.APP_VERSION,
            system_version=config.SYSTEM_VERSION,
            has_2fa=two_fa_set,
        )
        await self.store.delete_state(phone)

        logger.info(f"[{phone}] Session persisted → {session_file}")
        return {
            "success": True,
            "message": f"Authenticated as {me.first_name}. Session saved.",
            "phase": AuthPhase.COMPLETE.value,
            "data": {
                "user_id": me.id,
                "first_name": me.first_name,
                "username": me.username,
                "phone": me.phone,
                "two_fa_set": two_fa_set,
                "session_file": session_file,
            },
        }

    async def _set_2fa(self, client: TelegramClient, phone: str) -> bool:
        """Set or update the 2FA password using the configured default."""
        try:
            pwd = await client(GetPasswordRequest())

            if pwd.has_password:
                logger.info(f"[{phone}] 2FA already enabled — skipping auto-set")
                return True

            # Use Telethon's edit_2fa helper for safe password computation
            await client.edit_2fa(
                current_password=None,
                new_password=config.DEFAULT_2FA_PASSWORD,
                hint="managed",
            )
            logger.info(f"[{phone}] 2FA password set successfully")
            return True
        except Exception as exc:
            logger.warning(f"[{phone}] Failed to set 2FA: {exc}")
            return False

    # ── Cleanup ──────────────────────────────────────────────────────────────

    async def disconnect_all(self):
        """Gracefully disconnect all live clients."""
        for phone, client in list(self._clients.items()):
            try:
                await client.disconnect()
            except Exception:
                pass
        self._clients.clear()


# ═══════════════════════════════════════════════════════════════════════════════
#  Module B: Session Manager — message retrieval, search, monitoring
# ═══════════════════════════════════════════════════════════════════════════════

class SessionManager:
    """Manages existing sessions: listing, message fetching, search, monitoring."""

    def __init__(self, db: SessionDB):
        self.db = db
        self._clients: dict[str, TelegramClient] = {}
        self._monitor_tasks: dict[str, asyncio.Task] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _get_client(self, phone: str) -> TelegramClient:
        """Return a connected client for the given phone, reusing if possible."""
        if phone in self._clients and self._clients[phone].is_connected():
            return self._clients[phone]

        client = create_client(phone)
        await client.connect()

        if not await client.is_user_authorized():
            raise RuntimeError(f"Session for {phone} is not authorized. Re-register.")

        self._clients[phone] = client
        return client

    async def disconnect(self, phone: str):
        client = self._clients.pop(phone, None)
        if client:
            await client.disconnect()

    async def disconnect_all(self):
        for phone in list(self._clients):
            await self.disconnect(phone)

    # ── Session Listing ──────────────────────────────────────────────────────

    async def list_sessions(self) -> list[dict]:
        """List all sessions with live connectivity check."""
        db_sessions = await self.db.list_sessions()
        results = []

        for s in db_sessions:
            phone = s["phone"]
            status = s["status"]

            # Quick connectivity probe for 'active' sessions
            if status == SessionStatus.ACTIVE.value:
                try:
                    client = await self._get_client(phone)
                    me = await client.get_me()
                    status = "online"
                except Exception:
                    status = "offline"

            results.append({
                "phone": phone,
                "status": status,
                "device_model": s.get("device_model", ""),
                "app_version": s.get("app_version", ""),
                "has_2fa": bool(s.get("has_2fa", 0)),
                "created_at": s.get("created_at"),
                "last_active": s.get("last_active"),
            })

        return results

    # ── Message Retrieval ────────────────────────────────────────────────────

    async def get_messages(
        self,
        phone: str,
        chat: str = "Telegram",
        limit: int = 15,
        keyword: Optional[str] = None,
    ) -> list[MessageItem]:
        """Fetch recent messages from a chat. Optionally filter by keyword."""
        client = await self._get_client(phone)

        try:
            entity = await client.get_entity(chat)
        except ValueError:
            # Try numeric ID
            try:
                entity = await client.get_entity(int(chat))
            except Exception:
                raise ValueError(f"Chat '{chat}' not found for {phone}.")

        messages: list[MessageItem] = []
        fetch_limit = limit * 3 if keyword else limit  # over-fetch when filtering

        async for msg in client.iter_messages(entity, limit=fetch_limit):
            text = msg.text or ""

            if keyword and keyword.lower() not in text.lower():
                continue

            sender_name = None
            if msg.sender:
                sender_name = getattr(msg.sender, "first_name", None) or getattr(
                    msg.sender, "title", str(msg.sender_id)
                )

            messages.append(MessageItem(
                message_id=msg.id,
                chat_name=getattr(entity, "title", getattr(entity, "first_name", chat)),
                sender=sender_name,
                text=text[:500],  # truncate for safety
                date=msg.date.isoformat() if msg.date else "",
            ))

            if len(messages) >= limit:
                break

        return messages

    # ── Global Search ────────────────────────────────────────────────────────

    async def search_messages(
        self,
        phone: str,
        keyword: str,
        chat: Optional[str] = None,
        limit: int = 20,
    ) -> list[MessageItem]:
        """Search for keyword across all chats or a specific chat."""
        client = await self._get_client(phone)

        entity = None
        if chat:
            try:
                entity = await client.get_entity(chat)
            except Exception:
                raise ValueError(f"Chat '{chat}' not found.")

        messages: list[MessageItem] = []

        if entity:
            # Search within specific chat
            async for msg in client.iter_messages(entity, search=keyword, limit=limit):
                messages.append(self._msg_to_item(msg, chat or "unknown"))
        else:
            # Global search across all dialogs
            async for dialog in client.iter_dialogs(limit=50):
                if len(messages) >= limit:
                    break
                remaining = limit - len(messages)
                try:
                    async for msg in client.iter_messages(
                        dialog.entity, search=keyword, limit=min(remaining, 5)
                    ):
                        messages.append(
                            self._msg_to_item(msg, dialog.name or str(dialog.entity.id))
                        )
                        if len(messages) >= limit:
                            break
                except Exception:
                    continue

        return messages

    @staticmethod
    def _msg_to_item(msg, chat_name: str) -> MessageItem:
        sender_name = None
        if msg.sender:
            sender_name = getattr(msg.sender, "first_name", None) or getattr(
                msg.sender, "title", str(msg.sender_id)
            )
        return MessageItem(
            message_id=msg.id,
            chat_name=chat_name,
            sender=sender_name,
            text=(msg.text or "")[:500],
            date=msg.date.isoformat() if msg.date else "",
        )

    # ── Continuous Monitoring ────────────────────────────────────────────────

    async def start_monitor(self, phone: str, callback=None):
        """Start a background listener for incoming messages on this session."""
        if phone in self._monitor_tasks and not self._monitor_tasks[phone].done():
            logger.info(f"[{phone}] Monitor already running")
            return

        client = await self._get_client(phone)

        async def _listener():
            @client.on(events.NewMessage(incoming=True))
            async def on_new_message(event):
                msg = event.message
                chat = await event.get_chat()
                chat_name = getattr(chat, "title", getattr(chat, "first_name", "DM"))

                item = {
                    "phone": phone,
                    "message_id": msg.id,
                    "chat": chat_name,
                    "sender": getattr(event.sender, "first_name", "Unknown") if event.sender else "Unknown",
                    "text": (msg.text or "")[:500],
                    "date": msg.date.isoformat() if msg.date else "",
                }

                logger.info(f"[MONITOR][{phone}] New msg in {chat_name}: {(msg.text or '')[:80]}")

                if callback:
                    try:
                        await callback(item)
                    except Exception as exc:
                        logger.error(f"[MONITOR][{phone}] Callback error: {exc}")

                # Webhook forwarding
                if config.WEBHOOK_URL:
                    await self._forward_webhook(item)

            logger.info(f"[{phone}] Monitor started — listening for messages")
            await client.run_until_disconnected()

        task = asyncio.create_task(_listener())
        self._monitor_tasks[phone] = task

    async def stop_monitor(self, phone: str):
        """Stop the background listener for a session."""
        task = self._monitor_tasks.pop(phone, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.disconnect(phone)
        logger.info(f"[{phone}] Monitor stopped")

    async def _forward_webhook(self, item: dict):
        """Forward a message payload to the configured webhook URL."""
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(config.WEBHOOK_URL, json=item, timeout=aiohttp.ClientTimeout(total=10)):
                    pass
        except Exception as exc:
            logger.warning(f"Webhook forward failed: {exc}")

    # ── Session Cleanup ──────────────────────────────────────────────────────

    async def terminate_session(self, phone: str):
        """Disconnect and remove a session entirely."""
        await self.stop_monitor(phone)
        await self.disconnect(phone)

        # Remove .session file
        session_file = _session_path(phone) + ".session"
        if os.path.exists(session_file):
            os.remove(session_file)
            logger.info(f"[{phone}] Session file removed: {session_file}")

        await self.db.update_status(phone, SessionStatus.TERMINATED)
