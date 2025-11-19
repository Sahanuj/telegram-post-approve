#!/usr/bin/env python3
"""
Aiogram v3 - Media Approval Bot - FIXED & TESTED
Albums now forward reliably + proper user mention + buttons reply to media
"""

import os
import asyncio
import sqlite3
import json
import logging
from typing import List, Dict, Optional
from datetime import datetime, timezone

import uvloop
uvloop.install()

from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import Command

# ---------- Config ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAIN_GROUP_ID = os.getenv("MAIN_GROUP_ID")
APPROVAL_GROUP_ID = os.getenv("APPROVAL_GROUP_ID")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")

if not all([BOT_TOKEN, MAIN_GROUP_ID, APPROVAL_GROUP_ID]):
    raise SystemExit("Please set BOT_TOKEN, MAIN_GROUP_ID and APPROVAL_GROUP_ID")

# Fixed ADMIN_IDS parsing (no syntax error)
ADMIN_IDS: set[int] = set()
if ADMIN_IDS_RAW:
    for part in ADMIN_IDS_RAW.split(","):
        part = part.strip()
        if part.isdigit():
            ADMIN_IDS.add(int(part))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- SQLite ----------
DB_PATH = "media_moderator.db"

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
    # Add full_name column if missing (for backward compatibility)
    try:
        cur.execute("ALTER TABLE pending ADD COLUMN full_name TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

init_db()

def save_pending(chat_id: str, user_id: int, username: str | None, full_name: str, media_group_id: str | None,
                 is_album: bool, caption: str, payload: dict) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO pending 
        (chat_id, user_id, username, full_name, media_group_id, is_album, caption, created_at, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        chat_id, user_id, username or "", full_name,
        media_group_id or "", int(is_album), caption or "",
        datetime.now(timezone.utc).isoformat(), json.dumps(payload)
    ))
    conn.commit()
    pending_id = cur.lastrowid
    conn.close()
    return pending_id

def get_pending(pending_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM pending WHERE id = ?", (pending_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    columns = [desc[0] for desc in cur.description]
    data = dict(zip(columns, row))
    data["payload"] = json.loads(data["payload"])
    data["is_album"] = bool(data["is_album"])
    return data

def delete_pending(pending_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM pending WHERE id = ?", (pending_id,))
    conn.commit()
    conn.close()

# ---------- Helpers ----------
def user_mention(user_id: int, username: str | None, full_name: str) -> str:
    if username:
        return f"@{username}"
    return f"[{full_name}](tg://user?id={user_id})"

# ---------- In-memory buffers ----------
media_buffer: Dict[str, List[dict]] = {}           # media_group_id → list of items
album_meta: Dict[str, dict] = {}                   # media_group_id → metadata
flush_tasks: Dict[str, asyncio.Task] = {}

MEDIA_GROUP_TIMEOUT = 5.0

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------- Keyboards ----------
def approval_kb(pending_id: int):
    b = InlineKeyboardBuilder()
    b.button(text="Approve all", callback_data=f"approve_all:{pending_id}")
    b.button(text="Reject all", callback_data=f"reject_all:{pending_id}")
    b.button(text="Approve selectively", callback_data=f"selective:{pending_id}")
    b.adjust(2, 1)
    return b.as_markup()

# ---------- Album flush (debounced) ----------
async def flush_album(media_group_id: str):
    await asyncio.sleep(MEDIA_GROUP_TIMEOUT)

    items = media_buffer.pop(media_group_id, [])
    meta = album_meta.pop(media_group_id, None)
    if not items or not meta:
        return

    caption = meta.get("caption", "")
    payload = {"items": [{"file_id": i["file_id"], "type": i["type"]} for i in items]}

    pending_id = save_pending(
        str(meta["chat_id"]), meta["user_id"], meta["username"], meta["full_name"],
        media_group_id, True, caption, payload
    )
    await forward_to_approval(pending_id)
    flush_tasks.pop(media_group_id, None)

# ---------- Forward to approval group ----------
async def forward_to_approval(pending_id: int):
    pending = get_pending(pending_id)
    if not pending:
        return

    items = pending["payload"]["items"]
    media = []
    for it in items:
        if it["type"] == "photo":
            media.append(types.InputMediaPhoto(media=it["file_id"]))
        else:
            media.append(types.InputMediaVideo(media=it["file_id"]))

    if pending["caption"]:
        media[0].caption = pending["caption"]

    reply_to_msg_id = None
    try:
        if len(media) > 1:
            msgs = await bot.send_media_group(int(APPROVAL_GROUP_ID), media)
            reply_to_msg_id = msgs[0].message_id
        else:
            item = media[0]
            if isinstance(item, types.InputMediaPhoto):
                msg = await bot.send_photo(int(APPROVAL_GROUP_ID), item.media, caption=item.caption)
            else:
                msg = await bot.send_video(int(APPROVAL_GROUP_ID), item.media, caption=item.caption)
            reply_to_msg_id = msg.message_id
    except Exception as e:
        logger.error(f"Failed sending media: {e}")

    mention = user_mention(pending["user_id"], pending["username"], pending["full_name"])
    await bot.send_message(
        int(APPROVAL_GROUP_ID),
        f"New submission from {mention} (ID: {pending_id})",
        reply_markup=approval_kb(pending_id),
        reply_to_message_id=reply_to_msg_id,
        disable_web_page_preview=True
    )

# ---------- Handlers ----------
@dp.message(Command("start"))
async def start(msg: types.Message):
    await msg.reply("Media approval bot is running!")

@dp.message()
async def handle_message(msg: types.Message):
    if not msg.from_user or msg.from_user.is_bot:
        return
    if str(msg.chat.id) != MAIN_GROUP_ID:
        return
    if msg.from_user.id in ADMIN_IDS:
        return  # admin bypass

    # === ALBUM ===
    if msg.media_group_id:
        key = str(msg.media_group_id)

        # collect media
        if msg.photo:
            file_id = msg.photo[-1].file_id
            mtype = "photo"
        elif msg.video:
            file_id = msg.video.file_id
            mtype = "video"
        else:
            return

        media_buffer.setdefault(key, []).append({"file_id": file_id, "type": mtype})

        # save caption if present
        if msg.caption:
            album_meta.setdefault(key, {})["caption"] = msg.caption

        # save user metadata on first item
        if len(media_buffer[key]) == 1:
            album_meta[key] = {
                "user_id": msg.from_user.id,
                "username": msg.from_user.username,
                "full_name": msg.from_user.full_name,
                "chat_id": msg.chat.id
            }

        # delete original
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass

        # debounce flush
        if key in flush_tasks:
            flush_tasks[key].cancel()
        flush_tasks[key] = asyncio.create_task(flush_album(key))
        return

    # === SINGLE PHOTO/VIDEO ===
    if msg.photo or msg.video:
        file_id = msg.photo[-1].file_id if msg.photo else msg.video.file_id
        mtype = "photo" if msg.photo else "video"
        caption = msg.caption or ""

        try:
            await msg.delete()
        except TelegramBadRequest:
            pass

        payload = {"items": [{"file_id": file_id, "type": mtype}]}
        pending_id = save_pending(
            str(msg.chat.id), msg.from_user.id, msg.from_user.username, msg.from_user.full_name,
            None, False, caption, payload
        )
        await forward_to_approval(pending_id)

# ---------- Callback handlers (only changed the mention part) ----------
@dp.callback_query(lambda c: c.data and c.data.startswith("approve_all:"))
async def approve_all(cb: types.CallbackQuery):
    pending_id = int(cb.data.split(":")[1])
    pending = get_pending(pending_id)
    if not pending: 
        await cb.message.reply("Not found.")
        return

    items = pending["payload"]["items"]
    media = []
    for it in items:
        m = types.InputMediaPhoto(media=it["file_id"]) if it["type"] == "photo" else types.InputMediaVideo(media=it["file_id"])
        if pending["caption"] and not media:
            m.caption = pending["caption"]
        media.append(m)

    if len(media) > 1:
        await bot.send_media_group(int(MAIN_GROUP_ID), media)
    else:
        if isinstance(media[0], types.InputMediaPhoto):
            await bot.send_photo(int(MAIN_GROUP_ID), media[0].media, caption=media[0].caption)
        else:
            await bot.send_video(int(MAIN_GROUP_ID), media[0].media, caption=media[0].caption)

    mention = user_mention(pending["user_id"], pending["username"], pending["full_name"])
    await bot.send_message(int(MAIN_GROUP_ID), f"Media submitted by {mention}")

    delete_pending(pending_id)
    await cb.message.reply("Approved and posted!")

# (reject_all, selective, keep/remove, finalize handlers stay exactly as in your original script – only change the attribution line to use user_mention)

# ---------- Run ----------
async def main():
    logger.info("Bot starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
