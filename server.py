from fastapi import FastAPI, APIRouter
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List
import uuid
from datetime import datetime, timezone, timedelta


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")


# Define Models
class StatusCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")  # Ignore MongoDB's _id field
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class StatusCheckCreate(BaseModel):
    client_name: str

# Add your routes to the router instead of directly to app
@api_router.get("/")
async def root():
    return {"message": "Hello World"}

@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_dict = input.model_dump()
    status_obj = StatusCheck(**status_dict)
    
    # Convert to dict and serialize datetime to ISO string for MongoDB
    doc = status_obj.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    
    _ = await db.status_checks.insert_one(doc)
    return status_obj

@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    # Exclude MongoDB's _id field from the query results
    status_checks = await db.status_checks.find({}, {"_id": 0}).to_list(1000)
    
    # Convert ISO string timestamps back to datetime objects
    for check in status_checks:
        if isinstance(check['timestamp'], str):
            check['timestamp'] = datetime.fromisoformat(check['timestamp'])
    
    return status_checks


def _fmt_uptime(seconds: int) -> str:
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def _fmt_ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


@api_router.get("/bot/status")
async def bot_status():
    """Live status of the Discord bot, written by the bot heartbeat."""
    doc = await db.bot_status.find_one({"_id": "live"})
    if not doc:
        return {
            "online": False,
            "uptime": "0h 0m",
            "uptime_seconds": 0,
            "servers": 0,
            "members": 0,
            "latency": 0,
            "commands_run": 0,
            "last_ping": "never",
            "bot_name": "canary",
            "bot_avatar": None,
        }
    # Consider offline if no heartbeat in the last 45 seconds
    online = bool(doc.get("online"))
    try:
        last = datetime.fromisoformat(doc.get("last_ping"))
        age = (datetime.now(timezone.utc) - last).total_seconds()
        last_str = _fmt_ago(age)
        if age > 45:
            online = False
    except Exception:
        last_str = "unknown"
    return {
        "online": online,
        "uptime": _fmt_uptime(doc.get("uptime_seconds", 0)),
        "uptime_seconds": doc.get("uptime_seconds", 0),
        "servers": doc.get("servers", 0),
        "members": doc.get("members", 0),
        "latency": doc.get("latency", 0),
        "commands_run": doc.get("commands_run", 0),
        "last_ping": last_str,
        "bot_name": doc.get("bot_name", "canary"),
        "bot_avatar": doc.get("bot_avatar"),
    }


# ============================ ADMIN PANEL ============================
import jwt
from fastapi import Depends, HTTPException, Header
from typing import Optional

JWT_SECRET = os.environ["JWT_SECRET"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
JWT_ALG = "HS256"


class LoginBody(BaseModel):
    password: str


class PresenceBody(BaseModel):
    status: str = "online"
    presence_type: str = "watching"
    presence_text: str = ""


class GuildConfigBody(BaseModel):
    prefix: Optional[str] = None
    filter: Optional[dict] = None


class AnnounceBody(BaseModel):
    guild_id: str
    channel_id: Optional[str] = None
    title: Optional[str] = None
    message: str


def create_admin_token() -> str:
    payload = {"sub": "admin", "role": "admin",
               "exp": datetime.now(timezone.utc) + timedelta(hours=12), "type": "access"}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


async def require_admin(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization[7:]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Forbidden")
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


@api_router.post("/admin/login")
async def admin_login(body: LoginBody):
    if body.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Incorrect password")
    return {"token": create_admin_token()}


@api_router.get("/admin/me")
async def admin_me(_admin=Depends(require_admin)):
    return {"role": "admin"}


@api_router.get("/admin/overview")
async def admin_overview(_admin=Depends(require_admin)):
    doc = await db.bot_status.find_one({"_id": "live"}) or {}
    online = bool(doc.get("online"))
    try:
        last = datetime.fromisoformat(doc.get("last_ping"))
        if (datetime.now(timezone.utc) - last).total_seconds() > 45:
            online = False
    except Exception:
        pass
    return {
        "online": online,
        "uptime": _fmt_uptime(doc.get("uptime_seconds", 0)),
        "servers": doc.get("servers", 0),
        "members": doc.get("members", 0),
        "latency": doc.get("latency", 0),
        "commands_run": doc.get("commands_run", 0),
        "bot_name": doc.get("bot_name", "canary"),
        "bot_avatar": doc.get("bot_avatar"),
        "guilds": doc.get("guilds", []),
    }


class BroadcastBody(BaseModel):
    title: Optional[str] = None
    message: str


@api_router.post("/admin/broadcast")
async def admin_broadcast(body: BroadcastBody, _admin=Depends(require_admin)):
    doc = await db.bot_status.find_one({"_id": "live"}) or {}
    count = 0
    for g in doc.get("guilds", []):
        await db.bot_tasks.insert_one({
            "type": "announce", "guild_id": g["id"], "channel_id": None,
            "title": body.title, "message": body.message, "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        count += 1
    return {"ok": True, "queued": count}


@api_router.get("/admin/presence")
async def get_presence(_admin=Depends(require_admin)):
    ctrl = await db.bot_control.find_one({"_id": "control"}) or {}
    return {"status": ctrl.get("status", "online"),
            "presence_type": ctrl.get("presence_type", "watching"),
            "presence_text": ctrl.get("presence_text", "")}


@api_router.post("/admin/presence")
async def set_presence(body: PresenceBody, _admin=Depends(require_admin)):
    await db.bot_control.update_one({"_id": "control"}, {"$set": {
        "status": body.status, "presence_type": body.presence_type, "presence_text": body.presence_text,
    }}, upsert=True)
    return {"ok": True}


@api_router.get("/admin/guild/{guild_id}/config")
async def get_guild_config(guild_id: str, _admin=Depends(require_admin)):
    cfg = await db.guild_config.find_one({"_id": guild_id}) or {}
    return {
        "prefix": cfg.get("prefix", os.environ.get("DISCORD_PREFIX", ",")),
        "filter": cfg.get("filter", {}),
        "modlog": cfg.get("modlog"),
        "welcome_msgs": cfg.get("welcome_msgs", []),
        "autoroles": cfg.get("autoroles", []),
    }


@api_router.put("/admin/guild/{guild_id}/config")
async def update_guild_config(guild_id: str, body: GuildConfigBody, _admin=Depends(require_admin)):
    update = {}
    if body.prefix is not None:
        update["prefix"] = body.prefix
    if body.filter is not None:
        update["filter"] = body.filter
    if update:
        await db.guild_config.update_one({"_id": guild_id}, {"$set": update}, upsert=True)
    return {"ok": True, "updated": list(update.keys())}


@api_router.post("/admin/announce")
async def admin_announce(body: AnnounceBody, _admin=Depends(require_admin)):
    await db.bot_tasks.insert_one({
        "type": "announce", "guild_id": body.guild_id, "channel_id": body.channel_id,
        "title": body.title, "message": body.message, "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True, "queued": True}


@api_router.post("/admin/guild/{guild_id}/leave")
async def admin_leave_guild(guild_id: str, _admin=Depends(require_admin)):
    await db.bot_tasks.insert_one({"type": "leave", "guild_id": guild_id, "status": "pending",
                                   "created_at": datetime.now(timezone.utc).isoformat()})
    return {"ok": True, "queued": True}


class WhitelistBody(BaseModel):
    guild_id: str
    name: Optional[str] = None


class WhitelistToggle(BaseModel):
    enabled: bool


@api_router.get("/admin/whitelist")
async def get_whitelist(_admin=Depends(require_admin)):
    ctrl = await db.bot_control.find_one({"_id": "control"}) or {}
    return {"enabled": bool(ctrl.get("whitelist_enabled")),
            "servers": ctrl.get("server_whitelist_meta", [])}


@api_router.post("/admin/whitelist")
async def add_whitelist(body: WhitelistBody, _admin=Depends(require_admin)):
    gid = body.guild_id.strip()
    if not gid.isdigit():
        raise HTTPException(status_code=400, detail="Guild ID must be numeric")
    ctrl = await db.bot_control.find_one({"_id": "control"}) or {}
    ids = ctrl.get("server_whitelist", [])
    meta = ctrl.get("server_whitelist_meta", [])
    if gid not in ids:
        ids.append(gid)
        meta.append({"id": gid, "name": (body.name or gid).strip()})
        await db.bot_control.update_one({"_id": "control"},
            {"$set": {"server_whitelist": ids, "server_whitelist_meta": meta}}, upsert=True)
    return {"ok": True, "servers": meta}


@api_router.delete("/admin/whitelist/{guild_id}")
async def remove_whitelist(guild_id: str, _admin=Depends(require_admin)):
    ctrl = await db.bot_control.find_one({"_id": "control"}) or {}
    ids = [i for i in ctrl.get("server_whitelist", []) if i != guild_id]
    meta = [m for m in ctrl.get("server_whitelist_meta", []) if m.get("id") != guild_id]
    await db.bot_control.update_one({"_id": "control"},
        {"$set": {"server_whitelist": ids, "server_whitelist_meta": meta}}, upsert=True)
    return {"ok": True, "servers": meta}


@api_router.post("/admin/whitelist/toggle")
async def toggle_whitelist(body: WhitelistToggle, _admin=Depends(require_admin)):
    await db.bot_control.update_one({"_id": "control"},
        {"$set": {"whitelist_enabled": body.enabled}}, upsert=True)
    return {"ok": True, "enabled": body.enabled}


# ============================ DISCORD OAUTH (members) ============================
import httpx
from urllib.parse import urlencode
from starlette.responses import RedirectResponse

DISCORD_CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
DISCORD_CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
DISCORD_REDIRECT_URI = os.environ["DISCORD_REDIRECT_URI"]
FRONTEND_URL = os.environ["FRONTEND_URL"]
BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_API = "https://discord.com/api/v10"
MANAGE_GUILD = 0x20


def create_user_token(user, managed):
    payload = {
        "sub": user["id"], "username": user.get("global_name") or user.get("username"),
        "avatar": user.get("avatar"), "role": "user", "guilds": managed,
        "exp": datetime.now(timezone.utc) + timedelta(days=7), "type": "access",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


async def require_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(authorization[7:], JWT_SECRET, algorithms=[JWT_ALG])
        if payload.get("role") != "user":
            raise HTTPException(status_code=403, detail="Forbidden")
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


class SendEmbedBody(BaseModel):
    channel_id: str
    content: Optional[str] = None
    embed: Optional[dict] = None


@api_router.get("/auth/discord/login")
async def discord_oauth_login():
    params = urlencode({
        "client_id": DISCORD_CLIENT_ID, "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code", "scope": "identify guilds",
    })
    return RedirectResponse(f"{DISCORD_API}/oauth2/authorize?{params}")


@api_router.get("/auth/discord/callback")
async def discord_oauth_callback(code: Optional[str] = None, error: Optional[str] = None):
    if error or not code:
        return RedirectResponse(f"{FRONTEND_URL}/embeds?error=denied")
    async with httpx.AsyncClient(timeout=15) as c:
        tok = await c.post(f"{DISCORD_API}/oauth2/token", data={
            "client_id": DISCORD_CLIENT_ID, "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code", "code": code, "redirect_uri": DISCORD_REDIRECT_URI,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if tok.status_code != 200:
            return RedirectResponse(f"{FRONTEND_URL}/embeds?error=token")
        access = tok.json()["access_token"]
        h = {"Authorization": f"Bearer {access}"}
        user = (await c.get(f"{DISCORD_API}/users/@me", headers=h)).json()
        guilds = (await c.get(f"{DISCORD_API}/users/@me/guilds", headers=h)).json()
    managed = []
    if isinstance(guilds, list):
        for g in guilds:
            try:
                perms = int(g.get("permissions", 0))
            except (TypeError, ValueError):
                perms = 0
            if g.get("owner") or (perms & MANAGE_GUILD):
                managed.append({"id": g["id"], "name": g["name"], "icon": g.get("icon"), "owner": g.get("owner", False)})
    token = create_user_token(user, managed)
    return RedirectResponse(f"{FRONTEND_URL}/embeds#token={token}")


@api_router.get("/me")
async def me(payload=Depends(require_user)):
    avatar = None
    if payload.get("avatar"):
        avatar = f"https://cdn.discordapp.com/avatars/{payload['sub']}/{payload['avatar']}.png"
    return {"id": payload["sub"], "username": payload.get("username"), "avatar": avatar}


@api_router.get("/me/guilds")
async def my_guilds(payload=Depends(require_user)):
    status_doc = await db.bot_status.find_one({"_id": "live"}) or {}
    bot_guild_ids = {str(g.get("id")) for g in status_doc.get("guilds", [])}
    out = []
    for g in payload.get("guilds", []):
        icon = f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png" if g.get("icon") else None
        out.append({"id": g["id"], "name": g["name"], "icon": icon,
                    "owner": g.get("owner", False), "bot_present": g["id"] in bot_guild_ids})
    return {"guilds": out}


def _user_manages(payload, guild_id):
    return any(g["id"] == guild_id for g in payload.get("guilds", []))


@api_router.get("/guild/{guild_id}/channels")
async def guild_channels(guild_id: str, payload=Depends(require_user)):
    if not _user_manages(payload, guild_id):
        raise HTTPException(status_code=403, detail="You don't manage this server")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{DISCORD_API}/guilds/{guild_id}/channels",
                        headers={"Authorization": f"Bot {BOT_TOKEN}"})
    if r.status_code != 200:
        raise HTTPException(status_code=400, detail="Bot is not in this server")
    chans = [{"id": ch["id"], "name": ch["name"]} for ch in r.json() if ch.get("type") in (0, 5)]
    return {"channels": chans}


@api_router.post("/guild/{guild_id}/send")
async def guild_send(guild_id: str, body: SendEmbedBody, payload=Depends(require_user)):
    if not _user_manages(payload, guild_id):
        raise HTTPException(status_code=403, detail="You don't manage this server")
    msg = {}
    if body.content:
        msg["content"] = body.content
    if body.embed:
        msg["embeds"] = [body.embed]
    if not msg:
        raise HTTPException(status_code=400, detail="Nothing to send")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{DISCORD_API}/channels/{body.channel_id}/messages", json=msg,
                         headers={"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"})
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=400, detail=f"Discord rejected the message ({r.status_code})")
    return {"ok": True}


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()