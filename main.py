# -*- coding: utf-8 -*-
import os, json, asyncio, random
from typing import Dict, Optional
from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from pydantic import BaseModel
import httpx

CONFIG = {"MINA_SERVER_IP": "", "MINA_SERVER_PORT": ""}
if os.path.exists("config.json"):
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            CONFIG.update(json.load(f))
    except Exception:
        pass

GATEWAY_BASE = os.getenv("GATEWAY_BASE", "internal-mock")
API_TOKEN = os.getenv("GATEWAY_TOKEN", "CHANGE_ME")
XP_PERIOD_SECONDS = int(os.getenv("XP_PERIOD_SECONDS", "180"))
XP_MIN = int(os.getenv("XP_MIN", "50"))
XP_MAX = int(os.getenv("XP_MAX", "250"))
MAX_ACCOUNTS = 20

app = FastAPI(title="Level-Up Dashboard (Mock)")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

class Account(BaseModel):
    uid: str
    token: str
    display_name: str = ""
    avatar_url: Optional[str] = None
    online: bool = False
    matchmaking: bool = False
    current_level: int = 1
    target_level: Optional[int] = None
    today_xp: int = 0
    total_xp: int = 0
    running: bool = False

ACCOUNTS: Dict[str, Account] = {}
TASKS: Dict[str, asyncio.Task] = {}

def level_from_xp(xp: int) -> int:
    return max(1, xp // 1000 + 1)

@app.post("/mock/queue/start")
async def mock_start(uid: str = Form(...)):
    return {"ok": True, "queued": True, "uid": uid}

@app.get("/mock/queue/status")
async def mock_status(uid: str):
    import random
    gained = random.randint(XP_MIN, XP_MAX)
    return {"ok": True, "uid": uid, "gained_xp": gained}

async def gw_start_match(uid: str, token: str, target_level: Optional[int]):
    if GATEWAY_BASE == "internal-mock":
        async with httpx.AsyncClient(base_url="http://127.0.0.1") as client:
            r = await client.post("/mock/queue/start", data={"uid": uid})
            r.raise_for_status()
            return r.json()
    else:
        url = f"{GATEWAY_BASE.rstrip('/')}/queue/start"
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        payload = {"uid": uid, "account_token": token, "target_level": target_level}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            return r.json()

async def gw_poll_result(uid: str):
    if GATEWAY_BASE == "internal-mock":
        async with httpx.AsyncClient(base_url="http://127.0.0.1") as client:
            r = await client.get("/mock/queue/status", params={"uid": uid})
            r.raise_for_status()
            return r.json()
    else:
        url = f"{GATEWAY_BASE.rstrip('/')}/queue/status"
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, params={"uid": uid}, headers=headers)
            r.raise_for_status()
            return r.json()

async def account_loop(uid: str):
    acc = ACCOUNTS[uid]
    acc.online = True
    acc.running = True
    while acc.running:
        acc.matchmaking = True
        try:
            await gw_start_match(acc.uid, acc.token, acc.target_level)
        except Exception:
            acc.matchmaking = False
            acc.running = False
            break

        await asyncio.sleep(XP_PERIOD_SECONDS)

        try:
            res = await gw_poll_result(acc.uid)
            gained = int(res.get("gained_xp", 0))
        except Exception:
            gained = 0

        acc.today_xp += gained
        acc.total_xp += gained
        acc.current_level = level_from_xp(acc.total_xp)
        acc.matchmaking = False

        if acc.target_level is not None and acc.current_level >= acc.target_level:
            acc.running = False
            break

        await asyncio.sleep(1)

    acc.matchmaking = False
    acc.online = True

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "max_accounts": MAX_ACCOUNTS,
        "server_ip": CONFIG.get("MINA_SERVER_IP", ""),
        "server_port": CONFIG.get("MINA_SERVER_PORT", "")
    })

@app.get("/api/accounts", response_class=JSONResponse)
async def list_accounts():
    return {"accounts": [a.dict() for a in ACCOUNTS.values()]}

@app.post("/api/accounts/add", response_class=JSONResponse)
async def add_account(uid: str = Form(...), token: str = Form(...), display_name: str = Form("")):
    if len(ACCOUNTS) >= MAX_ACCOUNTS:
        raise HTTPException(status_code=400, detail=f"Max {MAX_ACCOUNTS} accounts")
    if uid in ACCOUNTS:
        raise HTTPException(status_code=400, detail="Account already exists")
    ACCOUNTS[uid] = Account(uid=uid, token=token, display_name=display_name or f"User {uid[-4:]}")
    return {"ok": True}

@app.post("/api/accounts/remove", response_class=JSONResponse)
async def remove_account(uid: str = Form(...)):
    if uid not in ACCOUNTS:
        raise HTTPException(status_code=404, detail="Not found")
    t = TASKS.get(uid)
    if t and not t.done():
        ACCOUNTS[uid].running = False
        t.cancel()
    TASKS.pop(uid, None)
    ACCOUNTS.pop(uid, None)
    return {"ok": True}

@app.post("/api/accounts/start", response_class=JSONResponse)
async def start(uid: str = Form(...), target_level: Optional[int] = Form(None)):
    if uid not in ACCOUNTS:
        raise HTTPException(status_code=404, detail="Not found")
    acc = ACCOUNTS[uid]
    if target_level is not None:
        try:
            t = int(target_level)
            if t < 1:
                raise ValueError
            acc.target_level = t
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid target level")
    if uid in TASKS and not TASKS[uid].done():
        return {"ok": True, "msg": "Already running"}
    acc.running = True
    TASKS[uid] = asyncio.create_task(account_loop(uid))
    return {"ok": True}

@app.post("/api/accounts/stop", response_class=JSONResponse)
async def stop(uid: str = Form(...)):
    if uid not in ACCOUNTS:
        raise HTTPException(status_code=404, detail="Not found")
    acc = ACCOUNTS[uid]
    acc.running = False
    t = TASKS.get(uid)
    if t and not t.done():
        t.cancel()
    return {"ok": True}

@app.post("/api/accounts/reset_today", response_class=JSONResponse)
async def reset_today(uid: str = Form(...)):
    if uid not in ACCOUNTS:
        raise HTTPException(status_code=404, detail="Not found")
    ACCOUNTS[uid].today_xp = 0
    return {"ok": True}

def start_ngrok():
    token = os.getenv("NGROK_AUTHTOKEN", "").strip()
    if not token:
        return None
    try:
        from pyngrok import ngrok, conf
        conf.get_default().auth_token = token
        public_url = ngrok.connect(addr="80", proto="http").public_url
        print(f"🔗 NGROK URL: {public_url}")
        return public_url
    except Exception as e:
        print("Ngrok start error:", e)
        return None

if os.getenv("ENABLE_NGROK", "1") == "1":
    start_ngrok()
