import os
import base64
from telethon import TelegramClient, events

# -------------------------------
# Telegram API info
# -------------------------------
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_FILE = "ux_solsniper.session"

# -------------------------------
# Load session from Render secret
# -------------------------------
SESSION_B64 = os.getenv("TG_SESSION_BASE64")
if SESSION_B64:
    # Decode and write the session file if it doesn't exist
    if not os.path.exists(SESSION_FILE):
        with open(SESSION_FILE, "wb") as f:
            f.write(base64.b64decode(SESSION_B64))
        print("Telegram session created from secret.")

# -------------------------------
# Create Telethon client
# -------------------------------
client = TelegramClient(SESSION_FILE, API_ID, API_HASH)

async def main():
    await client.start()
    me = await client.get_me()
    print(f"Logged in as: {me.username} ({me.id})")

    # Example: listen to new messages from a specific channel
    @client.on(events.NewMessage(chats=['@YourTargetChannel']))
    async def handler(event):
        print("New message:", event.message.text)
        # You can trigger your sniper logic here

    # Keep running
    print("Bot is now running...")
    await client.run_until_disconnected()

# -------------------------------
# Run async client
# -------------------------------
if __name__ == "__main__":
    import asyncio
    asyncio.run(main())