import inspect
from pyrogram.types import Message
print(inspect.signature(Message.copy))
