from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import json
from datetime import datetime
from typing import Dict, List

app = FastAPI()

users: Dict[str, dict] = {}
chats: Dict[str, dict] = {}
user_chats: Dict[str, List[str]] = {}

chat_counter = 0

def new_chat_id():
    global chat_counter
    chat_counter += 1
    return f"chat_{chat_counter}"

@app.get("/", response_class=HTMLResponse)
async def get_home():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/search")
async def search_users(q: str):
    q = q.lower().strip()
    if not q: return []
    results = []
    for uname, data in users.items():
        if q in uname.lower() or q in data.get("display_name", "").lower():
            results.append({
                "username": uname,
                "display_name": data.get("display_name", uname),
                "online": data.get("ws") is not None
            })
    return results

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await websocket.accept()
    
    if username not in users:
        users[username] = {"display_name": username, "friends": [], "ws": websocket}
        user_chats[username] = []
    else:
        users[username]["ws"] = websocket
    
    my_data = users[username]
    
    my_chat_ids = user_chats.get(username, [])
    my_chats_list = []
    for cid in my_chat_ids:
        if cid in chats:
            c = chats[cid]
            last_msg = c["messages"][-1] if c["messages"] else None
            my_chats_list.append({"id": cid, "name": c["name"], "is_group": c["is_group"], "members": c["members"], "last_msg": last_msg})
    
    await websocket.send_json({"type": "init", "profile": {"username": username, "display_name": my_data["display_name"]}, "chats": my_chats_list, "friends": my_data["friends"]})
    await broadcast_online_status(username, True)
    
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            
            if msg_type == "message":
                chat_id = data.get("chat_id")
                text = data.get("text")
                reply_to = data.get("reply_to")
                msg_id = data.get("id", datetime.now().isoformat())
                
                if chat_id and chat_id in chats and username in chats[chat_id]["members"]:
                    msg = {"type": "message", "chat_id": chat_id, "from": username, "display_name": users[username]["display_name"], "text": text, "id": msg_id, "reply_to": reply_to, "time": datetime.now().strftime("%H:%M")}
                    chats[chat_id]["messages"].append(msg)
                    for member in chats[chat_id]["members"]:
                        if member in users and users[member]["ws"]:
                            try: await users[member]["ws"].send_json(msg)
                            except: pass
            
            elif msg_type == "create_chat":
                members = data.get("members", [])
                is_group = data.get("is_group", False)
                chat_name = data.get("name", "")
                
                # ЕСЛИ ПУСТОЙ ЧАТ - ОСТАВЛЯЕМ ТОЛЬКО СОЗДАТЕЛЯ
                if not members:
                    members = [username]
                elif username not in members:
                    members.append(username)
                
                # Проверка на существующий личный чат
                if not is_group and len(members) == 2:
                    for cid, c in chats.items():
                        if not c["is_group"] and set(c["members"]) == set(members):
                            for m in members:
                                if cid not in user_chats.get(m, []): user_chats.setdefault(m, []).append(cid)
                            await websocket.send_json({"type": "chat_created", "chat_id": cid})
                            return
                
                cid = new_chat_id()
                if not chat_name:
                    if is_group: chat_name = "Тусовка " + datetime.now().strftime("%d.%m")
                    else:
                        other = [m for m in members if m != username][0] if len(members) > 1 else username
                        chat_name = users[other]["display_name"]
                
                chats[cid] = {"name": chat_name, "members": members, "messages": [], "is_group": is_group, "created_by": username}
                for m in members: user_chats.setdefault(m, []).append(cid)
                
                for m in members:
                    if m in users and users[m]["ws"]:
                        try: await users[m]["ws"].send_json({"type": "chat_created", "chat_id": cid, "chat": {"id": cid, "name": chat_name, "is_group": is_group, "members": members, "last_msg": None}})
                        except: pass

            elif msg_type == "add_member": # ИНВАЙТ В ЧАТ
                chat_id = data.get("chat_id")
                new_user = data.get("username")
                if chat_id in chats and new_user in users and new_user not in chats[chat_id]["members"]:
                    chats[chat_id]["members"].append(new_user)
                    user_chats.setdefault(new_user, []).append(chat_id)
                    # Уведомляем нового юзера
                    if users[new_user]["ws"]:
                        try:
                            c = chats[chat_id]
                            await users[new_user]["ws"].send_json({"type": "chat_created", "chat_id": chat_id, "chat": {"id": chat_id, "name": c["name"], "is_group": c["is_group"], "members": c["members"], "last_msg": None}})
                        except: pass
                    # Уведомляем остальных
                    for m in chats[chat_id]["members"]:
                        if m != new_user and m in users and users[m]["ws"]:
                            try: await users[m]["ws"].send_json({"type": "system_message", "chat_id": chat_id, "text": f"{username} позвал в чат {new_user}"})
                            except: pass

            elif msg_type == "update_profile":
                new_display = data.get("display_name", "").strip()
                new_username = data.get("username", "").strip()
                if new_display: users[username]["display_name"] = new_display
                if new_username and new_username != username and new_username not in users:
                    users[new_username] = users.pop(username)
                    user_chats[new_username] = user_chats.pop(username)
                    for cid, c in chats.items():
                        if username in c["members"]: c["members"] = [new_username if m == username else m for m in c["members"]]
                        for msg in c["messages"]:
                            if msg["from"] == username: msg["from"] = new_username; msg["display_name"] = new_display
                    username = new_username
                await websocket.send_json({"type": "profile_updated", "username": username, "display_name": users[username]["display_name"]})
            
            elif msg_type == "delete_message":
                chat_id = data.get("chat_id")
                msg_id = data.get("id")
                if chat_id and chat_id in chats:
                    chats[chat_id]["messages"] = [m for m in chats[chat_id]["messages"] if m.get("id") != msg_id]
                    for member in chats[chat_id]["members"]:
                        if member in users and users[member]["ws"]:
                            try: await users[member]["ws"].send_json({"type": "message_deleted", "chat_id": chat_id, "id": msg_id})
                            except: pass
    
    except WebSocketDisconnect:
        users[username]["ws"] = None
        await broadcast_online_status(username, False)

async def broadcast_online_status(username: str, online: bool):
    for cid, c in chats.items():
        if username in c["members"]:
            for member in c["members"]:
                if member != username and member in users and users[member]["ws"]:
                    try: await users[member]["ws"].send_json({"type": "user_status", "username": username, "online": online})
                    except: pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)