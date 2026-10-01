import asyncio
import logging

from aiogram import Bot, Dispatcher
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .agents.delegate import build_delegate
from .bot import BOT_COMMANDS, TelegramUI
from .browser import Browser
from .config import get_settings
from .db import DB
from .pipeline import scout_und_bewerten
from .services import Services, ordercity_session

log = logging.getLogger("ausschreibungsbot")


async def scheduler(services: Services) -> None:
    """Sucht in festem Abstand nach neuen Ausschreibungen."""
    await asyncio.sleep(10)
    while True:
        try:
            await scout_und_bewerten(services)
        except Exception:
            log.exception("Geplanter Scout-Lauf fehlgeschlagen")
        await asyncio.sleep(services.settings.poll_interval_minutes * 60)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # Warnungen zu CAD-Plänen u.ä.

    s = get_settings()
    db = DB(s.data_dir / "bot.sqlite")
    await db.connect()
    browser = Browser(s)
    services = Services(settings=s, db=db, browser=browser, ordercity=ordercity_session(s))

    async with AsyncSqliteSaver.from_conn_string(str(s.data_dir / "checkpoints.sqlite")) as checkpointer:
        delegate = build_delegate(services, checkpointer)
        bot = Bot(s.telegram_bot_token)
        ui = TelegramUI(services, delegate, bot)
        await ui.init_users()
        services.notify_tenders = ui.notify_tenders

        dp = Dispatcher()
        dp.include_router(ui.router)
        await bot.set_my_commands(BOT_COMMANDS)

        task = asyncio.create_task(scheduler(services))
        log.info("Decocity Ausschreibungsbot läuft.")
        try:
            await dp.start_polling(bot)
        finally:
            task.cancel()
            await browser.close()
            if services.ordercity:
                await services.ordercity.close()
            await bot.session.close()
            await db.close()
