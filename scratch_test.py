import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram.types import Message, MessageEntity
from pyrogram.enums import MessageEntityType
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
    print(dir(m))
    print(getattr(m.text, "html", "No html on text"))
    print(getattr(m.text, "markdown", "No markdown on text"))
    print(m)

if __name__ == "__main__":
    asyncio.run(main())
