import os
import json
import re
import logging
import asyncio
import sqlite3

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters, utils as pyrogram_utils
from pyrogram.types import Message
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.enums import ParseMode
from pyrogram.parser import html
from flask import Flask
from threading import Thread

# ---------------------------------------------------------------------------
# Critical Bug Fix — 64-bit Telegram Channel IDs in Pyrogram 2.0.x
# Pyrogram hardcodes MIN_CHANNEL_ID = -1002147483647 which causes
# ValueError: Peer id invalid for modern channels (like -1003405576403).
# ---------------------------------------------------------------------------
pyrogram_utils.MIN_CHANNEL_ID = -1009999999999999
pyrogram_utils.MAX_CHANNEL_ID = -1000000000000
pyrogram_utils.MIN_CHAT_ID = -2147483647
pyrogram_utils.MAX_USER_ID = 999999999999999

def _patched_get_peer_type(peer_id: int) -> str:
    if peer_id < 0:
        if peer_id <= -1000000000000:
            return "channel"
        return "chat"
    elif peer_id > 0:
        return "user"
    raise ValueError(f"Peer id invalid: {peer_id}")

def _patched_get_channel_id(peer_id: int) -> int:
    return -1000000000000 - peer_id

pyrogram_utils.get_peer_type = _patched_get_peer_type
pyrogram_utils.get_channel_id = _patched_get_channel_id

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
API_ID = int(os.environ.get("API_ID", 0))  # Fix: Pyrogram requires int, not str
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("SESSION_STRING")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Parse all source channels from both possible env var names
raw_sources = f"{os.environ.get('SOURCE_CHANNEL_IDS', '')},{os.environ.get('SOURCE_CHANNEL_ID', '')}"
if not raw_sources.replace(',', '').strip():
    raw_sources = "-1003405576403"
SOURCE_CHANNELS = list(set(int(x.strip()) for x in raw_sources.split(",") if x.strip()))
logger.info(f"Configured SOURCE_CHANNELS: {SOURCE_CHANNELS}")
DESTINATION_CHANNEL = int(os.environ.get("DESTINATION_CHANNEL_ID", "-1003912457227"))
logger.info(f"Configured DESTINATION_CHANNEL: {DESTINATION_CHANNEL}")

# Load Replacements
replacements = {
    "1Xbet": "Linebet",
    "https://telshort.com/CbwWjM": "https://shorturl.at/3ho5b",
    "https://telshort.com/GCVv5Q": "https://shorturl.at/B1NzY",
    "ZKP5": "GET1000RS"
}
if "REPLACEMENTS_JSON" in os.environ:
    try:
        replacements = json.loads(os.environ["REPLACEMENTS_JSON"])
    except Exception as e:
        logger.error(f"Error parsing REPLACEMENTS_JSON: {e}")

# Load Filters
filter_words = []
if "FILTER_WORDS_JSON" in os.environ:
    try:
        filter_words = json.loads(os.environ["FILTER_WORDS_JSON"])
    except Exception as e:
        logger.error(f"Error parsing FILTER_WORDS_JSON: {e}")

# ---------------------------------------------------------------------------
# Bug 2 Fix — SQLite-backed persistent message_map_v2
# Survives Render normal restarts (disk persists; wiped only on full redeploy).
# ---------------------------------------------------------------------------
DB_PATH = "message_map_v2.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS message_map_v2 "
        "(source_chat_id INTEGER, source_id INTEGER, dest_id INTEGER NOT NULL, "
        "PRIMARY KEY (source_chat_id, source_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_api_keys "
        "(user_id INTEGER PRIMARY KEY, api_key TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

def db_get(source_chat_id: int, source_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT dest_id FROM message_map_v2 WHERE source_chat_id = ? AND source_id = ?", 
        (source_chat_id, source_id)
    ).fetchone()
    conn.close()
    return row[0] if row else None

def db_set(source_chat_id: int, source_id: int, dest_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO message_map_v2 (source_chat_id, source_id, dest_id) VALUES (?, ?, ?)",
        (source_chat_id, source_id, dest_id)
    )
    conn.commit()
    conn.close()

def db_delete(source_chat_id: int, source_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM message_map_v2 WHERE source_chat_id = ? AND source_id = ?", (source_chat_id, source_id))
    conn.commit()
    conn.close()

def db_save_api_key(user_id: int, api_key: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO user_api_keys (user_id, api_key) VALUES (?, ?)", (user_id, api_key))
    conn.commit()
    conn.close()

def db_get_api_key(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT api_key FROM user_api_keys WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row else None

def db_delete_api_key(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM user_api_keys WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

init_db()

# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def apply_replacements(text: str):
    """Returns (modified_text, was_replaced: bool)."""
    if not text:
        return text, False
    original = text
    for old, new in replacements.items():
        pattern = re.compile(re.escape(old), re.IGNORECASE)
        # re.sub processes \ as escapes, so we need to escape the replacement string just in case
        new_escaped = new.replace('\\', r'\\')
        text = pattern.sub(new_escaped, text)
    return text, (text != original)


def contains_filter_word(text):
    if not text:
        return False
    for word in filter_words:
        pattern = r'\b' + re.escape(word.lower()) + r'\b'
        if re.search(pattern, text.lower()):
            return True
    return False

# ---------------------------------------------------------------------------
# Bug 6 Fix — Album debounce tracker
# Telegram sends photo/video albums as individual messages sharing a
# media_group_id. Buffer them for 1.5s then forward as a group atomically.
# ---------------------------------------------------------------------------
_album_buffer: dict = {}
_album_tasks: dict = {}


async def _flush_album(client: Client, media_group_id: str):
    """Wait briefly then forward the buffered album as a grouped media."""
    await asyncio.sleep(1.5)
    messages = _album_buffer.pop(media_group_id, [])
    _album_tasks.pop(media_group_id, None)
    if not messages:
        return

    messages.sort(key=lambda m: m.id)
    first = messages[0]

    reply_to = None
    if first.reply_to_message_id:
        reply_to = db_get(first.chat.id, first.reply_to_message_id)

    async def _do_copy():
        from pyrogram.types import InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument
        
        media_group = []
        for msg in messages:
            kwargs = {}
            if msg == first:
                # Apply replacements to the caption of the first item
                text = msg.caption if msg.media else msg.text
                entities = msg.caption_entities if msg.media else msg.entities
                html_text = text
                if text and entities:
                    html_text = html.HTML(client).unparse(text, entities)
                new_html, was_replaced = apply_replacements(html_text)
                
                # Strip custom emojis to prevent Premium-required API errors
                if new_html:
                    new_html = re.sub(r'<emoji id="[^"]+">(.*?)</emoji>', r'\1', new_html)
                
                if new_html is not None:
                    kwargs["caption"] = new_html
                    if was_replaced or entities:
                        kwargs["parse_mode"] = ParseMode.HTML
                    elif not was_replaced and msg.caption_entities:
                        kwargs["caption_entities"] = msg.caption_entities
                        
            if msg.photo:
                media_group.append(InputMediaPhoto(msg.photo.file_id, **kwargs))
            elif msg.video:
                media_group.append(InputMediaVideo(msg.video.file_id, **kwargs))
            elif msg.audio:
                media_group.append(InputMediaAudio(msg.audio.file_id, **kwargs))
            elif msg.document:
                media_group.append(InputMediaDocument(msg.document.file_id, **kwargs))

        if not media_group:
            return []

        return await client.send_media_group(
            chat_id=DESTINATION_CHANNEL,
            media=media_group,
            reply_to_message_id=reply_to
        )

    try:
        sent_list = await _do_copy()
        for src_msg, dst_msg in zip(messages, sent_list):
            db_set(src_msg.chat.id, src_msg.id, dst_msg.id)
        logger.info(f"Forwarded album {media_group_id} ({len(messages)} items)")
    except FloodWait as e:
        logger.warning(f"FloodWait {e.value}s on album {media_group_id}, retrying...")
        await asyncio.sleep(e.value)
        try:
            sent_list = await _do_copy()
            for src_msg, dst_msg in zip(messages, sent_list):
                db_set(src_msg.chat.id, src_msg.id, dst_msg.id)
            logger.info(f"Forwarded album {media_group_id} after FloodWait")
        except Exception as e2:
            logger.error(f"Failed to forward album {media_group_id} after retry: {e2}")
    except Exception as e:
        logger.error(f"Failed to forward album {media_group_id}: {e}")


# ---------------------------------------------------------------------------
# Pyrogram clients
# ---------------------------------------------------------------------------
app = Client(
    "my_account",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

smm_bot = None
if BOT_TOKEN:
    smm_bot = Client(
        "smm_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN
    )

    import smm_api

    # ---------------------------------------------------------------------------
    # SMM Panel Commands (Public Bot Token)
    # ---------------------------------------------------------------------------
    @smm_bot.on_message(filters.command(["login"]))
    async def handle_login(client: Client, message: Message):
        cmd = message.command
        if len(cmd) < 2:
            await message.reply("Usage: `/login <your_api_key>`")
            return
        db_save_api_key(message.from_user.id, cmd[1])
        await message.reply("✅ Successfully logged in! You can now use `/balance` or place orders.")

    @smm_bot.on_message(filters.command(["logout"]))
    async def handle_logout(client: Client, message: Message):
        db_delete_api_key(message.from_user.id)
        await message.reply("✅ Successfully logged out.")

    @smm_bot.on_message(filters.command(["balance", "order", "status"]))
    async def smm_commands(client: Client, message: Message):
        user_key = db_get_api_key(message.from_user.id)
        if not user_key:
            await message.reply("❌ Please `/login <your_api_key>` first.")
            return
            
        cmd = message.command
        if cmd[0] == "balance":
            resp = await smm_api.get_balance(user_key)
            if "balance" in resp:
                await message.reply(f"💰 **Balance:** {resp['balance']} {resp.get('currency', 'USD')}")
            else:
                await message.reply(f"❌ Error: {resp.get('error', 'Unknown')}")
                
        elif cmd[0] == "order":
            if len(cmd) < 4:
                await message.reply("Usage: `/order <service_id> <link> <quantity>`")
                return
            resp = await smm_api.place_order(user_key, cmd[1], cmd[2], cmd[3])
            if "order" in resp:
                await message.reply(f"✅ **Order Placed!**\nOrder ID: `{resp['order']}`")
            else:
                await message.reply(f"❌ Error: {resp.get('error', 'Unknown')}")
                
        elif cmd[0] == "status":
            if len(cmd) < 2:
                await message.reply("Usage: `/status <order_id>`")
                return
            resp = await smm_api.get_status(user_key, cmd[1])
            if "status" in resp:
                msg = f"📊 **Order Status:** {resp['status']}\n"
                msg += f"Charge: {resp.get('charge', '0')}\n"
                msg += f"Remains: {resp.get('remains', '0')}"
                await message.reply(msg)
            else:
                await message.reply(f"❌ Error: {resp.get('error', 'Unknown')}")

# ---------------------------------------------------------------------------
# Debug Logger — trace all incoming messages to find the exact Chat ID
# ---------------------------------------------------------------------------
@app.on_message(filters.all, group=-1)
async def log_all_incoming(client: Client, message: Message):
    chat_id = message.chat.id if message.chat else "Unknown"
    logger.info(f"DEBUG: Received message from chat {chat_id}")

# ---------------------------------------------------------------------------
# Helper — forward a single (non-album) message to the destination
# ---------------------------------------------------------------------------
async def _forward_single(client: Client, message: Message, reply_to=None):
    """
    Apply replacements and forward one message to DESTINATION_CHANNEL.
    Returns the sent Message object, or None if message type is unsupported.
    """
    text = message.caption if message.media else message.text
    entities = message.caption_entities if message.media else message.entities

    html_text = text
    if text and entities:
        html_text = html.HTML(client).unparse(text, entities)
        
    new_html, was_replaced = apply_replacements(html_text)

    # Strip custom emojis to prevent Premium-required API errors
    if new_html:
        new_html = re.sub(r'<emoji id="[^"]+">(.*?)</emoji>', r'\1', new_html)

    if message.media:
        kwargs = {"reply_to_message_id": reply_to}
        if new_html is not None:
            kwargs["caption"] = new_html
            if was_replaced or entities:
                kwargs["parse_mode"] = ParseMode.HTML
            elif not was_replaced and message.caption_entities:
                kwargs["caption_entities"] = message.caption_entities
        return await message.copy(DESTINATION_CHANNEL, **kwargs)

    elif message.text:
        kwargs = {"reply_to_message_id": reply_to}
        if was_replaced or entities:
            kwargs["parse_mode"] = ParseMode.HTML
        elif not was_replaced and message.entities:
            kwargs["entities"] = message.entities
        return await client.send_message(
            DESTINATION_CHANNEL,
            text=new_html,
            **kwargs
        )

    # Unsupported type (poll, venue, location, etc.) — log and skip
    logger.info(f"Skipping unsupported message type for message {message.id}")
    return None

# ---------------------------------------------------------------------------
# Handler — new messages
# ---------------------------------------------------------------------------
@app.on_message(filters.chat(SOURCE_CHANNELS))
async def handle_new_message(client: Client, message: Message):
    text_to_check = message.caption if message.media else message.text
    if contains_filter_word(text_to_check):
        logger.info(f"Ignored message {message.id} due to filter.")
        return

    # Bug 6 Fix: album grouping — buffer parts and debounce
    if message.media_group_id:
        gid = str(message.media_group_id)
        _album_buffer.setdefault(gid, []).append(message)
        if gid in _album_tasks and not _album_tasks[gid].done():
            _album_tasks[gid].cancel()
        _album_tasks[gid] = asyncio.create_task(_flush_album(client, gid))
        return

    # Resolve reply target from persistent SQLite map
    reply_to = None
    if message.reply_to_message_id:
        reply_to = db_get(message.chat.id, message.reply_to_message_id)

    async def _send():
        return await _forward_single(client, message, reply_to=reply_to)

    try:
        sent_msg = await _send()
        if sent_msg:
            db_set(message.chat.id, message.id, sent_msg.id)
            logger.info(f"Forwarded message {message.id} -> {sent_msg.id}")
    except FloodWait as e:
        # Bug 1 Fix: retry once after the mandatory wait instead of dropping the message
        logger.warning(f"FloodWait {e.value}s on message {message.id}, retrying...")
        await asyncio.sleep(e.value)
        try:
            sent_msg = await _send()
            if sent_msg:
                db_set(message.chat.id, message.id, sent_msg.id)
                logger.info(f"Forwarded message {message.id} -> {sent_msg.id} (after FloodWait)")
        except Exception as e2:
            logger.error(f"Failed to forward message {message.id} after FloodWait retry: {e2}")
    except Exception as e:
        logger.error(f"Failed to forward message {message.id}: {e}")


# ---------------------------------------------------------------------------
# Handler — edited messages
# ---------------------------------------------------------------------------
@app.on_edited_message(filters.chat(SOURCE_CHANNELS))
async def handle_edited_message(client: Client, message: Message):
    text_to_check = message.caption if message.media else message.text
    dest_id = db_get(message.chat.id, message.id)

    # -----------------------------------------------------------------------
    # Bug 8 Fix: message was originally filtered (no dest_id),
    # but the edit removed the blocked word — forward it now as a new message.
    # -----------------------------------------------------------------------
    if dest_id is None:
        if text_to_check and not contains_filter_word(text_to_check):
            logger.info(
                f"Previously filtered message {message.id} is now clean — late-forwarding."
            )
            reply_to = None
            if message.reply_to_message_id:
                reply_to = db_get(message.chat.id, message.reply_to_message_id)
            try:
                sent_msg = await _forward_single(client, message, reply_to=reply_to)
                if sent_msg:
                    db_set(message.chat.id, message.id, sent_msg.id)
                    logger.info(f"Late-forwarded {message.id} -> {sent_msg.id}")
            except FloodWait as e:
                logger.warning(f"FloodWait {e.value}s on late-forward {message.id}, retrying...")
                await asyncio.sleep(e.value)
                try:
                    sent_msg = await _forward_single(client, message, reply_to=reply_to)
                    if sent_msg:
                        db_set(message.chat.id, message.id, sent_msg.id)
                        logger.info(f"Late-forwarded {message.id} -> {sent_msg.id} (after FloodWait)")
                except Exception as e2:
                    logger.error(f"Failed late-forward for {message.id} after retry: {e2}")
            except Exception as e:
                logger.error(f"Failed late-forward for message {message.id}: {e}")
        return

    # -----------------------------------------------------------------------
    # Bug 7 Fix: previously clean message edited to contain a filter word
    # → delete from destination so blocked content doesn't persist.
    # -----------------------------------------------------------------------
    if contains_filter_word(text_to_check):
        try:
            await client.delete_messages(DESTINATION_CHANNEL, dest_id)
            db_delete(message.chat.id, message.id)
            logger.info(
                f"Deleted destination message {dest_id} "
                f"(source {message.id} edited to contain filter word)."
            )
        except Exception as e:
            logger.error(f"Failed to delete destination message {dest_id}: {e}")
        return

    text = message.caption if message.media else message.text
    entities = message.caption_entities if message.media else message.entities
    
    html_text = text
    if text and entities:
        html_text = html.HTML(client).unparse(text, entities)
        
    new_html, was_replaced = apply_replacements(html_text)

    # Strip custom emojis to prevent Premium-required API errors
    if new_html:
        new_html = re.sub(r'<emoji id="[^"]+">(.*?)</emoji>', r'\1', new_html)

    async def _do_edit():
        if message.media:
            if new_html is None:
                logger.info(
                    f"Source media {message.id} caption removed — skipping destination edit."
                )
                return
            await client.edit_message_caption(
                chat_id=DESTINATION_CHANNEL,
                message_id=dest_id,
                caption=new_html,
                parse_mode=ParseMode.HTML if (was_replaced or entities) else None
            )
        elif message.text:
            await client.edit_message_text(
                chat_id=DESTINATION_CHANNEL,
                message_id=dest_id,
                text=new_html,
                parse_mode=ParseMode.HTML if (was_replaced or entities) else None
            )
        logger.info(f"Edited destination message {dest_id} (source {message.id})")

    try:
        await _do_edit()
    except FloodWait as e:
        # Bug 1 Fix: retry edit after FloodWait
        logger.warning(f"FloodWait {e.value}s on edit for dest {dest_id}, retrying...")
        await asyncio.sleep(e.value)
        try:
            await _do_edit()
        except Exception as e2:
            logger.error(f"Failed to edit destination message {dest_id} after retry: {e2}")
    except MessageNotModified:
        logger.info(f"Message {message.id} not modified in destination (no changes).")
    except Exception as e:
        logger.error(f"Failed to edit message {message.id}: {e}")


# ---------------------------------------------------------------------------
# Dummy Web Server — keeps Render alive (UptimeRobot pings this)
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "User Bot is running securely!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False, debug=False)


from pyrogram import idle

async def run_bot():
    await app.start()
    if smm_bot:
        await smm_bot.start()
    logger.info("Caching peers to fix 'Peer id invalid' errors...")
    try:
        # Iterating through dialogs forces Pyrogram to cache all channel IDs from Telegram
        async for _ in app.get_dialogs():
            pass
    except Exception as e:
        logger.error(f"Failed to cache peers: {e}")
    
    logger.info("Bot is fully ready and listening for messages!")
    await idle()
    await app.stop()
    if smm_bot:
        await smm_bot.stop()

if __name__ == "__main__":
    if not API_ID or not API_HASH or not SESSION_STRING:
        logger.error("Missing API_ID, API_HASH, or SESSION_STRING! Cannot start bot.")
    else:
        logger.info("Starting Web Server...")
        Thread(target=run_web, daemon=True).start()

        logger.info("Starting Telegram Bots...")
        app.run(run_bot())
