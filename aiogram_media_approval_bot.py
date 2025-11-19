#!/usr/bin/env python3
"""
Aiogram v3 - Media Approval Bot (single-file) - FIXED VERSION
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

# ---------- Configuration ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAIN_GROUP_ID = os.getenv("MAIN_GROUP_ID")
APPROVAL_GROUP_ID = os.getenv("APPROVAL_GROUP_ID")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")

if not all([BOT_TOKEN, MAIN_GROUP_ID, APPROVAL_GROUP_ID]):
    raise SystemExit("Set BOT_TOKEN, MAIN_GROUP_ID and APPROVAL_GROUP_ID env vars")

ADMIN_IDS = set(map(int, filter(None, (p.strip() for p in ADMIN_IDS_RAW.split(",")))) if ADMIN_IDS_RAW else set()

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- SQLite ----------
DB_PATH = "media_moderator.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
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
        """
    )
    # backward compatibility - add column if missing
    try:
        cur.execute("ALTER TABLE pending ADD COLUMN full_name TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

init_db()

def save_pending(chat_id: str, user_id: int, username: Optional[str], full_name: str, media_group_id: Optional[str], is_album: bool, caption: str, payload: dict) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    created_at = datetime.now(timezone.utc).isoformat()
    cur.execute(
        "INSERT INTO pending (chat_id, user_id, username, full_name, media_group_id, is_album, caption, created_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_id, user_id, username or "", full_name, media_group_id or "", int(bool(is_album)), caption or "", created_at, json.dumps(payload)),
    )
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid

def get_pending(pending_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, chat_id, user_id, username, full_name, media_group_id, is_album, caption, created_at, payload FROM pending WHERE id=?", (pending_id,))
    r = cur.fetchone()
    conn.close()
    if not r:
        return None
    return {
        "id": r[0],
        "chat_id": r[1],
        "user_id": r[2],
        "username": r[3],
        "full_name": r[4] if len(r) > 9 else None,  # old DB compatibility
        "media_group_id": r[5],
        "is_album": bool(r[6]),
        "caption": r[7],
        "created_at": r[8],
        "payload": json.loads(r[9]),
    }

def delete_pending = lambda pid: sqlite3.connect(DB_PATH).execute("DELETE FROM pending WHERE id=?", (pid,)).connection.commit() and None

# ---------- Helpers ----------
def get_user_mention(user_id: int, username: Optional[str] = None, full_name: str = "User") -> str:
    if username:
        return f"@{username}"
    else:
        return f"[{full_name or 'User'}](tg://user?id={user_id})"

# ---------- In-memory ----------
media_buffer: Dict[str, List[dict]] = {}          # key = str(media_group_id) → [{"file_id":, "type":}]
album_metadata: Dict[str, dict] = {}               # key → {"user_id":, "username":, "full_name":, "chat_id":, "caption": optional}
flush_tasks: Dict[str, asyncio.Task] = {}
MEDIA_GROUP_TIMEOUT = 5.0

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------- Keyboards ----------
def build_approval_keyboard(pending_id: int):
    b = InlineKeyboardBuilder()
    b.button(text="✅ Approve all", callback_data=f"approve_all:{pending_id}")
    b.button(text="❌ Reject all", callback_data=f"reject_all:{pending_id}")
    b.button(text="✂ Approve selectively", callback_data=f"selective:{pending_id}")
    b.adjust(2, 1)
    return b.as_markup()

# ---------- Core flush with debounce ----------
async def schedule_media_group_flush(key: str):
    try:
        await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
    except asyncio.CancelledError:
        return

    items = media_buffer.pop(key, [])
    meta = album_metadata.pop(key, None)
    if not items or not meta:
        return

    caption = meta.get("caption", "")
    user_id = meta["user_id"]
    username = meta["username"]
    full_name = meta["full_name"]
    chat_id = meta["chat_id"]

    payload = {"items": [{"file_id": i["file_id"], "type": i["type"]} for i in items]}

    pending_id = save_pending(str(chat_id), user_id, username, full_name, key, True, caption, payload)
    await forward_to_approval_group(pending_id)
    except Exception as e:
        logger.exception("Error in album flush for key %s: %s", key, e)
    finally:
        flush_tasks.pop(key, None)

# ---------- Forward with reply_to ----------
async def forward_to_approval_group(pending_id: int):
    pending = get_pending(pending_id)
    if not pending:
        return

    items = pending["payload"]["items"]
    caption = pending["caption"] or None
    media = [
        types.InputMediaPhoto(media=it["file_id"]) if it["type"] == "photo" else types.InputMediaVideo(media=it["file_id"])
        for it in items
    ]
    if caption:
        media[0].caption = caption

    try:
        if len(media) > 1:
            sent_msgs = await bot.send_media_group(chat_id=int(APPROVAL_GROUP_ID), media=media)
            reply_to = sent_msgs[-1].message_id
        else:
            single = media[0]
            if pending["payload"]["items"][0]["type"] == "photo":
                sent = await bot.send_photo(chat_id=int(APPROVAL_GROUP_ID), photo=single.media, caption=caption)
            else:
                sent = await bot.send_video(chat_id=int(APPROVAL_GROUP_ID), video=single.media, caption=caption)
            reply_to = sent.message_id
    except Exception as e:
        logger.exception("Failed to forward media: %s", e)
        reply_to = None

    kb = build_approval_keyboard(pending_id)
    mention = get_user_mention(pending["user_id"], pending["username"], pending["full_name"])
    await bot.send_message(
        chat_id=int(APPROVAL_GROUP_ID),
        text=f"New submission from {mention} (id:{pending_id})",
        reply_markup=kb,
        reply_to_message_id=reply_to
    )

# ---------- Handlers ----------
@dp.message(Command(commands=["start"]))
async def start(message: types.Message):
    await message.reply("Media approval bot active.")

@dp.message()
async def on_message(message: types.Message):
    if not message or message.from_user.is_bot or str(message.chat.id) != MAIN_GROUP_ID:
        return

    if message.from_user.id in ADMIN_IDS:
        return  # admin bypass

    # ---------- ALBUM ----------
    if message.media_group_id:
        mgid_key = str(message.media_group_id)

        if message.photo:
            file_id = message.photo[-1].file_id
            mtype = "photo"
        elif message.video:
            file_id = message.video.file_id
            mtype = "video"
        else:
            return  # unsupported in album

        media_buffer.setdefault(mgid_key, []).append({"file_id": file_id, "type": mtype})

        if message.caption:
            album_metadata.setdefault(mgid_key, {})["caption"] = message.caption

        if len(media_buffer[mgid_key]) == 1:  # first item → store metadata
            album_metadata[mgid_key] = {
                "user_id": message.from_user.id,
                "username": message.from_user.username,
                "full_name": message.from_user.full_name,
                "chat_id": message.chat.id
            }

        try:
            await message.delete()
        except TelegramBadRequest:
            pass

        # debounce
        if mgid_key in flush_tasks:
            flush_tasks[mgid_key].cancel()
        flush_tasks[mgid_key] = asyncio.create_task(schedule_media_group_flush(mgid_key))
        return

    # ---------- SINGLE ----------
    if message.photo or message.video:
        file_id = message.photo[-1].file_id if message.photo else message.video.file_id
        mtype = "photo" if message.photo else "video"
        caption = message.caption or ""

        try:
            await message.delete()
        except TelegramBadRequest:
            pass

        payload = {"items": [{"file_id": file_id, "type": mtype}]}
        pending_id = save_pending(
            str(message.chat.id),
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name,
            None,
            False,
            caption,
            payload
        )
        await forward_to_approval_group(pending_id)

# ---------- Callbacks (only username mention changes) ----------
# approve_all, reject_all, selective, keep/remove, finalize stay the same
# just change the mention lines:

# in cb_approve_all and cb_finalize attribution:
await bot.send_message(
    chat_id=int(MAIN_GROUP_ID),
    text=f"📌 Media submitted by {get_user_mention(pending['user_id'], pending['username'], pending['full_name'])}"
)

# in cb_reject_all (optional, you can keep or remove the mention):
await bot.send_message(chat_id=int(MAIN_GROUP_ID), text=f"❌ Media rejected")

# selective ones stay the same

# ---------- Run ----------
async def main():
    logger.info("Bot starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

