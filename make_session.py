"""Create a Telethon StringSession locally for MEOWBET v9.
Run this on your own computer, not on Render. Never share the printed session.
"""
import asyncio
import getpass
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

async def main():
    api_id = int(input("TG_API_ID: ").strip())
    api_hash = getpass.getpass("TG_API_HASH (hidden): ").strip()
    phone = input("Phone with country code (e.g. +98...): ").strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        code = getpass.getpass("Telegram login code (hidden): ").strip().replace(" ", "")
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            password = getpass.getpass("Telegram two-step verification password (hidden): ")
            await client.sign_in(password=password)
        print("\nTG_SESSION (keep secret, add only to Render Environment):\n")
        print(client.session.save())
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
