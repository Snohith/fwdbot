import os
import json
import logging
import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters
from pyrogram.types import Message
from flask import Flask
from threading import Thread

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
API_ID = os.environ.get("API_ID")
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

def apply_replacements(text):
    if not text:
        return text
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text

def contains_filter_word(text):
    if not text:
        return False
    lower_text = text.lower()
    for word in filter_words:
        if word.lower() in lower_text:
            return True
    return False

# In-memory mapping of Source ID -> Destination ID to support edits/replies
message_map = {}

app = Client(
    "my_account",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

@app.on_message(filters.chat(SOURCE_CHANNEL))
async def handle_new_message(client: Client, message: Message):
    text_to_check = message.caption if message.media else message.text
    if contains_filter_word(text_to_check):
        logger.info(f"Ignored message {message.id} due to filter.")
        return

    # Check if replying to an existing message
    reply_to = None
    if message.reply_to_message_id and message.reply_to_message_id in message_map:
        reply_to = message_map[message.reply_to_message_id]

    try:
        new_text = apply_replacements(text_to_check)
        sent_msg = None

        if message.media:
            # Copy media and override caption
            sent_msg = await message.copy(
                DESTINATION_CHANNEL,
                caption=new_text,
                reply_to_message_id=reply_to
            )
        elif message.text:
            # Send new text message
            sent_msg = await client.send_message(
                DESTINATION_CHANNEL,
                text=new_text,
                reply_to_message_id=reply_to
            )

        if sent_msg:
            message_map[message.id] = sent_msg.id
            logger.info(f"Forwarded message {message.id} -> {sent_msg.id}")
            
    except Exception as e:
        logger.error(f"Failed to forward message {message.id}: {e}")

@app.on_edited_message(filters.chat(SOURCE_CHANNEL))
async def handle_edited_message(client: Client, message: Message):
    if message.id not in message_map:
        return

    text_to_check = message.caption if message.media else message.text
    if contains_filter_word(text_to_check):
        return

    dest_id = message_map[message.id]
    new_text = apply_replacements(text_to_check)

    try:
        if message.media:
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
        logger.info(f"Edited message {dest_id}")
    except Exception as e:
        logger.error(f"Failed to edit message {message.id}: {e}")

# Dummy Web Server (So Render doesn't shut down the service)
flask_app = Flask(__name__)
@flask_app.route('/')
def health_check():
    return "User Bot is running securely!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    if not API_ID or not API_HASH or not SESSION_STRING:
        logger.error("Missing API_ID, API_HASH, or SESSION_STRING! Cannot start bot.")
    else:
        logger.info("Starting Web Server...")
        Thread(target=run_web, daemon=True).start()
        
        logger.info("Starting Telegram User Bot...")
        app.run()
