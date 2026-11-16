"""End-of-lesson behavior: 100 min, no polls, archive persists, DM cards disappear."""
import asyncio
from datetime import timedelta

from queuebot.config import utc_iso, utc_now, load_config
from queuebot.db import Database
from queuebot.service import QueueService
from test_handlers_v21 import FakeBot, Settings, handler_module


def make_active(tmp_path):
    cfg=Settings()
    db=Database(str(tmp_path / 'end.sqlite3'))
    svc=QueueService(db,cfg)
    for uid in (101,202,303):
        db.exec('INSERT INTO users(id,name,created_at) VALUES(?,?,?)',
                (uid, str(uid), utc_iso(utc_now())))
    qid=svc.create(-10042,101,'Прога',utc_now()+timedelta(days=1),.05)
    for uid in (101,202,303): svc.signup(qid,uid)
    db.exec('UPDATE queues SET closes_at=? WHERE id=?',
            (utc_iso(utc_now()-timedelta(seconds=3)),qid))
    svc.draw(qid)
    for uid, mid in ((101,701),(202,702),(303,703)):
        db.exec('INSERT INTO personal_panels(queue_id,user_id,message_id) VALUES(?,?,?)',(qid,uid,mid))
    db.exec('UPDATE queues SET group_message_id=999 WHERE id=?',(qid,))
    return cfg,db,svc,qid


def test_closed_queue_cleans_cards_of_withdrawn_members_too(tmp_path,handler_module):
    cfg,db,svc,qid=make_active(tmp_path)
    svc.withdraw(qid,202)
    handler_module.setup(db,cfg,svc,'test_bot')
    bot=FakeBot()
    asyncio.run(handler_module.finish_and_notify(bot,qid))
    assert db.queue(qid)['status']=='closed'
    assert not db.all('SELECT * FROM personal_panels WHERE queue_id=?',(qid,))
    assert {c[1] for c in bot.calls if c[0]=='delete'} == {101,202,303}
    public=[c for c in bot.calls if c[0]=='edit' and c[1]==-10042]
    assert len(public)==1 and 'Завершена' in public[0][3]
    assert not any(c[0]=='delete' and c[1]==-10042 for c in bot.calls)
    # All subsequent /my or callback attempts must never create a new panel.
    count=len(bot.calls)
    asyncio.run(handler_module.refresh_panel(bot,qid,101))
    assert count==len(bot.calls)


def test_transient_network_failure_retries_cleanup(tmp_path,handler_module):
    cfg,db,svc,qid=make_active(tmp_path)
    handler_module.setup(db,cfg,svc,'test_bot')
    svc.finish(qid)
    class UnreliableBot(FakeBot):
        def __init__(self):
            super().__init__()
            self.fail=True
        async def delete_message(self, **kw):
            if self.fail and kw['chat_id']==101:
                self.fail=False
                raise ConnectionError('temporary error')
            await super().delete_message(**kw)
    bot=UnreliableBot()
    asyncio.run(handler_module.cleanup_closed_panels(bot,qid))
    assert db.one('SELECT * FROM personal_panels WHERE queue_id=? AND user_id=101',(qid,))
    asyncio.run(handler_module.cleanup_closed_panels(bot,qid))
    assert not db.all('SELECT * FROM personal_panels WHERE queue_id=?',(qid,))


def test_legacy_survey_state_migrates_into_no_survey_flow(tmp_path):
    cfg,db,svc,qid=make_active(tmp_path)
    db.exec("UPDATE queues SET status='feedback' WHERE id=?",(qid,))
    db.exec("INSERT INTO feedback(queue_id,user_id,result,updated_at) VALUES(?,?,?,?)",
            (qid,101,'no',utc_iso(utc_now())))
    Database(db.path)  # opens DB with migration
    assert db.queue(qid)['status']=='active'
    assert all(m['priority']==0 for m in db.members(qid))
    assert not svc.candidates(qid)[0].priority
    svc.finish(qid)
    assert db.queue(qid)['status']=='closed'


def test_config_ignores_old_90_minute_env(monkeypatch):
    monkeypatch.setenv('BOT_TOKEN','safe_test_token')
    monkeypatch.setenv('LESSON_DURATION_MINUTES','90')
    monkeypatch.setenv('FEEDBACK_HOURS','12')
    assert load_config().lesson_minutes==100


def test_deadline_worker_not_before_but_after_100_minutes(tmp_path,handler_module,monkeypatch):
    # The handler fixture installs an aiogram stub; extend it for runner imports.
    import sys
    from types import ModuleType
    monkeypatch.setattr(sys.modules['aiogram'], 'Dispatcher', type('Dispatcher', (), {}), raising=False)
    client = ModuleType('aiogram.client'); client.__path__ = []
    default = ModuleType('aiogram.client.default')
    default.DefaultBotProperties = type('DefaultBotProperties', (), {})
    monkeypatch.setitem(sys.modules, 'aiogram.client', client)
    monkeypatch.setitem(sys.modules, 'aiogram.client.default', default)
    from queuebot import runner
    cfg,db,svc,qid=make_active(tmp_path)
    cfg.lesson_minutes=100
    handler_module.setup(db,cfg,svc,'test_bot')
    class UnreliableBot(FakeBot):
        failed = False

        async def edit_message_text(self, text, **kwargs):
            if 'Завершена' in text and not self.failed:
                self.failed = True
                raise ConnectionError('temporary error editing public archive')
            return await super().edit_message_text(text, **kwargs)

    bot=UnreliableBot()
    async def once():
        raise asyncio.CancelledError()
    monkeypatch.setattr(runner.asyncio, 'sleep', lambda delay: once())
    # At 99 minutes the queue remains active with all cards intact.
    db.exec('UPDATE queues SET lesson_at=? WHERE id=?',
            (utc_iso(utc_now()-timedelta(minutes=99)),qid))
    try:
        asyncio.run(runner.deadline_worker(bot,db,svc,cfg))
    except asyncio.CancelledError:
        pass
    assert db.queue(qid)['status']=='active'
    assert len(db.all('SELECT * FROM personal_panels WHERE queue_id=?',(qid,)))==3
    # This cancelled queue was left with a card before the worker started.
    cancelled = svc.create(-10042, 101, 'Математика', utc_now()+timedelta(days=1), .05)
    db.exec('INSERT INTO personal_panels(queue_id,user_id,message_id) VALUES(?,?,?)',
            (cancelled, 101, 704))
    svc.cancel(cancelled)
    # At 101 minutes it closes, deletes private cards and retains group archive.
    db.exec('UPDATE queues SET lesson_at=? WHERE id=?',
            (utc_iso(utc_now()-timedelta(minutes=101)),qid))
    try:
        asyncio.run(runner.deadline_worker(bot,db,svc,cfg))
    except asyncio.CancelledError:
        pass
    assert db.queue(qid)['status']=='closed'
    assert not db.all('SELECT * FROM personal_panels WHERE queue_id=?',(qid,))
    assert not db.all('SELECT * FROM personal_panels WHERE queue_id=?',(cancelled,))
    failed = db.queue(qid)
    assert failed['group_synced_revision'] < failed['revision'] and failed['group_retry_at']

    # Once the retry is due, a restart still recovers the public archive.
    db.exec('UPDATE queues SET group_retry_at=? WHERE id=?', (utc_iso(utc_now()), qid))
    db = Database(db.path)
    svc = QueueService(db, cfg)
    handler_module.setup(db,cfg,svc,'test_bot')
    try:
        asyncio.run(runner.deadline_worker(bot,db,svc,cfg))
    except asyncio.CancelledError:
        pass
    q = db.queue(qid)
    assert q['group_synced_revision'] == q['revision']
    assert any(c[0]=='edit' and c[1]==-10042 and 'Завершена' in c[3] for c in bot.calls)
