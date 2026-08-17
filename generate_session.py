import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client

print("==============================================")
print("TELEGRAM SESSION GENERATOR")
print("==============================================")
print("1. Go to https://my.telegram.org and log in.")
print("2. Go to 'API development tools'.")
print("3. Create an app (any name) to get your API_ID and API_HASH.")
print("==============================================\n")

api_id = input("Enter your API_ID: ")
api_hash = input("Enter your API_HASH: ")

async def main():
    print("\nConnecting to Telegram... (Check your Telegram app for the login code)")
    app = Client("my_account", api_id=api_id, api_hash=api_hash)
    await app.start()
    
    session_string = await app.export_session_string()
    
    print("\n" + "="*50)
    print("SUCCESS! HERE IS YOUR SESSION STRING:")
    print("="*50)
    print(session_string)
    print("="*50)
    print("KEEP THIS STRING SECRET! You will need it for the server.")
    
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
