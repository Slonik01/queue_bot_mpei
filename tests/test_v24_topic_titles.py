"""Queue commands publish in the current topic and silently discard invalid input."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from queuebot.config import utc_now, utc_iso
from queuebot.db import Database
from queuebot.presentation import personal_message
from queuebot.service import DomainError, QueueService
from test_handlers_v21 import FakeBot, Settings, handler_module, mock_call


def mock_group(text='/queue', thread=711, uid=101):
    return SimpleNamespace(
        chat=SimpleNamespace(type='supergroup', id=-10042, title='Учебная группа'),
        from_user=SimpleNamespace(id=uid), text=text, message_thread_id=thread,
        message_id=9000, ephemeral_message_id=None,
    )


def setup(tmp_path, mod):
    cfg = Settings()
    db = Database(str(tmp_path / 'bot.sqlite3'))
    mod.setup(db, cfg, QueueService(db, cfg), 'example_bot')
    return db, FakeBot()


def test_queue_with_multiword_title_is_posted_in_current_topic(tmp_path, handler_module):
    db, bot = setup(tmp_path, handler_module)
    future = (utc_now() + timedelta(days=3)).astimezone(handler_module.CFG.tz)
    message = mock_group(f'/queue@Example_Bot Высшая математика {future:%d.%m.%Y %H:%M} 30м')
    asyncio.run(handler_module.create_queue(message, bot))
    queue = db.one('SELECT * FROM queues')
    assert queue['title'] == 'Высшая математика'
    assert queue['message_thread_id'] == 711
    assert queue['lesson_at'] == utc_iso(future.replace(second=0, microsecond=0))
    assert [call[0] for call in bot.calls] == ['get_member', 'send', 'delete']
    assert bot.calls[1][1] == -10042
    assert bot.calls[1][4]['message_thread_id'] == 711


@pytest.mark.parametrize('command', [
    '/queue',
    '/queue Физика {date} 13:45',
    '/queue Физика {date} 3 6ч',
    '/queue Физика | {date} 13:45 | 6ч',
    '/queue Физика 99.88.2026 13:45 6ч',
    '/queue Физика {date} 13:45 2м',
    '/queue Физика {date} 13:45 бессрочно',
    '/queue Физика {date} 13:45 30м 2',
])
def test_invalid_queue_command_only_deletes_the_input(tmp_path, handler_module, command):
    db, bot = setup(tmp_path, handler_module)
    date = (utc_now() + timedelta(days=3)).astimezone(handler_module.CFG.tz)
    asyncio.run(handler_module.create_queue(mock_group(command.format(date=f'{date:%d.%m.%Y}')), bot))
    assert not db.one('SELECT id FROM queues')
    assert [call[0] for call in bot.calls if call[0] != 'get_member'] == ['delete']


@pytest.mark.parametrize('suffix', ['', ' 1'])
def test_queue_uses_chat_title_and_keeps_bare_hour_duration(tmp_path, handler_module, suffix):
    db, bot = setup(tmp_path, handler_module)
    date = (utc_now() + timedelta(days=3)).astimezone(handler_module.CFG.tz)
    message = mock_group(f'/queue {date:%d.%m.%Y} 13:45 1{suffix}', thread=None)
    asyncio.run(handler_module.create_queue(message, bot))
    q = db.one('SELECT * FROM queues')
    assert q is not None
    assert q['title'] == 'Учебная группа'
    assert q['use_preferences'] == 1


def test_queue_uses_topic_title_from_service_message_after_restart(tmp_path, handler_module):
    db, bot = setup(tmp_path, handler_module)
    future = (utc_now() + timedelta(days=3)).astimezone(handler_module.CFG.tz)
    created = mock_group('', thread=None)
    created.message_id = 711
    created.forum_topic_created = SimpleNamespace(name='Лабораторные')
    first = mock_group(f'/queue {future:%d.%m.%Y %H:%M} 30м')
    first.reply_to_message = created
    asyncio.run(handler_module.create_queue(first, bot))
    q = db.one('SELECT * FROM queues')
    assert q is not None and q['title'] == 'Лабораторные'

    reopened = Database(db.path)
    handler_module.setup(reopened, handler_module.CFG,
                         QueueService(reopened, handler_module.CFG), 'example_bot')
    edited = mock_group('')
    edited.forum_topic_edited = SimpleNamespace(name='Практикум')
    asyncio.run(handler_module.cache_topic_name(edited))
    asyncio.run(handler_module.create_queue(
        mock_group(f'/queue {future + timedelta(days=1):%d.%m.%Y %H:%M} 30м'), bot,
    ))
    titles = reopened.all('SELECT title,subject_id FROM queues ORDER BY id')
    assert [row['title'] for row in titles] == ['Лабораторные', 'Практикум']
    assert titles[0]['subject_id'] == titles[1]['subject_id']


def test_random_queue_draw_and_personal_card_skip_preferences(tmp_path, handler_module):
    db, bot = setup(tmp_path, handler_module)
    cfg = handler_module.CFG
    date = (utc_now() + timedelta(days=3)).astimezone(cfg.tz)
    asyncio.run(handler_module.create_queue(mock_group(f'/queue {date:%d.%m.%Y} 13:45 30м 0'), bot))
    q = db.one('SELECT * FROM queues')
    assert q is not None
    qid, subject = q['id'], q['subject_id']

    db = Database(db.path)
    svc = QueueService(db, cfg)
    handler_module.setup(db, cfg, svc, 'example_bot')
    for uid in (101, 202, 303, 404):
        db.exec('INSERT INTO users(id,name,created_at) VALUES(?,?,?)', (uid, str(uid), utc_iso(utc_now())))
        svc.signup(qid, uid)
    svc.withdraw(qid, 404)
    db.exec('INSERT INTO fairness(subject_id,user_id,balance) VALUES(?,?,?)', (subject, 101, 42))

    _, markup = personal_message(db, qid, 101, cfg)
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == [f'leave:{qid}']
    with pytest.raises(DomainError):
        svc.preference(qid, 101, 'exact', 1)
    bot.calls.clear()
    asyncio.run(handler_module.ask_exact(mock_call(101, f'exact:{qid}'), bot))
    assert not bot.calls

    db.exec("UPDATE participants SET pref_mode='exact',desired=1 WHERE queue_id=?", (qid,))
    db.exec('UPDATE queues SET seed=?,closes_at=? WHERE id=?',
            ('01', utc_iso(utc_now() - timedelta(seconds=1)), qid))
    assert svc.draw(qid) == {202: 1, 303: 2, 101: 3}
    assert [(row['user_id'], row['current_position']) for row in db.members(qid)] == [
        (202, 1), (303, 2), (101, 3),
    ]
    assert [tuple(row) for row in db.all('SELECT user_id,balance FROM fairness')] == [(101, 42)]


@pytest.mark.parametrize(('handler_name', 'command'), [
    ('close_reg_cmd', '/close_reg'),
    ('finish_lesson_cmd', '/finish_lesson ID'),
    ('cancel_cmd', '/cancel 9999999999999999999999999'),
    ('close_reg_cmd', '/close_reg 123'),
])
def test_admin_command_with_invalid_id_only_deletes_the_input(
    tmp_path, handler_module, handler_name, command,
):
    _, bot = setup(tmp_path, handler_module)
    handler = getattr(handler_module, handler_name)
    asyncio.run(handler(mock_group(command), bot))
    assert [call[0] for call in bot.calls if call[0] != 'get_member'] == ['delete']


def test_close_registration_and_finish_lesson_update_the_group(tmp_path, handler_module):
    db, bot = setup(tmp_path, handler_module)
    qid = handler_module.SERVICE.create(-10042, 101, 'Физика', utc_now() + timedelta(days=2), 1,
                                        message_thread_id=711)
    db.exec('UPDATE queues SET group_message_id=55 WHERE id=?', (qid,))
    asyncio.run(handler_module.close_reg_cmd(mock_group(f'/close_reg {qid}'), bot))
    assert db.queue(qid)['status'] == 'active'

    bot.calls.clear()
    asyncio.run(handler_module.finish_lesson_cmd(mock_group(f'/finish_lesson {qid}'), bot))
    assert db.queue(qid)['status'] == 'active'
    assert [call[0] for call in bot.calls] == ['get_member', 'delete']

    db.exec('UPDATE queues SET lesson_at=? WHERE id=?', (utc_iso(utc_now() - timedelta(minutes=1)), qid))
    bot.calls.clear()
    asyncio.run(handler_module.finish_lesson_cmd(mock_group(f'/finish_lesson {qid}'), bot))
    assert db.queue(qid)['status'] == 'closed'
    assert [call[0] for call in bot.calls] == ['get_member', 'edit', 'delete']
