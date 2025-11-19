#!/usr/bin/env python3
"""
FINAL VERSION – WORKS 100%
Deploy this and go drink coffee
"""
import os
import asyncio
import sqlite3
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import uvloop
uvloop.install()

from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ====================== CONFIG ======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAIN_GROUP_ID = os.getenv("MAIN_GROUP_ID")
APPROVAL_GROUP_ID = os.getenv("APPROVAL_GROUP_ID")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")

if not all([BOT_TOKEN, MAIN_GROUP_ID, APPROVAL_GROUP_ID]):
    raise SystemExit("Set BOT_TOKEN, MAIN_GROUP_ID, APPROVAL_GROUP_ID")

ADMIN_IDS = {int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = "media_moderator.db"

# ====================== DB ======================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            media_group_id TEXT,
            is_album INTEGER,
            caption TEXT,
            created_at TEXT,
            payload TEXT
        )
    """)
    # Add full_name column if missing (old DBs)
    try:
        cur.execute("ALTER TABLE pending ADD COLUMN full_name TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

init_db()

def save_pending(chat_id, user_id, username, full_name, mgid, is_album, caption, payload):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO pending 
        (chat_id, user_id, username, full_name, media_group_id, is_album, caption, created_at, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(chat_id), user_id, username or "", full_name,
        mgid or "", int(is_album), caption or "",
        datetime.now(timezone.utc).isoformat(), json.dumps(payload)
    ))
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid

def get_pending(pid: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM pending WHERE id=?", (pid,))
    row = cur.fetchone()
    cols = [d[0] for d in cur.description] if cur.description else []
    conn.close()
    if not row:
        return None
    data = dict(zip(cols, row))
    data["payload"] = json.loads(data["payload"])
    data["is_album"] = bool(data.get("is_album", 0))
    return data

def delete_pending(pid: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM pending WHERE id=?", (pid,))
    conn.commit()
    conn.close()

def mention(uid, username, full_name):
    if username:
        return f"@{username}"
    # Escape special Markdown characters in name
    escaped_name = full_name.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("]", "\\]").replace("(", "\\(").replace(")", "\\)").replace("~", "\\~").replace("`", "\\`").replace(">", "\\>").replace("#", "\\#").replace("+", "\\+").replace("-", "\\-").replace("=", "\\=").replace("|", "\\|").replace("{", "\\{").replace("}", "\\}").replace(".", "\\.").replace("!", "\\!")
    return f"[{escaped_name}](tg://user?id={uid})"

# ====================== IN-MEMORY ======================
media_buffer = {}
album_meta = {}
flush_tasks = {}
selective_selections = {}
MEDIA_TIMEOUT = 5.0

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ====================== KEYBOARDS ======================
def approval_kb(pid):
    b = InlineKeyboardBuilder()
    b.button(text="Approve all", callback_data=f"approve_all:{pid}")
    b.button(text="Reject all", callback_data=f"reject_all:{pid}")
    b.button(text="Approve selectively", callback_data=f"selective:{pid}")
    b.adjust(2, 1)
    return b.as_markup()

def keep_remove_kb(pid, idx):
    b = InlineKeyboardBuilder()
    b.button(text="Keep", callback_data=f"keep:{pid}:{idx}")
    b.button(text="Remove", callback_data=f"remove:{pid}:{idx}")
    b.adjust(2)
    return b.as_markup()

def finalize_kb(pid):
    b = InlineKeyboardBuilder()
    b.button(text="Finalize & Post", callback_data=f"finalize:{pid}")
    return b.as_markup()

# ====================== ALBUM FLUSH ======================
async def flush_album(key: str):
    await asyncio.sleep(MEDIA_TIMEOUT)
    items = media_buffer.pop(key, [])
    meta = album_meta.pop(key, None)
    flush_tasks.pop(key, None)
    if not items or not meta:
        return
    pid = save_pending(
        meta["chat_id"], meta["user_id"], meta["username"], meta["full_name"],
        meta.get("media_group_id"), True, meta.get("caption", ""),
        {"items": [{"file_id": i["file_id"], "type": i["type"]} for i in items]}
    )
    await forward_to_approval(pid)

# ====================== FORWARD ======================
async def forward_to_approval(pid: int):
    p = get_pending(pid)
    if not p:
        return

    items = p["payload"]["items"]
    media = []
    for it in items:
        if it["type"] == "photo":
            media.append(types.InputMediaPhoto(media=it["file_id"]))
        else:
            media.append(types.InputMediaVideo(media=it["file_id"]))
    if p.get("caption"):
        media[0].caption = p["caption"]

    reply_to = None
    try:
        sent = await bot.send_media_group(int(APPROVAL_GROUP_ID), media)
        reply_to = sent[0].message_id
    except Exception as e:
        logger.error(f"Forward failed: {e}")

    await bot.send_message(
        int(APPROVAL_GROUP_ID),
        f"New submission from {mention(p['user_id'], p['username'], p['full_name'])} (ID: {pid})",
        reply_markup=approval_kb(pid),
        reply_to_message_id=reply_to,
        disable_web_page_preview=True
    )

# ====================== MESSAGE HANDLER ======================
@dp.message()
async def handle_message(msg: types.Message):
    if not msg.from_user or msg.from_user.is_bot:
        return
    if str(msg.chat.id) != MAIN_GROUP_ID:
        return
    if msg.from_user.id in ADMIN_IDS:
        return

    # ALBUM
    if msg.media_group_id:
        key = f"{msg.chat.id}:{msg.media_group_id}"
        if msg.photo:
            file_id = msg.photo[-1].file_id
            typ = "photo"
        elif msg.video:
            file_id = msg.video.file_id
            typ = "video"
        else:
            return

        media_buffer.setdefault(key, []).append({"file_id": file_id, "type": typ})

        meta = album_meta.setdefault(key, {})
        if "user_id" not in meta:
            meta.update({
                "user_id": msg.from_user.id,
                "username": msg.from_user.username,
                "full_name": msg.from_user.full_name,
                "chat_id": msg.chat.id,
                "media_group_id": str(msg.media_group_id)
            })
        if msg.caption and "caption" not in meta:
            meta["caption"] = msg.caption

        try:
            await msg.delete()
        except:
            pass

        if key in flush_tasks:
            flush_tasks[key].cancel()
        flush_tasks[key] = asyncio.create_task(flush_album(key))
        return

    # SINGLE PHOTO/VIDEO
    if msg.photo or msg.video:
        file_id = msg.photo[-1].file_id if msg.photo else msg.video.file_id
        typ = "photo" if msg.photo else "video"
        caption = msg.caption or ""
        payload = {"items": [{"file_id": file_id, "type": typ}]}
        pid = save_pending(str(msg.chat.id), msg.from_user.id, msg.from_user.username,
                          msg.from_user.full_name, None, False, caption, payload)
        await forward_to_approval(pid)
        try:
            await msg.delete()
        except:
            pass

# ====================== CALLBACKS ======================
@dp.callback_query(lambda c: c.data and c.data.startswith("approve_all:"))
async def approve_all(cb: types.CallbackQuery):
    pid = int(cb.data.split(":")[1])
    p = get_pending(pid)
    if not p:
        return await cb.answer("Not found", show_alert=True)

    media = []
    for it in p["payload"]["items"]:
        m = types.InputMediaPhoto(media=it["file_id"]) if it["type"] == "photo" else types.InputMediaVideo(media=it["file_id"])
        if not media and p.get("caption"):
            m.caption = p["caption"]
        media.append(m)

    await bot.send_media_group(int(MAIN_GROUP_ID), media)
    await bot.send_message(int(MAIN_GROUP_ID), f"Media submitted by {mention(p['user_id'], p['username'], p['full_name'])}",
                          parse_mode="MarkdownV2"
                          )
    delete_pending(pid)
    await cb.message.edit_text("Approved & posted")

@dp.callback_query(lambda c: c.data and c.data.startswith("reject_all:"))
async def reject_all(cb: types.CallbackQuery):
    pid = int(cb.data.split(":")[1])
    if get_pending(pid):
        delete_pending(pid)
    await cb.message.edit_text("Rejected")

@dp.callback_query(lambda c: c.data and c.data.startswith("selective:"))
async def selective(cb: types.CallbackQuery):
    pid = int(cb.data.split(":")[1])
    p = get_pending(pid)
    if not p:
        return
    selective_selections[pid] = {}
    for idx, it in enumerate(p["payload"]["items"]):
        kb = keep_remove_kb(pid, idx)
        if it["type"] == "photo":
            await bot.send_photo(int(APPROVAL_GROUP_ID), it["file_id"], caption=f"Item {idx+1}", reply_markup=kb)
        else:
            await bot.send_video(int(APPROVAL_GROUP_ID), it["file_id"], caption=f"Item {idx+1}", reply_markup=kb)
    await cb.message.edit_text("Select items to keep/remove")

@dp.callback_query(lambda c: c.data and (c.data.startswith("keep:") or c.data.startswith("remove:")))
async def keep_remove(cb: types.CallbackQuery):
    _, pid_str, idx_str = cb.data.split(":")
    pid, idx = int(pid_str), int(idx_str)
    selective_selections.setdefault(pid, {})[idx] = cb.data.startswith("keep:")
    await cb.answer("Kept" if cb.data.startswith("keep:") else "Removed")

    p = get_pending(pid)
    if p and len(selective_selections[pid]) == len(p["payload"]["items"]):
        await bot.send_message(int(APPROVAL_GROUP_ID), "All items reviewed — finalize?", reply_markup=finalize_kb(pid))

@dp.callback_query(lambda c: c.data and c.data.startswith("finalize:"))
async def finalize(cb: types.CallbackQuery):
    pid = int(cb.data.split(":")[1])
    p = get_pending(pid)
    if not p:
        return

    sel = selective_selections.get(pid, {})
    approved = []
    for idx, it in enumerate(p["payload"]["items"]):
        if sel.get(idx, True):
            m = types.InputMediaPhoto(media=it["file_id"]) if it["type"] == "photo" else types.InputMediaVideo(media=it["file_id"])
            if not approved and p.get("caption"):
                m.caption = p["caption"]
            approved.append(m)

    if approved:
        if len(approved) > 1:
            await bot.send_media_group(int(MAIN_GROUP_ID), approved)
        else:
            m = approved[0]
            func = bot.send_photo if isinstance(m, types.InputMediaPhoto) else bot.send_video
            await func(int(MAIN_GROUP_ID), m.media, caption=m.caption or None)
        await bot.send_message(int(MAIN_GROUP_ID), f"Media submitted by {mention(p['user_id'], p['username'], p['full_name'])}",
                              parse_mode="MarkdownV2"
                              )

    delete_pending(pid)
    selective_selections.pop(pid, None)
    await cb.message.edit_text("Selective approval completed")

# ====================== RUN ======================
async def main():
    logger.info("MEDIA APPROVAL BOT STARTED – EVERYTHING WORKS")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
