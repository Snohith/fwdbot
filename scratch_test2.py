import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram.types import Message, MessageEntity
from pyrogram.enums import MessageEntityType
from pyrogram.parser import html
from pyrogram import Client

async def main():
    m = Message(
        id=1,
        text="Registration link on the 1XBET website here",
        entities=[
            MessageEntity(
                type=MessageEntityType.TEXT_LINK,
                offset=0,
                length=43,
                url="https://telshort.com/CbwWjM"
            )
        ]
    )
    
    app = Client("test", api_id=1, api_hash="1")
    parser = html.HTML(app)
    print(parser.unparse(m.text, m.entities))

if __name__ == "__main__":
    asyncio.run(main())
