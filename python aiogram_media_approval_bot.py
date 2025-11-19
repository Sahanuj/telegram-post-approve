"""
Telegram Media Approval Bot (Aiogram)
- Handles photos & videos only
- Supports single media and media albums (media_group)
- Stores pending submissions in SQLite
- Admins approve/reject in an approval group
- On approve: bot reposts media to main group preserving original caption
- Posts a small attribution message under the repost

Files included in this single-code document:
- aiogram_media_approval_bot.py  (main bot)
- requirements.txt (as comment)
- Procfile (as comment, for Railway)
- README usage (as comment)

Set environment variables:
- BOT_TOKEN  (your bot token)
- MAIN_GROUP_ID  (the integer ID or @username of main group)
- APPROVAL_GROUP_ID (the approval/admin group where admins review submissions)
- ADMIN_IDS (optional, comma-separated admin user IDs who can bypass)

Note: This script uses long-polling by default (simple). For webhook deployment on Railway, replace the bottom "start polling" with webhook integration (I can provide that if you want).
"""

import os
import asyncio
import sqlite3
import json
import logging
from typing import List, Dict, Optional
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest

# -----------------------------
# Configuration (from ENV)
# -----------------------------
BOT_TOKEN = os.getenv('BOT_TOKEN')
MAIN_GROUP_ID = os.getenv('MAIN_GROUP_ID')  # e.g. -1001234567890
APPROVAL_GROUP_ID = os.getenv('APPROVAL_GROUP_ID')  # where admins will review
ADMIN_IDS = os.getenv('ADMIN_IDS', '')  # optional comma-separated user ids

if not BOT_TOKEN or not MAIN_GROUP_ID or not APPROVAL_GROUP_ID:
    raise SystemExit('Please set BOT_TOKEN, MAIN_GROUP_ID and APPROVAL_GROUP_ID environment variables')

ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS.split(',') if x.strip()} if ADMIN_IDS else set()

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -----------------------------
# SQLite helper
# -----------------------------
DB_PATH = 'media_moderator.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
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
    ''')
    conn.commit()
    conn.close()

def save_pending(chat_id, user_id, username, media_group_id, is_album, caption, payload: dict):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''INSERT INTO pending (chat_id, user_id, username, media_group_id, is_album, caption, created_at, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (str(chat_id), user_id, username, media_group_id or '', int(bool(is_album)), caption or '', datetime.utcnow().isoformat(), json.dumps(payload)))
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid

def get_pending(pending_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT id, chat_id, user_id, username, media_group_id, is_album, caption, created_at, payload FROM pending WHERE id=?', (pending_id,))
    r = cur.fetchone()
    conn.close()
    if not r:
        return None
    return {
        'id': r[0], 'chat_id': r[1], 'user_id': r[2], 'username': r[3], 'media_group_id': r[4], 'is_album': bool(r[5]),
        'caption': r[6], 'created_at': r[7], 'payload': json.loads(r[8])
    }

def delete_pending(pending_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('DELETE FROM pending WHERE id=?', (pending_id,))
    conn.commit()
    conn.close()

# -----------------------------
# Bot setup
# -----------------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

init_db()

# In-memory temporary buffer for grouping media_group items until we save them - small window
media_buffer: Dict[str, List[dict]] = {}
MEDIA_GROUP_TIMEOUT = 2.0  # seconds to wait for album completion

async def schedule_media_group_flush(media_group_id: str):
    await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
    items = media_buffer.pop(media_group_id, [])
    if not items:
        return
    # Save pending submission using file_ids and metadata
    first = items[0]
    caption = first.get('caption')
    payload = {'items': items}
    pending_id = save_pending(first['chat_id'], first['user_id'], first.get('username') or '', media_group_id, True, caption, payload)
    await forward_to_approval_group(pending_id)

async def forward_to_approval_group(pending_id: int):
    pending = get_pending(pending_id)
    if not pending:
        return
    items = pending['payload']['items']
    media = []
    for it in items:
        if it['type'] == 'photo':
            media.append(InputMediaPhoto(media=it['file_id']))
        else:
            media.append(InputMediaVideo(media=it['file_id']))
    try:
        # send as media group to approval chat
        await bot.send_media_group(chat_id=int(APPROVAL_GROUP_ID), media=media)
    except Exception as e:
        logger.exception('failed to forward album to approval group: %s', e)
    # Send control message with approve/reject buttons
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton('✅ Approve all', callback_data=f'approve_all:{pending_id}'), InlineKeyboardButton('❌ Reject all', callback_data=f'reject_all:{pending_id}')],
        [InlineKeyboardButton('✂ Approve selectively', callback_data=f'selective:{pending_id}')]
    ])
    await bot.send_message(chat_id=int(APPROVAL_GROUP_ID), text=f'New submission from @{pending["username"]} (id:{pending_id})', reply_markup=kb)

# -----------------------------
# Utilities
# -----------------------------
def is_user_admin(user_id: int, chat: types.Chat) -> bool:
    # We rely on ADMIN_IDS env or assume approval group contains admins who will moderate
    return user_id in ADMIN_IDS

# -----------------------------
# Handlers
# -----------------------------
@dp.message(Command(commands=['start']))
async def cmd_start(message: types.Message):
    await message.reply('Media approval bot active.')

@dp.message()
async def on_message(message: types.Message):
    # Ignore messages from bots
    if message.from_user.is_bot:
        return
    # If user is admin in MAIN_GROUP_ID, skip approval
    try:
        # Simple bypass: if user is in ADMIN_IDS set
        if message.chat and str(message.chat.id) == str(MAIN_GROUP_ID) and message.from_user.id in ADMIN_IDS:
            return  # allow admins to post freely
    except Exception:
        pass

    # Only handle messages in the main group
    if not message.chat or str(message.chat.id) != str(MAIN_GROUP_ID):
        return

    # Only process photos or videos (single or album)
    if message.media_group_id:
        # it's part of an album
        mgid = f"{message.chat.id}:{message.media_group_id}"
        arr = media_buffer.setdefault(mgid, [])
        file_id = None
        mtype = None
        if message.photo:
            file_id = message.photo[-1].file_id
            mtype = 'photo'
        elif message.video:
            file_id = message.video.file_id
            mtype = 'video'
        else:
            return
        arr.append({'file_id': file_id, 'type': mtype, 'user_id': message.from_user.id, 'chat_id': message.chat.id, 'username': message.from_user.username or message.from_user.full_name, 'caption': message.caption or ''})
        # delete the user's message to hide unapproved media
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        # schedule flush (only once)
        if len(arr) == 1:
            asyncio.create_task(schedule_media_group_flush(mgid))
        return

    # Non-album single photo/video
    if message.photo or message.video:
        file_id = message.photo[-1].file_id if message.photo else message.video.file_id
        mtype = 'photo' if message.photo else 'video'
        # delete the user's message
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        payload = {'items': [{'file_id': file_id, 'type': mtype}]}
        pending_id = save_pending(message.chat.id, message.from_user.id, message.from_user.username or message.from_user.full_name, None, False, message.caption or '', payload)
        await forward_to_approval_group(pending_id)
        return

    # other messages: ignore

# -----------------------------
# Callback handlers for approval actions
# -----------------------------
@dp.callback_query(lambda c: c.data and c.data.startswith('approve_all:'))
async def cb_approve_all(callback: types.CallbackQuery):
    await callback.answer('Approving all...')
    pending_id = int(callback.data.split(':', 1)[1])
    pending = get_pending(pending_id)
    if not pending:
        await callback.message.reply('Pending item not found (stale).')
        return
    items = pending['payload']['items']
    # repost to main group preserving caption
    media = []
    for idx, it in enumerate(items):
        if it['type'] == 'photo':
            media.append(InputMediaPhoto(media=it['file_id'], caption=pending['caption'] if idx == 0 and pending['caption'] else None))
        else:
            media.append(InputMediaVideo(media=it['file_id'], caption=pending['caption'] if idx == 0 and pending['caption'] else None))
    try:
        await bot.send_media_group(chat_id=int(MAIN_GROUP_ID), media=media)
        # attribution message
        await bot.send_message(chat_id=int(MAIN_GROUP_ID), text=f'📌 Album submitted by @{pending["username"]}')
    except Exception as e:
        logger.exception('Failed repost album: %s', e)
        await callback.message.reply('Failed to repost to main group.')
        return
    delete_pending(pending_id)
    await callback.message.reply('Approved and posted to main group.')

@dp.callback_query(lambda c: c.data and c.data.startswith('reject_all:'))
async def cb_reject_all(callback: types.CallbackQuery):
    pending_id = int(callback.data.split(':', 1)[1])
    pending = get_pending(pending_id)
    if not pending:
        await callback.answer('Pending item not found (stale).')
        return
    try:
        await bot.send_message(chat_id=int(MAIN_GROUP_ID), text=f'❌ Media from @{pending["username"]} was rejected by admins.')
    except Exception:
        pass
    delete_pending(pending_id)
    await callback.message.reply('Rejected and removed.')

@dp.callback_query(lambda c: c.data and c.data.startswith('selective:'))
async def cb_selective(callback: types.CallbackQuery):
    pending_id = int(callback.data.split(':', 1)[1])
    pending = get_pending(pending_id)
    if not pending:
        await callback.answer('Pending item not found (stale).')
        return
    items = pending['payload']['items']
    # Send a control message listing items with approve/reject for each
    for idx, it in enumerate(items):
        # send each item individually with inline approve/reject
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton('✅ Keep', callback_data=f'keep:{pending_id}:{idx}'), InlineKeyboardButton('❌ Remove', callback_data=f'remove:{pending_id}:{idx}')]
        ])
        try:
            if it['type'] == 'photo':
                await bot.send_photo(chat_id=int(APPROVAL_GROUP_ID), photo=it['file_id'], caption=f'Item #{idx+1}', reply_markup=kb)
            else:
                await bot.send_video(chat_id=int(APPROVAL_GROUP_ID), video=it['file_id'], caption=f'Item #{idx+1}', reply_markup=kb)
        except Exception as e:
            logger.exception('failed to send selective item: %s', e)
    await callback.message.reply('Sent items for selective approval.')

# temporary storage for selections (in-memory)
selection_store: Dict[int, Dict[int, bool]] = {}  # pending_id -> {index: keep_bool}

@dp.callback_query(lambda c: c.data and (c.data.startswith('keep:') or c.data.startswith('remove:')))
async def cb_keep_remove(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    action = parts[0]
    pending_id = int(parts[1])
    idx = int(parts[2])
    sel = selection_store.setdefault(pending_id, {})
    if action == 'keep':
        sel[idx] = True
        await callback.answer('Marked as KEEP')
    else:
        sel[idx] = False
        await callback.answer('Marked as REMOVE')
    # Provide a finalization button when all items have a selection
    pending = get_pending(pending_id)
    if not pending:
        return
    total = len(pending['payload']['items'])
    if len(sel) == total:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton('✅ Finalize and post approved', callback_data=f'finalize:{pending_id}')]])
        await bot.send_message(chat_id=int(APPROVAL_GROUP_ID), text=f'All items reviewed for submission {pending_id}. Finalize?', reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith('finalize:'))
async def cb_finalize(callback: types.CallbackQuery):
    pending_id = int(callback.data.split(':', 1)[1])
    pending = get_pending(pending_id)
    if not pending:
        await callback.answer('Pending item not found.')
        return
    sel = selection_store.get(pending_id, {})
    items = pending['payload']['items']
    approved_media = []
    for idx, it in enumerate(items):
        keep = sel.get(idx, True)  # default keep if not selected
        if keep:
            if it['type'] == 'photo':
                approved_media.append(InputMediaPhoto(media=it['file_id']))
            else:
                approved_media.append(InputMediaVideo(media=it['file_id']))
    if not approved_media:
        await callback.message.reply('No items approved — nothing to post.')
        delete_pending(pending_id)
        selection_store.pop(pending_id, None)
        return
    # attach caption to first approved item if original caption exists
    if pending['caption']:
        approved_media[0].caption = pending['caption']
    try:
        await bot.send_media_group(chat_id=int(MAIN_GROUP_ID), media=approved_media)
        await bot.send_message(chat_id=int(MAIN_GROUP_ID), text=f'📌 Album submitted by @{pending["username"]}')
    except Exception as e:
        logger.exception('failed to post approved selection: %s', e)
        await callback.message.reply('Failed to post to main group.')
        return
    delete_pending(pending_id)
    selection_store.pop(pending_id, None)
    await callback.message.reply('Approved selection posted.')

# -----------------------------
# Run the bot (polling by default)
# -----------------------------
if __name__ == '__main__':
    import uvloop
    uvloop.install()
    try:
        from aiogram import run_polling
        print('Starting polling...')
        run_polling(dp, bot)
    except Exception as e:
        logger.exception('Error starting bot: %s', e)


# -----------------------------
# Requirements (requirements.txt)
# -----------------------------
# aiogram>=3.0.0a7
# uvloop

# -----------------------------
# Procfile (for Railway)
# -----------------------------
# web: python aiogram_media_approval_bot.py

# -----------------------------
# README (short)
# -----------------------------
# 1) Set environment variables: BOT_TOKEN, MAIN_GROUP_ID, APPROVAL_GROUP_ID, ADMIN_IDS (optional)
# 2) Invite the bot to both groups and make it admin in the MAIN_GROUP (permission to delete messages) and in APPROVAL_GROUP (permission to send media and messages).
# 3) Run: python aiogram_media_approval_bot.py
# 4) For production on Railway: push repo, set environment vars on Railway, and use the Procfile above.
# 5) If you want webhook instead of polling, say so and I'll provide webhook-ready code.
