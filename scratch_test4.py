import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import inspect
from pyrogram.types import Message
print(inspect.signature(Message.copy))
