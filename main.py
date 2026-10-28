from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import json
import sqlite3
import hashlib
import os
from datetime import datetime
from typing import List, Dict

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === БАЗА ДАННЫХ ===
DB_NAME = "pohuy_link.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS chats (
        chat_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        is_group INTEGER DEFAULT 0,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS chat_members (
        chat_id TEXT,
        username TEXT,
        PRIMARY KEY (chat_id, username)
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        msg_id TEXT PRIMARY KEY,
        chat_id TEXT,
        sender TEXT,
        text TEXT,
        reply_to TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    conn.commit()
    conn.close()

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

init_db()

# === ОНЛАЙН ЮЗЕРЫ ===
online_users: Dict[str, WebSocket] = {}

@app.get("/", response_class=HTMLResponse)
async def get_home():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

# === АВТОРИЗАЦИЯ ===
@app.post("/api/register")
async def register(data: dict):
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    display_name = data.get("display_name", username)
    
    if not username or not password:
        raise HTTPException(status_code=400, detail="Юзернейм и пароль обязательны")
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Юзернейм слишком короткий")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Пароль слишком короткий")
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT username FROM users WHERE username = ?", (username,))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="Этот юзернейм уже занят, блядь!")
    
    password_hash = hash_password(password)
    c.execute("INSERT INTO users (username, display_name, password_hash) VALUES (?, ?, ?)", 
              (username, display_name, password_hash))
    conn.commit()
    conn.close()
    
    return {"success": True, "username": username}

@app.post("/api/login")
async def login(data: dict):
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username = ?", (username,))
    user = c.fetchone()
    conn.close()
    
    if not user:
        raise HTTPException(status_code=401, detail="Такого юзера нет, сука!")
    
    if user["password_hash"] != hash_password(password):
        raise HTTPException(status_code=401, detail="Неверный пароль, блядь!")
    
    return {
        "success": True,
        "username": user["username"],
        "display_name": user["display_name"]
    }

@app.get("/api/me/{username}")
async def get_user_profile(username: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT username, display_name, created_at FROM users WHERE username = ?", (username,))
    user = c.fetchone()
    conn.close()
    
    if not user:
        raise HTTPException(status_code=404, detail="Юзер не найден")
    
    return dict(user)

@app.put("/api/me/{username}")
async def update_profile(username: str, data: dict):
    new_display = data.get("display_name", "").strip()
    new_username = data.get("username", "").strip().lower()
    
    if not new_username or len(new_username) < 3:
        raise HTTPException(status_code=400, detail="Юзернейм слишком короткий")
    
    conn = get_db()
    c = conn.cursor()
    
    if new_username != username:
        c.execute("SELECT username FROM users WHERE username = ?", (new_username,))
        if c.fetchone():
            conn.close()
            raise HTTPException(status_code=409, detail="Этот юзернейм уже занят!")
        
        c.execute("UPDATE users SET username = ?, display_name = ? WHERE username = ?", 
                  (new_username, new_display or new_username, username))
        c.execute("UPDATE chat_members SET username = ? WHERE username = ?", (new_username, username))
        c.execute("UPDATE messages SET sender = ? WHERE sender = ?", (new_username, username))
        c.execute("UPDATE chats SET created_by = ? WHERE created_by = ?", (new_username, username))
    else:
        if new_display:
            c.execute("UPDATE users SET display_name = ? WHERE username = ?", (new_display, username))
    
    conn.commit()
    conn.close()
    
    return {"success": True, "username": new_username, "display_name": new_display or new_username}

# === ПОИСК ===
@app.get("/api/search")
async def search_users(q: str):
    q = q.lower().strip()
    if len(q) < 2:
        return []
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT username, display_name FROM users WHERE (username LIKE ? OR display_name LIKE ?)", 
              (f"%{q}%", f"%{q}%"))
    users = [{"username": row["username"], "display_name": row["display_name"], 
              "online": row["username"] in online_users} for row in c.fetchall()]
    conn.close()
    
    return users

# === ЧАТЫ ===
@app.get("/api/chats/{username}")
async def get_user_chats(username: str):
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""
        SELECT c.chat_id, c.name, c.is_group, c.created_by
        FROM chats c
        JOIN chat_members cm ON c.chat_id = cm.chat_id
        WHERE cm.username = ?
        ORDER BY c.created_at DESC
    """, (username,))
    
    chats = []
    for row in c.fetchall():
        chat_id = row["chat_id"]
        
        c.execute("SELECT username FROM chat_members WHERE chat_id = ?", (chat_id,))
        members = [r["username"] for r in c.fetchall()]
        
        c.execute("SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1", (chat_id,))
        last_msg_row = c.fetchone()
        last_msg = dict(last_msg_row) if last_msg_row else None
        if last_msg:
            last_msg["time"] = datetime.fromisoformat(last_msg["created_at"]).strftime("%H:%M") if last_msg["created_at"] else ""
        
        chats.append({
            "id": chat_id,
            "name": row["name"],
            "is_group": bool(row["is_group"]),
            "members": members,
            "last_msg": last_msg
        })
    
    conn.close()
    return chats

@app.get("/api/messages/{chat_id}")
async def get_chat_messages(chat_id: str, limit: int = 100):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT m.*, u.display_name 
        FROM messages m
        JOIN users u ON m.sender = u.username
        WHERE m.chat_id = ?
        ORDER BY m.created_at ASC
        LIMIT ?
    """, (chat_id, limit))
    
    messages = []
    for row in c.fetchall():
        msg = dict(row)
        msg["time"] = datetime.fromisoformat(msg["created_at"]).strftime("%H:%M") if msg["created_at"] else ""
        messages.append(msg)
    
    conn.close()
    return messages

# === WEBSOCKET ===
@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await websocket.accept()
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT username, display_name FROM users WHERE username = ?", (username,))
    user = c.fetchone()
    conn.close()
    
    if not user:
        await websocket.close(code=4001, reason="User not found")
        return
    
    online_users[username] = websocket
    
    chats = await get_user_chats(username)
    await websocket.send_json({
        "type": "init",
        "profile": {"username": user["username"], "display_name": user["display_name"]},
        "chats": chats
    })
    
    await broadcast_status(username, True)
    
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            
            if msg_type == "message":
                chat_id = data.get("chat_id")
                text = data.get("text")
                reply_to = data.get("reply_to")
                msg_id = data.get("id", f"msg_{int(datetime.now().timestamp() * 1000)}")
                
                conn = get_db()
                c = conn.cursor()
                c.execute("SELECT 1 FROM chat_members WHERE chat_id = ? AND username = ?", (chat_id, username))
                if not c.fetchone():
                    conn.close()
                    continue
                
                c.execute("INSERT INTO messages (msg_id, chat_id, sender, text, reply_to) VALUES (?, ?, ?, ?, ?)",
                          (msg_id, chat_id, username, text, reply_to))
                conn.commit()
                
                c.execute("SELECT username FROM chat_members WHERE chat_id = ?", (chat_id,))
                members = [r["username"] for r in c.fetchall()]
                conn.close()
                
                msg = {
                    "type": "message",
                    "id": msg_id,
                    "chat_id": chat_id,
                    "from": username,
                    "display_name": user["display_name"],
                    "text": text,
                    "reply_to": reply_to,
                    "time": datetime.now().strftime("%H:%M")
                }
                
                for member in members:
                    if member in online_users:
                        try:
                            await online_users[member].send_json(msg)
                        except:
                            pass
            
            elif msg_type == "create_chat":
                members = data.get("members", [])
                is_group = data.get("is_group", False)
                chat_name = data.get("name", "")
                
                if username not in members:
                    members.append(username)
                
                if not is_group and len(members) == 2:
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("""
                        SELECT chat_id FROM chats 
                        WHERE is_group = 0 
                        AND chat_id IN (
                            SELECT chat_id FROM chat_members WHERE username = ?
                            INTERSECT
                            SELECT chat_id FROM chat_members WHERE username = ?
                        )
                    """, (members[0], members[1]))
                    existing = c.fetchone()
                    conn.close()
                    
                    if existing:
                        await websocket.send_json({"type": "chat_created", "chat_id": existing["chat_id"]})
                        continue
                
                chat_id = f"chat_{int(datetime.now().timestamp() * 1000)}"
                if not chat_name:
                    if is_group:
                        chat_name = f"Тусовка {datetime.now().strftime('%d.%m')}"
                    else:
                        other = [m for m in members if m != username][0]
                        conn = get_db()
                        c = conn.cursor()
                        c.execute("SELECT display_name FROM users WHERE username = ?", (other,))
                        row = c.fetchone()
                        conn.close()
                        chat_name = row["display_name"] if row else other
                
                conn = get_db()
                c = conn.cursor()
                c.execute("INSERT INTO chats (chat_id, name, is_group, created_by) VALUES (?, ?, ?, ?)",
                          (chat_id, chat_name, int(is_group), username))
                
                for member in members:
                    c.execute("INSERT INTO chat_members (chat_id, username) VALUES (?, ?)", (chat_id, member))
                
                conn.commit()
                conn.close()
                
                for member in members:
                    if member in online_users:
                        try:
                            await online_users[member].send_json({
                                "type": "chat_created",
                                "chat_id": chat_id,
                                "chat": {
                                    "id": chat_id,
                                    "name": chat_name,
                                    "is_group": is_group,
                                    "members": members,
                                    "last_msg": None
                                }
                            })
                        except:
                            pass
            
            elif msg_type == "add_member":
                chat_id = data.get("chat_id")
                new_user = data.get("username")
                
                conn = get_db()
                c = conn.cursor()
                c.execute("SELECT 1 FROM chat_members WHERE chat_id = ? AND username = ?", (chat_id, username))
                if not c.fetchone():
                    conn.close()
                    continue
                
                c.execute("SELECT 1 FROM chat_members WHERE chat_id = ? AND username = ?", (chat_id, new_user))
                if c.fetchone():
                    conn.close()
                    continue
                
                c.execute("INSERT INTO chat_members (chat_id, username) VALUES (?, ?)", (chat_id, new_user))
                conn.commit()
                conn.close()
                
                if new_user in online_users:
                    try:
                        conn = get_db()
                        c = conn.cursor()
                        c.execute("SELECT name, is_group FROM chats WHERE chat_id = ?", (chat_id,))
                        chat = c.fetchone()
                        c.execute("SELECT username FROM chat_members WHERE chat_id = ?", (chat_id,))
                        members = [r["username"] for r in c.fetchall()]
                        conn.close()
                        
                        await online_users[new_user].send_json({
                            "type": "chat_created",
                            "chat_id": chat_id,
                            "chat": {
                                "id": chat_id,
                                "name": chat["name"],
                                "is_group": bool(chat["is_group"]),
                                "members": members,
                                "last_msg": None
                            }
                        })
                    except:
                        pass
                
                conn = get_db()
                c = conn.cursor()
                c.execute("SELECT username FROM chat_members WHERE chat_id = ?", (chat_id,))
                members = [r["username"] for r in c.fetchall()]
                conn.close()
                
                for member in members:
                    if member != new_user and member in online_users:
                        try:
                            await online_users[member].send_json({
                                "type": "system_message",
                                "chat_id": chat_id,
                                "text": f"{username} затащил {new_user} в чат"
                            })
                        except:
                            pass
            
            elif msg_type == "delete_message":
                msg_id = data.get("id")
                chat_id = data.get("chat_id")
                
                conn = get_db()
                c = conn.cursor()
                c.execute("DELETE FROM messages WHERE msg_id = ? AND chat_id = ?", (msg_id, chat_id))
                conn.commit()
                
                c.execute("SELECT username FROM chat_members WHERE chat_id = ?", (chat_id,))
                members = [r["username"] for r in c.fetchall()]
                conn.close()
                
                for member in members:
                    if member in online_users:
                        try:
                            await online_users[member].send_json({
                                "type": "message_deleted",
                                "chat_id": chat_id,
                                "id": msg_id
                            })
                        except:
                            pass
            
            elif msg_type in ["call_incoming", "call_accepted", "call_declined"]:
                target = data.get("target")
                if target and target in online_users:
                    try:
                        await online_users[target].send_json(data)
                    except:
                        pass
    
    except WebSocketDisconnect:
        if username in online_users:
            del online_users[username]
        await broadcast_status(username, False)

async def broadcast_status(username: str, online: bool):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT DISTINCT cm.username 
        FROM chat_members cm
        WHERE cm.username != ? AND cm.chat_id IN (
            SELECT chat_id FROM chat_members WHERE username = ?
        )
    """, (username, username))
    members = [r["username"] for r in c.fetchall()]
    conn.close()
    
    for member in members:
        if member in online_users:
            try:
                await online_users[member].send_json({
                    "type": "user_status",
                    "username": username,
                    "online": online
                })
            except:
                pass

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)