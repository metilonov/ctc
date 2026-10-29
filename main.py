import asyncio
import logging
import os

from aiogram import Bot

from bot.ui import make_dispatcher
from config import Settings
from database.store import Store
from drive.auth import TokenVault
from drive.client import Drive
from scheduler.service import Service
from storage.workspace import Workspace
from torrent.engine import Engine


async def main():
    os.umask(0o077)
    s = Settings.load()
    if not s.token or s.owner <= 0 or not s.key:
        raise SystemExit('Configure BOT_TOKEN, OWNER_TELEGRAM_ID, TOKEN_ENCRYPTION_KEY')
    if not s.p2p_allowed or not s.quota_confirmed:
        raise SystemExit('Confirm hosting P2P permission and enforced DATA_DIR quota; see docs/BOTHOST.md')
    ws = Workspace(s)
    ws.acquire()
    db = Store(ws.root / 'state.db')
    drive = Drive(db, TokenVault(ws.root, s.key), s.folder_name)
    service = Service(db, ws, s, Engine(s), drive)
    bot = Bot(s.token)
    dp = make_dispatcher(service)
    worker = asyncio.create_task(service.run())
    polling = asyncio.create_task(dp.start_polling(bot, close_bot_session=False))
    try:
        done, _ = await asyncio.wait([worker, polling], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        service.stopping = True
        polling.cancel()
        await asyncio.gather(polling, worker, return_exceptions=True)
        await drive.http.aclose()
        await bot.session.close()
        db.db.close()
        ws.lock.close()


if __name__ == '__main__':
    # HTTP client INFO logs contain session URLs; keep dependencies quiet.
    logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(main())
