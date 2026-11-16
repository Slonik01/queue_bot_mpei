"""Queue commands publish in the current topic and silently discard invalid input."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from queuebot.config import utc_now, utc_iso
from queuebot.db import Database
from queuebot.service import QueueService
from test_handlers_v21 import FakeBot, Settings, handler_module


def mock_group(text='/queue', thread=711, uid=101):
    return SimpleNamespace(
        chat=SimpleNamespace(type='supergroup', id=-10042),
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
    '/queue {date} 13:45 6ч',
    '/queue Физика {date} 13:45',
    '/queue Физика {date} 3 6ч',
    '/queue Физика | {date} 13:45 | 6ч',
    '/queue Физика 99.88.2026 13:45 6ч',
    '/queue Физика {date} 13:45 2м',
    '/queue Физика {date} 13:45 бессрочно',
])
def test_invalid_queue_command_only_deletes_the_input(tmp_path, handler_module, command):
    db, bot = setup(tmp_path, handler_module)
    date = (utc_now() + timedelta(days=3)).astimezone(handler_module.CFG.tz)
    asyncio.run(handler_module.create_queue(mock_group(command.format(date=f'{date:%d.%m.%Y}')), bot))
    assert not db.one('SELECT id FROM queues')
    assert [call[0] for call in bot.calls if call[0] != 'get_member'] == ['delete']


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
