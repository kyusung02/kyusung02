"""
공유 클라이언트 — Telethon 클라이언트 & ThreadPoolExecutor
"""
from concurrent.futures import ThreadPoolExecutor
from telethon import TelegramClient
from telethon.sessions import StringSession
from config import (
    TELEGRAM_API_ID, TELEGRAM_API_HASH,
    TELEGRAM_USER_SESSION, TELEGRAM_BOT_SESSION,
)

_executor = ThreadPoolExecutor(max_workers=4)

_user_session = StringSession(TELEGRAM_USER_SESSION) if TELEGRAM_USER_SESSION else "user_session"
_bot_session  = StringSession(TELEGRAM_BOT_SESSION)  if TELEGRAM_BOT_SESSION  else "bot_session"

user_client = TelegramClient(_user_session, TELEGRAM_API_ID, TELEGRAM_API_HASH)
bot_client  = TelegramClient(_bot_session,  TELEGRAM_API_ID, TELEGRAM_API_HASH)
