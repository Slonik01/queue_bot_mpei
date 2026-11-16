"""Launch bot polling and durable deadline scanner."""
import asyncio
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from queuebot.config import load_config, utc_now, utc_iso
from queuebot.db import Database
from queuebot.service import QueueService, DomainError
from queuebot import handlers

log=logging.getLogger(__name__)


async def deadline_worker(bot,db,svc,cfg):
    """Deadlines live in SQLite, not transient in-memory timers."""
    while True:
        try:
            now=utc_now()
            pending = db.all("""SELECT id FROM queues WHERE group_message_id IS NOT NULL
                AND group_synced_revision < revision AND (group_retry_at IS NULL OR group_retry_at<=?)
                ORDER BY group_retry_at, id LIMIT 40""", (utc_iso(now),))
            for r in pending:
                await handlers.update_group(bot, r['id'])
            reg=db.all("SELECT id FROM queues WHERE status='registration' AND closes_at<=? ORDER BY closes_at LIMIT 10",
                       (utc_iso(now),))
            for r in reg:
                qid=r['id']
                try:
                    await handlers.draw_and_notify(bot,qid)
                    log.info('Queue #%s drawn',qid)
                except (DomainError,RuntimeError) as exc:
                    log.exception('Draw failed for queue %s: %s',qid,exc)
            active=db.all("SELECT id,lesson_at FROM queues WHERE status='active' ORDER BY lesson_at LIMIT 40")
            for r in active:
                deadline=datetime.fromisoformat(r['lesson_at'])+timedelta(minutes=cfg.lesson_minutes)
                if deadline<=now:
                    try:
                        await handlers.finish_and_notify(bot,r['id'])
                        log.info('Lesson #%s finished',r['id'])
                    except DomainError as exc:
                        log.warning('Could not finish %s: %s',r['id'],exc)
            # Retry cleanup after failures, crashes or restart. The public
            # archive stays intact and is never deleted.
            remaining=db.all("""SELECT DISTINCT q.id FROM queues q
                JOIN personal_panels p ON p.queue_id=q.id WHERE q.status IN ('closed','cancelled')
                ORDER BY q.id""")
            for r in remaining:
                await handlers.cleanup_closed_panels(bot, r['id'])
            expired = db.all("SELECT queue_id,from_user,to_user FROM swaps WHERE status='pending' AND expires_at<=?",
                             (utc_iso(now),))
            if expired:
                db.exec("UPDATE swaps SET status='expired',handled_at=? WHERE status='pending' AND expires_at<=?",
                        (utc_iso(now),utc_iso(now)))
                for swap in expired:
                    await handlers.refresh_panel(bot,swap['queue_id'],swap['from_user'],view='main')
                    await handlers.refresh_panel(bot,swap['queue_id'],swap['to_user'],view='main')
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Deadline worker encountered error; retrying')
        await asyncio.sleep(10)


async def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    cfg=load_config()
    db=Database(cfg.database)
    svc=QueueService(db,cfg)
    async with Bot(cfg.token,default=DefaultBotProperties(parse_mode='HTML')) as bot:
        me=await bot.get_me()
        if not me.username:
            raise RuntimeError('Bot has no username')
        handlers.setup(db,cfg,svc,me.username)
        await handlers.register_commands(bot)
        dp=Dispatcher()
        dp.include_router(handlers.router)
        await bot.delete_webhook(drop_pending_updates=False)
        task=asyncio.create_task(deadline_worker(bot,db,svc,cfg))
        try:
            log.info('Running @%s',me.username)
            await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


if __name__=='__main__':
    asyncio.run(main())
