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

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from flask import Flask
from threading import Thread

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
API_ID = int(os.environ.get("API_ID", 0))  # Fix: Pyrogram requires int, not str
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("SESSION_STRING")

SOURCE_CHANNEL = int(os.environ.get("SOURCE_CHANNEL_ID", "-1003405576403"))
DESTINATION_CHANNEL = int(os.environ.get("DESTINATION_CHANNEL_ID", "-1003912457227"))

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
# Bug 2 Fix — SQLite-backed persistent message_map
# Survives Render normal restarts (disk persists; wiped only on full redeploy).
# ---------------------------------------------------------------------------
DB_PATH = "message_map.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS message_map "
        "(source_id INTEGER PRIMARY KEY, dest_id INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()

def db_get(source_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT dest_id FROM message_map WHERE source_id = ?", (source_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None

def db_set(source_id: int, dest_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO message_map (source_id, dest_id) VALUES (?, ?)",
        (source_id, dest_id)
    )
    conn.commit()
    conn.close()

def db_delete(source_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM message_map WHERE source_id = ?", (source_id,))
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
        text = text.replace(old, new)
    return text, (text != original)


def contains_filter_word(text: str) -> bool:
    """
    Bug 3 Fix — word-boundary regex matching prevents false positives.
    e.g. filter word 'ad' no longer matches 'advanced', 'road', 'download'.
    """
    if not text:
        return False
    lower_text = text.lower()
    for word in filter_words:
        pattern = r'\b' + re.escape(word.lower()) + r'\b'
        if re.search(pattern, lower_text):
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
        reply_to = db_get(first.reply_to_message_id)

    async def _do_copy():
        return await client.copy_media_group(
            chat_id=DESTINATION_CHANNEL,
            from_chat_id=first.chat.id,
            message_id=first.id,
            reply_to_message_id=reply_to
        )

    try:
        sent_list = await _do_copy()
        for src_msg, dst_msg in zip(messages, sent_list):
            db_set(src_msg.id, dst_msg.id)
        logger.info(f"Forwarded album {media_group_id} ({len(messages)} items)")
    except FloodWait as e:
        logger.warning(f"FloodWait {e.value}s on album {media_group_id}, retrying...")
        await asyncio.sleep(e.value)
        try:
            sent_list = await _do_copy()
            for src_msg, dst_msg in zip(messages, sent_list):
                db_set(src_msg.id, dst_msg.id)
            logger.info(f"Forwarded album {media_group_id} after FloodWait")
        except Exception as e2:
            logger.error(f"Failed to forward album {media_group_id} after retry: {e2}")
    except Exception as e:
        logger.error(f"Failed to forward album {media_group_id}: {e}")


# ---------------------------------------------------------------------------
# Pyrogram client
# ---------------------------------------------------------------------------
app = Client(
    "my_account",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)


# ---------------------------------------------------------------------------
# Helper — forward a single (non-album) message to the destination
# ---------------------------------------------------------------------------
async def _forward_single(client: Client, message: Message, reply_to=None):
    """
    Apply replacements and forward one message to DESTINATION_CHANNEL.
    Returns the sent Message object, or None if message type is unsupported.
    """
    text_to_check = message.caption if message.media else message.text
    new_text, was_replaced = apply_replacements(text_to_check)

    if message.media:
        # Bug 4 Fix: only pass caption= kwarg when there is an actual caption.
        # Passing caption=None would explicitly wipe the original embedded caption.
        kwargs = {"reply_to_message_id": reply_to}
        if new_text is not None:
            kwargs["caption"] = new_text
            # Bug 5 Fix: preserve original caption entities when no replacement was made.
            # Entity byte offsets become wrong after text length changes from replacement.
            if not was_replaced and message.caption_entities:
                kwargs["caption_entities"] = message.caption_entities
        return await message.copy(DESTINATION_CHANNEL, **kwargs)

    elif message.text:
        kwargs = {"reply_to_message_id": reply_to}
        # Bug 5 Fix: preserve original text entities when no replacement was made.
        if not was_replaced and message.entities:
            kwargs["entities"] = message.entities
        return await client.send_message(
            DESTINATION_CHANNEL,
            text=new_text,
            **kwargs
        )

    # Unsupported type (poll, venue, location, etc.) — log and skip
    logger.info(f"Skipping unsupported message type for message {message.id}")
    return None


# ---------------------------------------------------------------------------
# Handler — new messages
# ---------------------------------------------------------------------------
@app.on_message(filters.chat(SOURCE_CHANNEL))
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
        reply_to = db_get(message.reply_to_message_id)

    async def _send():
        return await _forward_single(client, message, reply_to=reply_to)

    try:
        sent_msg = await _send()
        if sent_msg:
            db_set(message.id, sent_msg.id)
            logger.info(f"Forwarded message {message.id} -> {sent_msg.id}")
    except FloodWait as e:
        # Bug 1 Fix: retry once after the mandatory wait instead of dropping the message
        logger.warning(f"FloodWait {e.value}s on message {message.id}, retrying...")
        await asyncio.sleep(e.value)
        try:
            sent_msg = await _send()
            if sent_msg:
                db_set(message.id, sent_msg.id)
                logger.info(f"Forwarded message {message.id} -> {sent_msg.id} (after FloodWait)")
        except Exception as e2:
            logger.error(f"Failed to forward message {message.id} after FloodWait retry: {e2}")
    except Exception as e:
        logger.error(f"Failed to forward message {message.id}: {e}")


# ---------------------------------------------------------------------------
# Handler — edited messages
# ---------------------------------------------------------------------------
@app.on_edited_message(filters.chat(SOURCE_CHANNEL))
async def handle_edited_message(client: Client, message: Message):
    text_to_check = message.caption if message.media else message.text
    dest_id = db_get(message.id)

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
                reply_to = db_get(message.reply_to_message_id)
            try:
                sent_msg = await _forward_single(client, message, reply_to=reply_to)
                if sent_msg:
                    db_set(message.id, sent_msg.id)
                    logger.info(f"Late-forwarded {message.id} -> {sent_msg.id}")
            except FloodWait as e:
                logger.warning(f"FloodWait {e.value}s on late-forward {message.id}, retrying...")
                await asyncio.sleep(e.value)
                try:
                    sent_msg = await _forward_single(client, message, reply_to=reply_to)
                    if sent_msg:
                        db_set(message.id, sent_msg.id)
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
            db_delete(message.id)
            logger.info(
                f"Deleted destination message {dest_id} "
                f"(source {message.id} edited to contain filter word)."
            )
        except Exception as e:
            logger.error(f"Failed to delete destination message {dest_id}: {e}")
        return

    # Normal edit — apply replacements and update destination
    new_text, _ = apply_replacements(text_to_check)

    async def _do_edit():
        if message.media:
            # Bug 9 Fix: if caption was removed (new_text is None), skip the edit.
            # Passing caption=None to edit_message_caption raises an API error.
            if new_text is None:
                logger.info(
                    f"Source media {message.id} caption removed — skipping destination edit."
                )
                return
            await client.edit_message_caption(
                chat_id=DESTINATION_CHANNEL,
                message_id=dest_id,
                caption=new_text
            )
        elif message.text:
            await client.edit_message_text(
                chat_id=DESTINATION_CHANNEL,
                message_id=dest_id,
                text=new_text
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


if __name__ == "__main__":
    if not API_ID or not API_HASH or not SESSION_STRING:
        logger.error("Missing API_ID, API_HASH, or SESSION_STRING! Cannot start bot.")
    else:
        logger.info("Starting Web Server...")
        Thread(target=run_web, daemon=True).start()

        logger.info("Starting Telegram User Bot...")
        app.run()
