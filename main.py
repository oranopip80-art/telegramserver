"""
main.py — FastAPI server for Telegram multi-session management.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import config
from core import AuthStateStore, RegistrationEngine, SessionManager
from schema import (
    CodeSubmit,
    MessageQuery,
    MessageResponse,
    PasswordSetRequest,
    PhoneRequest,
    SearchQuery,
    SessionDB,
    SessionInfo,
    StatusResponse,
    TwoFASubmit,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("main")

# ─── Globals ─────────────────────────────────────────────────────────────────
db: SessionDB = SessionDB(config.DB_PATH)
store: AuthStateStore = AuthStateStore(config.REDIS_URL)
reg_engine: RegistrationEngine = None  # type: ignore
sess_mgr: SessionManager = None  # type: ignore


@asynccontextmanager
async def lifespan(app: FastAPI):
    global reg_engine, sess_mgr
    await db.connect()
    await store.connect()
    reg_engine = RegistrationEngine(db, store)
    sess_mgr = SessionManager(db)
    logger.info("System initialized — ready to accept connections")
    yield
    await reg_engine.disconnect_all()
    await sess_mgr.disconnect_all()
    await store.close()
    await db.close()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Telegram Session Manager",
    description="Multi-session Telegram automation & management system",
    version="1.0.0",
    lifespan=lifespan,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Module A — Registration Endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/auth/request-code", response_model=StatusResponse)
async def request_code(req: PhoneRequest):
    result = await reg_engine.request_code(req.phone)
    return StatusResponse(**result, phase=result.get("phase"))


@app.post("/auth/submit-code", response_model=StatusResponse)
async def submit_code(req: CodeSubmit):
    result = await reg_engine.submit_code(req.phone, req.code)
    return StatusResponse(**result, phase=result.get("phase"))


@app.post("/auth/submit-2fa", response_model=StatusResponse)
async def submit_2fa(req: TwoFASubmit):
    result = await reg_engine.submit_2fa(req.phone, req.password)
    return StatusResponse(**result, phase=result.get("phase"))


# ═══════════════════════════════════════════════════════════════════════════════
#  Module B — Session Management Endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/sessions", response_model=list[SessionInfo])
async def list_sessions():
    sessions = await sess_mgr.list_sessions()
    return [SessionInfo(**s) for s in sessions]


@app.post("/messages/get", response_model=MessageResponse)
async def get_messages(req: MessageQuery):
    try:
        msgs = await sess_mgr.get_messages(req.phone, req.chat or "Telegram", req.limit, req.keyword)
        return MessageResponse(success=True, phone=req.phone, messages=msgs, count=len(msgs))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/messages/search", response_model=MessageResponse)
async def search_messages(req: SearchQuery):
    try:
        msgs = await sess_mgr.search_messages(req.phone, req.keyword, req.chat, req.limit)
        return MessageResponse(success=True, phone=req.phone, messages=msgs, count=len(msgs))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/monitor/start")
async def start_monitor(req: PhoneRequest):
    try:
        await sess_mgr.start_monitor(req.phone)
        return {"success": True, "message": f"Monitor started for {req.phone}"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/monitor/stop")
async def stop_monitor(req: PhoneRequest):
    await sess_mgr.stop_monitor(req.phone)
    return {"success": True, "message": f"Monitor stopped for {req.phone}"}


@app.delete("/sessions/{phone}")
async def terminate_session(phone: str):
    phone = f"+{phone}" if not phone.startswith("+") else phone
    await sess_mgr.terminate_session(phone)
    return {"success": True, "message": f"Session {phone} terminated"}


# ═══════════════════════════════════════════════════════════════════════════════
#  Dashboard — serves the management UI
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with open("dashboard.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ═══════════════════════════════════════════════════════════════════════════════
#  Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=True)
