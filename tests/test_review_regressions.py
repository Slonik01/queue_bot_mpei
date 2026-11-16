import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

from queuebot.config import utc_iso, utc_now
from queuebot.db import Database, SCHEMA
from queuebot.service import DomainError, QueueService
from test_handlers_v21 import FakeBot, Settings, handler_module
from test_v24_topic_titles import mock_group
from test_v27_end_of_lesson import make_active


def test_topic_identity_survives_rename_and_cancelled_queue_can_be_recreated(tmp_path):
    db = Database(str(tmp_path / 'topics.sqlite3'))
    svc = QueueService(db, Settings())
    start = utc_now() + timedelta(days=2)
    first = svc.create(-100, 1, 'Original', start, 1, message_thread_id=42)
    subject_id = db.queue(first)['subject_id']
    db.exec('INSERT INTO fairness(subject_id,user_id,balance) VALUES(?,?,?)', (subject_id, 1, 17))

    with pytest.raises(DomainError, match='уже существует'):
        svc.create(-100, 1, 'Renamed', start, 1, message_thread_id=42)
    svc.cancel(first)
    second = svc.create(-100, 1, 'Renamed', start, 1, message_thread_id=42)
    other = svc.create(-100, 1, 'Renamed', start, 1, message_thread_id=43)
    assert db.queue(first)['status'] == 'cancelled'
    assert db.queue(first)['title'] == 'Original'
    assert db.queue(second)['title'] == 'Renamed'
    assert db.queue(second)['subject_id'] == subject_id
    assert db.queue(other)['subject_id'] != subject_id
    assert db.one('SELECT balance FROM fairness WHERE subject_id=?', (subject_id,))[0] == 17


def test_legacy_database_merges_topic_history_and_keeps_dependent_rows(tmp_path):
    path = str(tmp_path / 'v28.sqlite3')
    start = utc_iso(utc_now() + timedelta(days=2))
    now = utc_iso(utc_now())
    with sqlite3.connect(path) as raw:
        raw.executescript("""
            CREATE TABLE subjects (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                title TEXT NOT NULL COLLATE NOCASE, UNIQUE(chat_id,title)
            );
            CREATE TABLE queues (
                id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER NOT NULL REFERENCES subjects(id),
                chat_id INTEGER NOT NULL, creator_id INTEGER NOT NULL,
                lesson_at TEXT NOT NULL, closes_at TEXT NOT NULL, feedback_deadline TEXT,
                status TEXT NOT NULL, group_message_id INTEGER, message_thread_id INTEGER,
                seed TEXT NOT NULL, seed_hash TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, UNIQUE(subject_id,lesson_at)
            );
        """ + SCHEMA)
        raw.execute("INSERT INTO app_metadata VALUES('statistics_reset_v28','done')")
        raw.execute("INSERT INTO users(id,name,created_at) VALUES(1,'Student',?)", (now,))
        raw.execute("INSERT INTO subjects VALUES(1,-100,'First'),(2,-100,'Renamed')")
        raw.executemany("""INSERT INTO queues(
            id,subject_id,chat_id,creator_id,lesson_at,closes_at,status,group_message_id,
            message_thread_id,seed,seed_hash,created_at)
            VALUES(?,?,-100,1,?,?,'registration',55,42,'cafe','',?)""",
            [(1, 1, start, now, now), (2, 2, start, now, now)])
        raw.execute('UPDATE sqlite_sequence SET seq=50 WHERE name=?', ('queues',))
        raw.execute('INSERT INTO participants(queue_id,user_id,joined_at) VALUES(1,1,?)', (now,))
        raw.execute('INSERT INTO personal_panels(queue_id,user_id,message_id) VALUES(1,1,66)')
        raw.executemany('INSERT INTO fairness(subject_id,user_id,balance,updated_at) VALUES(?,1,?,?)',
                        [(1, 12, now), (2, 23, start)])
        raw.commit()

    db = Database(path)
    db = Database(path)
    svc = QueueService(db, Settings())
    assert db.all('PRAGMA foreign_key_check') == []
    assert db.members(1)[0]['user_id'] == 1
    assert db.one('SELECT message_id FROM personal_panels WHERE queue_id=1')[0] == 66
    assert db.queue(1)['group_message_id'] == 55
    assert db.queue(1)['group_synced_revision'] == -1
    assert db.queue(1)['title'] == 'First'
    assert db.queue(2)['title'] == 'Renamed'
    subject_id = db.queue(1)['subject_id']
    assert subject_id == db.queue(2)['subject_id']
    assert db.one('SELECT balance FROM fairness WHERE subject_id=?', (subject_id,))[0] == 23
    svc.cancel(2)
    with pytest.raises(DomainError, match='уже существует'):
        svc.create(-100, 1, 'Today', datetime.fromisoformat(start), 1, message_thread_id=42)
    svc.cancel(1)
    fresh = svc.create(-100, 1, 'Today', datetime.fromisoformat(start), 1, message_thread_id=42)
    assert fresh > 50


def test_expired_swap_is_committed_before_reporting_an_error(tmp_path):
    _, db, svc, qid = make_active(tmp_path)
    original = [p['current_position'] for p in db.members(qid)]
    swap_id = svc.swap_request(qid, 101, 202)
    db.exec('UPDATE swaps SET expires_at=? WHERE id=?',
            (utc_iso(utc_now() - timedelta(seconds=1)), swap_id))
    with pytest.raises(DomainError, match='истёк'):
        svc.swap_decision(swap_id, 202, True)
    saved = db.one('SELECT status,handled_at FROM swaps WHERE id=?', (swap_id,))
    assert saved['status'] == 'expired' and saved['handled_at']
    assert [p['current_position'] for p in db.members(qid)] == original


def test_concurrent_public_edits_apply_newest_revision_last(tmp_path, handler_module):
    cfg, db, svc, qid = make_active(tmp_path)
    handler_module.setup(db, cfg, svc, 'test_bot')

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        class DelayedBot(FakeBot):
            async def edit_message_text(self, text, **kwargs):
                if 'Завершена' not in text:
                    started.set()
                    await release.wait()
                return await super().edit_message_text(text, **kwargs)

        bot = DelayedBot()
        older = asyncio.create_task(handler_module.update_group(bot, qid))
        await started.wait()
        svc.finish(qid)
        newer = asyncio.create_task(handler_module.update_group(bot, qid))
        await asyncio.sleep(0)
        assert not bot.calls
        release.set()
        await asyncio.gather(older, newer)
        assert ['Завершена' in call[3] for call in bot.calls] == [False, True]
        q = db.queue(qid)
        assert q['group_synced_revision'] == q['revision']

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_failed_publication_can_be_retried_for_same_lesson(tmp_path, handler_module):
    cfg = Settings()
    db = Database(str(tmp_path / 'publish.sqlite3'))
    svc = QueueService(db, cfg)
    handler_module.setup(db, cfg, svc, 'test_bot')
    lesson = (utc_now() + timedelta(days=2)).astimezone(cfg.tz).strftime('%d.%m.%Y %H:%M')
    message = mock_group(f'/queue Test {lesson} 1ч')

    class UnavailableBot(FakeBot):
        async def send_message(self, chat_id, *args, **kwargs):
            if chat_id == message.chat.id:
                raise ConnectionError('temporary failure')
            return await super().send_message(chat_id, *args, **kwargs)

    unavailable = UnavailableBot()
    asyncio.run(handler_module.create_queue(message, unavailable))
    assert [call[0] for call in unavailable.calls] == ['get_member', 'delete']
    asyncio.run(handler_module.create_queue(message, FakeBot()))
    queues = db.all('SELECT status,group_message_id FROM queues ORDER BY id')
    assert [tuple(q) for q in queues] == [('cancelled', None), ('registration', 12345)]


def test_cancel_retires_cards_including_withdrawn_members(tmp_path, handler_module):
    cfg, db, svc, qid = make_active(tmp_path)
    svc.withdraw(qid, 202)
    handler_module.setup(db, cfg, svc, 'test_bot')

    class OldCardBot(FakeBot):
        async def delete_message(self, **kwargs):
            if kwargs['chat_id'] == 202:
                raise handler_module.TelegramBadRequest("message can't be deleted")
            await super().delete_message(**kwargs)

    bot = OldCardBot()
    asyncio.run(handler_module.cancel_cmd(mock_group(f'/cancel {qid}'), bot))
    assert db.queue(qid)['status'] == 'cancelled'
    assert not db.all('SELECT * FROM personal_panels')
    assert {call[1] for call in bot.calls if call[0] == 'delete'} == {101, 303, -10042}
    edit = next(call for call in bot.calls if call[0] == 'edit' and call[1] == 202)
    assert 'отменена' in edit[3] and edit[4]['reply_markup'] is None
