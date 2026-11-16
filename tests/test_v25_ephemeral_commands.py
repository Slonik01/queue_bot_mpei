"""v2.5: commands in groups are truly ephemeral, classic layout restored."""
import asyncio
from types import SimpleNamespace
from test_handlers_v21 import FakeBot, handler_module
from queuebot.presentation import ik


def test_group_commands_registered_ephemeral(handler_module):
    bot = FakeBot()
    asyncio.run(handler_module.register_commands(bot))
    calls = [c for c in bot.calls if c[0]=='set_commands']
    assert len(calls) == 4
    private, admins, members, default = [c[3] for c in calls]
    assert {'start','my','info','help'} == {cmd.command for cmd in private}
    assert {'queue','close_reg','finish_lesson','cancel'} == {cmd.command for cmd in admins}
    assert not members and not default
    assert all(cmd.is_ephemeral is True for cmd in admins)
    assert all(getattr(cmd,'is_ephemeral',None) is None for cmd in private)


def test_ephemeral_group_command_deleted_for_its_recipient(handler_module):
    bot = FakeBot()
    message = SimpleNamespace(chat=SimpleNamespace(id=-10042),
                              from_user=SimpleNamespace(id=101),
                              receiver_user=SimpleNamespace(id=101),
                              ephemeral_message_id=42, message_id=0)
    asyncio.run(handler_module.delete_input(bot,message))
    assert bot.calls == [('delete_ephemeral', -10042, 42, None,
                          {'chat_id': -10042, 'receiver_user_id': 101,
                           'ephemeral_message_id': 42})]


def test_ephemeral_group_command_falls_back_to_sender_if_receiver_omitted(handler_module):
    bot = FakeBot()
    message = SimpleNamespace(chat=SimpleNamespace(id=-10042),
                              from_user=SimpleNamespace(id=303),
                              ephemeral_message_id=72, message_id=0)
    asyncio.run(handler_module.delete_input(bot,message))
    assert bot.calls[0][4]['receiver_user_id'] == 303


def test_legacy_public_group_command_deleted(handler_module):
    bot = FakeBot()
    message = SimpleNamespace(chat=SimpleNamespace(id=-10042), ephemeral_message_id=None, message_id=111)
    asyncio.run(handler_module.delete_input(bot,message))
    assert any(c[0]=='delete' and c[1]==-10042 and c[2]==111 for c in bot.calls)


def test_no_button_styles():
    kb=ik([[('✍️ Записаться','start:q'),('🚪 Выйти','leave:q')]])
    assert all(getattr(b,'style',None) is None for row in kb.inline_keyboard for b in row)


def test_queue_command_is_deleted_after_handling_as_ephemeral(tmp_path, handler_module):
    from datetime import timedelta
    from queuebot.config import utc_now
    from queuebot.db import Database
    from queuebot.service import QueueService
    from test_handlers_v21 import Settings

    cfg = Settings()
    cfg.admins = {101}
    db = Database(str(tmp_path / 'queue.sqlite3'))
    svc = QueueService(db, cfg)
    handler_module.setup(db, cfg, svc, 'test_bot')
    future = (utc_now() + timedelta(days=2)).astimezone(cfg.tz).strftime('%d.%m.%Y %H:%M')
    bot = FakeBot()
    message = SimpleNamespace(from_user=SimpleNamespace(id=101),
                              receiver_user=SimpleNamespace(id=101),
                              chat=SimpleNamespace(id=-10042, type='supergroup'),
                              text=f'/queue Прога {future} 3м',
                              message_id=0, ephemeral_message_id=927,
                              message_thread_id=221)
    asyncio.run(handler_module.create_queue(message, bot))
    assert any(c[0] == 'send' and c[1] == -10042 and c[4].get('message_thread_id') == 221 for c in bot.calls)
    assert any(c[0] == 'delete_ephemeral' and c[2] == 927 for c in bot.calls)
    assert not any(c[0] == 'delete' for c in bot.calls)
