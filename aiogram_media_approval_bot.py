#!/usr/bin/env python3
"""
Aiogram v3 - Media Approval Bot (single-file)

Features:
- Photos & Videos only (single + albums)
- Deletes original media in main group, forwards to approval group for review
- Admins can Approve All / Reject All / Approve Selectively
- Preserves original caption (attached to first media item in an album)
- Posts attribution message under reposted media in main group
- SQLite storage for pending submissions
- Admin bypass via ADMIN_IDS env var
- Uses uvloop for performance

Environment variables required:
- BOT_TOKEN
- MAIN_GROUP_ID
- APPROVAL_GROUP_ID
- ADMIN_IDS (optional, comma-separated)
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

# ---------- Configuration (env) ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAIN_GROUP_ID = os.getenv("MAIN_GROUP_ID")  # e.g. -1001234567890
APPROVAL_GROUP_ID = os.getenv("APPROVAL_GROUP_ID")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")

if not BOT_TOKEN or not MAIN_GROUP_ID or not APPROVAL_GROUP_ID:
    raise SystemExit("Please set BOT_TOKEN, MAIN_GROUP_ID and APPROVAL_GROUP_ID environment variables")

# parse ADMIN_IDS into set of ints
ADMIN_IDS = set()
if ADMIN_IDS_RAW:
    for part in ADMIN_IDS_RAW.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ADMIN_IDS.add(int(part))
        except ValueError:
            # ignore invalid values
            pass

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- SQLite helpers ----------
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
            media_group_id TEXT,
            is_album INTEGER,
            caption TEXT,
            created_at TEXT,
            payload TEXT
        )
        """
    )
    conn.commit()
    conn.close()

def save_pending(chat_id, user_id, username, media_group_id, is_album, caption, payload: dict) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    created_at = datetime.now(timezone.utc).isoformat()
    cur.execute(
        "INSERT INTO pending (chat_id, user_id, username, media_group_id, is_album, caption, created_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(chat_id), int(user_id), username or "", media_group_id or "", int(bool(is_album)), caption or "", created_at, json.dumps(payload)),
    )
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid

def get_pending(pending_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, chat_id, user_id, username, media_group_id, is_album, caption, created_at, payload FROM pending WHERE id=?", (pending_id,))
    r = cur.fetchone()
    conn.close()
    if not r:
        return None
    return {
        "id": r[0],
        "chat_id": r[1],
        "user_id": r[2],
        "username": r[3],
        "media_group_id": r[4],
        "is_album": bool(r[5]),
        "caption": r[6],
        "created_at": r[7],
        "payload": json.loads(r[8]),
    }

def delete_pending(pending_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM pending WHERE id=?", (pending_id,))
    conn.commit()
    conn.close()

# ---------- Bot & Dispatcher ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

init_db()

# ---------- In-memory buffers ----------
# media_buffer: collects media items that belong to the same media_group
# key = f"{chat_id}:{media_group_id}"
media_buffer: Dict[str, List[dict]] = {}
MEDIA_GROUP_TIMEOUT = 2.0  # seconds to wait for rest of album to arrive

# selection_store stores selective approvals: pending_id -> {index: bool}
selection_store: Dict[int, Dict[int, bool]] = {}

# ---------- Utilities ----------
def build_approval_keyboard(pending_id: int) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Approve all", callback_data=f"approve_all:{pending_id}")
    builder.button(text="❌ Reject all", callback_data=f"reject_all:{pending_id}")
    builder.button(text="✂ Approve selectively", callback_data=f"selective:{pending_id}")
    builder.adjust(2, 1)  # first row 2 buttons, second row 1 button
    return builder.as_markup()

def build_finalize_keyboard(pending_id: int) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Finalize and post approved", callback_data=f"finalize:{pending_id}")
    return builder.as_markup()

def build_keep_remove_keyboard(pending_id: int, idx: int) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Keep", callback_data=f"keep:{pending_id}:{idx}")
    builder.button(text="❌ Remove", callback_data=f"remove:{pending_id}:{idx}")
    builder.adjust(2)
    return builder.as_markup()

def user_is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ---------- Media group flush ----------
async def schedule_media_group_flush(mgid: str):
    await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
    items = media_buffer.pop(mgid, [])
    if not items:
        return
    # Save pending submission
    first = items[0]
    caption = first.get("caption", "")
    payload = {"items": items}
    pending_id = save_pending(first["chat_id"], first["user_id"], first.get("username") or "", mgid, True, caption, payload)
    await forward_to_approval_group(pending_id)

# ---------- Forward to approval group ----------
async def forward_to_approval_group(pending_id: int):
    pending = get_pending(pending_id)
    if not pending:
        return
    items = pending["payload"]["items"]
    media = []
    # Build InputMedia objects for upload
    for idx, it in enumerate(items):
        if it["type"] == "photo":
            im = types.InputMediaPhoto(media=it["file_id"])
        else:
            im = types.InputMediaVideo(media=it["file_id"])
        # attach caption to first item if exists
        if idx == 0 and pending["caption"]:
            im.caption = pending["caption"]
        media.append(im)
    try:
        # Send as media group if many, else send single media
        if len(media) > 1:
            await bot.send_media_group(chat_id=int(APPROVAL_GROUP_ID), media=media)
        else:
            single = media[0]
            if isinstance(single, types.InputMediaPhoto):
                await bot.send_photo(chat_id=int(APPROVAL_GROUP_ID), photo=single.media, caption=single.caption)
            else:
                await bot.send_video(chat_id=int(APPROVAL_GROUP_ID), video=single.media, caption=single.caption)
    except Exception as e:
        logger.exception("failed to forward media to approval group: %s", e)
        # still send control message so admins can know
    # send control message with inline buttons
    try:
        kb = build_approval_keyboard(pending_id)
        username = pending.get("username") or ""
        await bot.send_message(chat_id=int(APPROVAL_GROUP_ID), text=f"New submission from @{username} (id:{pending_id})", reply_markup=kb)
    except Exception as e:
        logger.exception("failed to send approval control message: %s", e)

# ---------- Handlers ----------
@dp.message(Command(commands=["start"]))
async def cmd_start_handler(message: types.Message):
    await message.reply("Media approval bot active.")

@dp.message()
async def on_message_handler(message: types.Message):
    # ignore bots
    if not message or message.from_user.is_bot:
        return

    # Only process messages in the MAIN_GROUP_ID
    try:
        if str(message.chat.id) != str(MAIN_GROUP_ID):
            return
    except Exception:
        return

    # Allow admins (by ADMIN_IDS) to post normally
    if message.from_user and user_is_admin(message.from_user.id):
        return

    # ---- Handle albums (media_group_id) ----
    if message.media_group_id:
        mgid_key = f"{message.chat.id}:{message.media_group_id}"
        arr = media_buffer.setdefault(mgid_key, [])
        file_id = None
        mtype = None
        if message.photo:
            file_id = message.photo[-1].file_id
            mtype = "photo"
        elif message.video:
            file_id = message.video.file_id
            mtype = "video"
        else:
            # unsupported media in album; ignore
            return
        arr.append({
            "file_id": file_id,
            "type": mtype,
            "user_id": message.from_user.id,
            "chat_id": message.chat.id,
            "username": message.from_user.username or message.from_user.full_name,
            "caption": message.caption or ""
        })
        # delete the original message to hide unapproved media
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        # schedule flush only when first item arrives
        if len(arr) == 1:
            asyncio.create_task(schedule_media_group_flush(mgid_key))
        return

    # ---- Handle single photo/video ----
    if message.photo or message.video:
        file_id = message.photo[-1].file_id if message.photo else message.video.file_id
        mtype = "photo" if message.photo else "video"
        # delete original
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        payload = {"items": [{"file_id": file_id, "type": mtype, "user_id": message.from_user.id, "chat_id": message.chat.id, "username": message.from_user.username or message.from_user.full_name, "caption": message.caption or ""}]}
        pending_id = save_pending(message.chat.id, message.from_user.id, message.from_user.username or message.from_user.full_name, None, False, message.caption or "", payload)
        await forward_to_approval_group(pending_id)
        return

    # ignore other messages
    return

# ---------- Callback handlers ----------
@dp.callback_query(lambda c: c.data and c.data.startswith("approve_all:"))
async def cb_approve_all(callback: types.CallbackQuery):
    await callback.answer("Approving all...")
    try:
        pending_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.message.reply("Invalid request.")
        return
    pending = get_pending(pending_id)
    if not pending:
        await callback.message.reply("Pending item not found (stale).")
        return
    items = pending["payload"]["items"]
    media = []
    for idx, it in enumerate(items):
        if it["type"] == "photo":
            im = types.InputMediaPhoto(media=it["file_id"])
        else:
            im = types.InputMediaVideo(media=it["file_id"])
        if idx == 0 and pending["caption"]:
            im.caption = pending["caption"]
        media.append(im)
    try:
        if len(media) > 1:
            await bot.send_media_group(chat_id=int(MAIN_GROUP_ID), media=media)
        else:
            single = media[0]
            if isinstance(single, types.InputMediaPhoto):
                await bot.send_photo(chat_id=int(MAIN_GROUP_ID), photo=single.media, caption=single.caption)
            else:
                await bot.send_video(chat_id=int(MAIN_GROUP_ID), video=single.media, caption=single.caption)
        # attribution message
        await bot.send_message(chat_id=int(MAIN_GROUP_ID), text=f"📌 Album submitted by @{pending['username']}")
    except Exception as e:
        logger.exception("Failed repost to main group: %s", e)
        await callback.message.reply("Failed to repost to main group.")
        return
    delete_pending(pending_id)
    await callback.message.reply("Approved and posted to main group.")

@dp.callback_query(lambda c: c.data and c.data.startswith("reject_all:"))
async def cb_reject_all(callback: types.CallbackQuery):
    try:
        pending_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Invalid request.")
        return
    pending = get_pending(pending_id)
    if not pending:
        await callback.answer("Pending item not found (stale).")
        return
    try:
        await bot.send_message(chat_id=int(MAIN_GROUP_ID), text=f"❌ Media from @{pending['username']} was rejected by admins.")
    except Exception:
        pass
    delete_pending(pending_id)
    await callback.message.reply("Rejected and removed.")

@dp.callback_query(lambda c: c.data and c.data.startswith("selective:"))
async def cb_selective(callback: types.CallbackQuery):
    try:
        pending_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Invalid request.")
        return
    pending = get_pending(pending_id)
    if not pending:
        await callback.answer("Pending item not found (stale).")
        return
    items = pending["payload"]["items"]
    # initialize selection_store for this pending
    selection_store[pending_id] = {}
    # send each item individually with keep/remove buttons for admins to choose
    for idx, it in enumerate(items):
        kb = build_keep_remove_keyboard(pending_id, idx)
        caption = f"Item #{idx+1}"
        try:
            if it["type"] == "photo":
                await bot.send_photo(chat_id=int(APPROVAL_GROUP_ID), photo=it["file_id"], caption=caption, reply_markup=kb)
            else:
                await bot.send_video(chat_id=int(APPROVAL_GROUP_ID), video=it["file_id"], caption=caption, reply_markup=kb)
        except Exception as e:
            logger.exception("failed to send selective item: %s", e)
    await callback.message.reply("Sent items for selective approval. Mark Keep/Remove for each item.")

@dp.callback_query(lambda c: c.data and (c.data.startswith("keep:") or c.data.startswith("remove:")))
async def cb_keep_remove(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Invalid request.")
        return
    action, pending_str, idx_str = parts
    try:
        pending_id = int(pending_str)
        idx = int(idx_str)
    except Exception:
        await callback.answer("Invalid request.")
        return
    sel = selection_store.setdefault(pending_id, {})
    if action == "keep":
        sel[idx] = True
        await callback.answer("Marked as KEEP")
    else:
        sel[idx] = False
        await callback.answer("Marked as REMOVE")
    # check if all items have a selection
    pending = get_pending(pending_id)
    if not pending:
        return
    total = len(pending["payload"]["items"])
    if len(sel) == total:
        # send finalize button
        kb = build_finalize_keyboard(pending_id)
        await bot.send_message(chat_id=int(APPROVAL_GROUP_ID), text=f"All items reviewed for submission {pending_id}. Finalize?", reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("finalize:"))
async def cb_finalize(callback: types.CallbackQuery):
    try:
        pending_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Invalid request.")
        return
    pending = get_pending(pending_id)
    if not pending:
        await callback.answer("Pending item not found.")
        return
    sel = selection_store.get(pending_id, {})
    items = pending["payload"]["items"]
    approved_media: List[types.InputMedia] = []
    for idx, it in enumerate(items):
        keep = sel.get(idx, True)  # default keep if not explicitly removed
        if keep:
            if it["type"] == "photo":
                approved_media.append(types.InputMediaPhoto(media=it["file_id"]))
            else:
                approved_media.append(types.InputMediaVideo(media=it["file_id"]))
    if not approved_media:
        await callback.message.reply("No items approved — nothing to post.")
        delete_pending(pending_id)
        selection_store.pop(pending_id, None)
        return
    # attach caption to first approved item if original caption exists
    if pending["caption"]:
        approved_media[0].caption = pending["caption"]
    try:
        if len(approved_media) > 1:
            await bot.send_media_group(chat_id=int(MAIN_GROUP_ID), media=approved_media)
        else:
            single = approved_media[0]
            if isinstance(single, types.InputMediaPhoto):
                await bot.send_photo(chat_id=int(MAIN_GROUP_ID), photo=single.media, caption=single.caption)
            else:
                await bot.send_video(chat_id=int(MAIN_GROUP_ID), video=single.media, caption=single.caption)
        await bot.send_message(chat_id=int(MAIN_GROUP_ID), text=f"📌 Album submitted by @{pending['username']}")
    except Exception as e:
        logger.exception("failed to post approved selection: %s", e)
        await callback.message.reply("Failed to post to main group.")
        return
    delete_pending(pending_id)
    selection_store.pop(pending_id, None)
    await callback.message.reply("Approved selection posted.")

# ---------- Run the bot ----------
async def main():
    try:
        logger.info("Starting polling...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.exception("Error starting bot: %s", e)

if __name__ == "__main__":
    asyncio.run(main())


