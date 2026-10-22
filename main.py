import os, re, json, uuid, secrets
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from passlib.context import CryptContext
from starlette.middleware.sessions import SessionMiddleware
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB = os.getenv("MONGODB_DB", "radiowave3d")
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"
app = FastAPI(title="RadioWave 3D")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="radiowave_session", same_site="lax", https_only=COOKIE_SECURE, max_age=1209600)
app.mount("/static", StaticFiles(directory="static"), name="static")
pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
serializer = URLSafeTimedSerializer(SECRET_KEY, salt="radiowave-ws")
client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000) if MONGODB_URI else None
db = client[MONGODB_DB] if client is not None else None
if db is not None:
    try:
        db.users.create_index([("email", ASCENDING)], unique=True)
        db.users.create_index([("username_lower", ASCENDING)], unique=True)
        db.saved_frequencies.create_index([("user_id", ASCENDING), ("frequency", ASCENDING)], unique=True)
    except Exception:
        pass

rooms = {}       # frequency -> {socket_id: websocket}
sockets = {}     # socket_id -> public user
socket_freq = {} # socket_id -> frequency

def now(): return datetime.now(timezone.utc).isoformat()
def need_db():
    if db is None: raise HTTPException(503, "MongoDB not configured. Set MONGODB_URI in Render.")
def public(u): return {"id": str(u["_id"]), "username": u["username"], "email": u["email"]}
def current_user(request):
    if db is None or not request.session.get("uid"): return None
    try:
        from bson import ObjectId
        return db.users.find_one({"_id": ObjectId(request.session["uid"])})
    except Exception: return None
def clean_freq(v):
    v = str(v).strip().upper().replace(" ", "")
    if not re.fullmatch(r"\d{2,3}\.\d{1,4}", v): raise HTTPException(400, "Use a frequency such as 145.500.")
    n = float(v)
    if not 20 <= n <= 1000: raise HTTPException(400, "Frequency must be between 20 and 1000 MHz.")
    return f"{n:.3f}"

class Register(BaseModel):
    username: str = Field(min_length=3, max_length=24)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
class Login(BaseModel):
    login: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)
class SavedFrequency(BaseModel):
    frequency: str
    label: str = Field(default="Saved channel", max_length=40)

@app.get("/", response_class=HTMLResponse)
def home():
    return open("index.html", encoding="utf-8").read()

@app.get("/health")
def health():
    connected = False
    if client:
        try: client.admin.command("ping"); connected = True
        except Exception: pass
    return {"status":"ok", "mongodb_configured": db is not None, "mongodb_connected": connected}

@app.get("/api/me")
def me(request: Request):
    u = current_user(request)
    return {"authenticated": bool(u), "user": public(u) if u else None}

@app.post("/api/register")
def register(p: Register, request: Request):
    need_db()
    username = p.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,24}", username):
        raise HTTPException(400, "Username must be 3–24 letters, numbers, dots, dashes or underscores.")
    doc = {"username":username, "username_lower":username.lower(), "email":str(p.email).lower().strip(),
           "password_hash":pwd.hash(p.password), "created_at":now()}
    try: result = db.users.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "That email or username is already registered.")
    request.session.clear(); request.session["uid"] = str(result.inserted_id)
    doc["_id"] = result.inserted_id
    return {"ok":True, "user":public(doc)}

@app.post("/api/login")
def login(p: Login, request: Request):
    need_db()
    v = p.login.strip().lower()
    u = db.users.find_one({"$or":[{"email":v},{"username_lower":v}]})
    if not u or not pwd.verify(p.password, u.get("password_hash","")):
        raise HTTPException(401, "Incorrect username/email or password.")
    request.session.clear(); request.session["uid"] = str(u["_id"])
    return {"ok":True, "user":public(u)}

@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok":True}

@app.get("/api/frequencies")
def list_frequencies(request: Request):
    u = current_user(request)
    if not u: raise HTTPException(401, "Please sign in.")
    rows = db.saved_frequencies.find({"user_id":str(u["_id"])}).sort("frequency", ASCENDING)
    return {"frequencies":[{"frequency":x["frequency"],"label":x.get("label","Saved channel")} for x in rows]}

@app.post("/api/frequencies")
def save_frequency(p: SavedFrequency, request: Request):
    u = current_user(request)
    if not u: raise HTTPException(401, "Please sign in.")
    f = clean_freq(p.frequency)
    db.saved_frequencies.update_one({"user_id":str(u["_id"]),"frequency":f},
        {"$set":{"label":p.label.strip() or "Saved channel","updated_at":now()}}, upsert=True)
    return {"ok":True,"frequency":f}

@app.delete("/api/frequencies/{frequency}")
def delete_frequency(frequency: str, request: Request):
    u = current_user(request)
    if not u: raise HTTPException(401, "Please sign in.")
    db.saved_frequencies.delete_one({"user_id":str(u["_id"]),"frequency":clean_freq(frequency)})
    return {"ok":True}

@app.get("/api/voice-token")
def voice_token(request: Request):
    u = current_user(request)
    if not u: raise HTTPException(401, "Please sign in.")
    return {"token":serializer.dumps({"uid":str(u["_id"])})}

async def send(ws, data):
    try: await ws.send_text(json.dumps(data))
    except Exception: pass
async def broadcast(freq, data, exclude=None):
    for sid, ws in list(rooms.get(freq, {}).items()):
        if sid != exclude: await send(ws, data)

@app.websocket("/ws/voice")
async def voice(ws: WebSocket):
    token = ws.query_params.get("token","")
    try: uid = serializer.loads(token, max_age=1209600).get("uid")
    except (BadSignature, SignatureExpired): uid = None
    if not uid or db is None:
        await ws.close(code=1008); return
    try:
        from bson import ObjectId
        user = db.users.find_one({"_id":ObjectId(uid)})
    except Exception: user = None
    if not user:
        await ws.close(code=1008); return
    sid = str(uuid.uuid4()); freq = "145.500"
    await ws.accept()
    sockets[sid] = {"id":sid,"username":user["username"]}
    socket_freq[sid] = freq; rooms.setdefault(freq,{})[sid] = ws
    await send(ws, {"type":"welcome","id":sid,"frequency":freq})
    await send(ws, {"type":"roster","peers":[sockets[k] for k in rooms[freq] if k != sid]})
    await broadcast(freq, {"type":"peer-joined","peer":sockets[sid]}, sid)
    try:
        while True:
            try: m = json.loads(await ws.receive_text())
            except Exception: continue
            kind = m.get("type")
            if kind == "tune":
                new = clean_freq(m.get("frequency",""))
                old = socket_freq[sid]
                if new == old: continue
                rooms.get(old,{}).pop(sid,None)
                await broadcast(old, {"type":"peer-left","peer_id":sid}, sid)
                if not rooms.get(old): rooms.pop(old,None)
                socket_freq[sid] = new; rooms.setdefault(new,{})[sid] = ws
                await send(ws, {"type":"tuned","frequency":new})
                await send(ws, {"type":"roster","peers":[sockets[k] for k in rooms[new] if k != sid]})
                await broadcast(new, {"type":"peer-joined","peer":sockets[sid]}, sid)
            elif kind in ("offer","answer","ice"):
                target = m.get("target")
                if target in rooms.get(socket_freq[sid],{}):
                    await send(rooms[socket_freq[sid]][target], {"type":kind,"from":sid,
                        "description":m.get("description"),"candidate":m.get("candidate")})
            elif kind == "ptt":
                await broadcast(socket_freq[sid], {"type":"ptt","peer_id":sid,"active":bool(m.get("active"))}, sid)
    except WebSocketDisconnect: pass
    finally:
        old = socket_freq.pop(sid, "145.500")
        rooms.get(old,{}).pop(sid,None)
        if not rooms.get(old): rooms.pop(old,None)
        sockets.pop(sid,None)
        await broadcast(old, {"type":"peer-left","peer_id":sid})
