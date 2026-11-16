"""Behavioural tests of the Telegram handlers with a fake transport.

No Telegram token, external network, or aiogram installation required.
"""
import asyncio
import importlib
import sys
from datetime import timedelta
from types import ModuleType, SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from queuebot.config import utc_now, utc_iso
from queuebot.db import Database
from queuebot.service import QueueService


class Settings:
    max_participants = 35
    feedback_hours = 12

    @property
    def tz(self):
        return ZoneInfo('Europe/Moscow')


class FakeBot:
    def __init__(self, administrators=None):
        self.calls = []
        self.administrators = {101} if administrators is None else set(administrators)

    async def get_chat_member(self, chat_id, uid):
        self.calls.append(('get_member', chat_id, uid, None, {}))
        return SimpleNamespace(status='administrator' if uid in self.administrators else 'member')

    async def edit_message_text(self, text, **kwargs):
        self.calls.append(('edit', kwargs['chat_id'], kwargs['message_id'], text, kwargs))
        return True

    async def send_message(self, *args, **kwargs):
        user_or_group = kwargs.get('chat_id', args[0] if args else None)
        self.calls.append(('send', user_or_group, None, kwargs.get('text', args[1] if len(args)>1 else ''), kwargs))
        return SimpleNamespace(message_id=12345)

    async def delete_message(self, **kwargs):
        self.calls.append(('delete', kwargs['chat_id'], kwargs['message_id'], None, kwargs))

    async def delete_ephemeral_message(self, **kwargs):
        self.calls.append(('delete_ephemeral', kwargs['chat_id'], kwargs['ephemeral_message_id'], None, kwargs))

    async def set_my_commands(self, commands, scope):
        self.calls.append(('set_commands', scope.__class__.__name__, None, commands, {}))


@pytest.fixture
def handler_module(monkeypatch):
    """Provide only the Bot API type skeleton to test code without aiogram."""
    m = ModuleType('aiogram')
    m.__path__ = []
    filters = ModuleType('aiogram.filters')
    types = ModuleType('aiogram.types')
    exceptions = ModuleType('aiogram.exceptions')

    class NoOp:
        def __call__(self, *args, **kwargs):
            return lambda fn: fn

    class DummyRouter:
        def __init__(self):
            self.message = NoOp()
            self.callback_query = NoOp()

    class MagicF:
        def __getattr__(self, key):
            return self

        def __eq__(self, value):
            return self

        def in_(self, values):
            return self

        def startswith(self, value):
            return self

    class Attrs:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class TelegramAPIError(Exception):
        pass

    class TelegramBadRequest(TelegramAPIError):
        pass

    class TelegramForbiddenError(TelegramAPIError):
        pass

    class TelegramRetryAfter(TelegramAPIError):
        pass

    m.Router, m.F, m.Bot = DummyRouter, MagicF(), type('Bot', (), {})
    filters.Command = filters.CommandStart = lambda *args, **kwargs: None
    for name in ('Message','CallbackQuery','EphemeralMessageParameters','ReplyParameters',
                 'BotCommand','BotCommandScopeAllPrivateChats','BotCommandScopeAllChatAdministrators',
                 'BotCommandScopeAllGroupChats','BotCommandScopeDefault',
                 'InlineKeyboardMarkup','InlineKeyboardButton'):
        setattr(types, name, type(name, (Attrs,), {}))
    exceptions.TelegramAPIError = TelegramAPIError
    exceptions.TelegramBadRequest = TelegramBadRequest
    exceptions.TelegramForbiddenError = TelegramForbiddenError
    exceptions.TelegramRetryAfter = TelegramRetryAfter
    monkeypatch.setitem(sys.modules, 'aiogram', m)
    monkeypatch.setitem(sys.modules, 'aiogram.filters', filters)
    monkeypatch.setitem(sys.modules, 'aiogram.types', types)
    monkeypatch.setitem(sys.modules, 'aiogram.exceptions', exceptions)
    sys.modules.pop('queuebot.handlers', None)
    handlers = importlib.import_module('queuebot.handlers')
    yield handlers
    sys.modules.pop('queuebot.handlers', None)


def make_queue(tmp_path):
    db = Database(str(tmp_path / 'bot.sqlite3'))
    svc = QueueService(db, Settings())
    for uid in (101, 202):
        with db.tx() as conn:
            conn.execute('INSERT INTO users(id,name,created_at) VALUES(?,?,?)',
                         (uid, f'Участник {uid}', utc_iso(utc_now())))
    qid = svc.create(-10042, 101, 'Тест', utc_now() + timedelta(days=2), 0.05)
    svc.signup(qid, 101)
    svc.signup(qid, 202)
    db.exec('UPDATE queues SET closes_at=? WHERE id=?', (utc_iso(utc_now()-timedelta(seconds=1)), qid))
    svc.draw(qid)
    return db, svc, qid


def mock_call(sender, data):
    responses=[]
    async def answer(text=None, **kwargs):
        responses.append((text,kwargs))
    return SimpleNamespace(from_user=SimpleNamespace(id=sender), data=data, answer=answer, responses=responses)


def test_swap_changes_existing_recipient_message_and_does_not_send_new(tmp_path, handler_module):
    db, svc, qid = make_queue(tmp_path)
    db.exec('INSERT INTO personal_panels(queue_id,user_id,message_id,view) VALUES(?,?,?,?)',
            (qid,101,701,'swaps'))
    db.exec('INSERT INTO personal_panels(queue_id,user_id,message_id,view) VALUES(?,?,?,?)',
            (qid,202,702,'main'))
    handler_module.setup(db, Settings(), svc, 'test_bot')
    bot=FakeBot()
    call=mock_call(101, f'swap:{qid}:202')
    asyncio.run(handler_module.offer_swap(call, bot))
    recipient_edits=[x for x in bot.calls if x[0]=='edit' and x[1]==202 and x[2]==702]
    assert len(recipient_edits)==1
    assert 'предлагает обменяться' in recipient_edits[0][3]
    assert not any(x[0]=='send' for x in bot.calls)
    assert db.one('SELECT status FROM swaps WHERE queue_id=?',(qid,))['status']=='pending'
    assert db.one('SELECT message_id FROM personal_panels WHERE queue_id=? AND user_id=?',(qid,202))['message_id']==702


def test_swap_does_not_create_unwanted_recipient_message(tmp_path, handler_module):
    db, svc, qid=make_queue(tmp_path)
    db.exec('INSERT INTO personal_panels(queue_id,user_id,message_id,view) VALUES(?,?,?,?)',
            (qid,101,701,'swaps'))
    handler_module.setup(db, Settings(), svc, 'test_bot')
    bot=FakeBot()
    call=mock_call(101, f'swap:{qid}:202')
    asyncio.run(handler_module.offer_swap(call,bot))
    assert not any(x[0]=='send' and x[1]==202 for x in bot.calls)
    assert db.one('SELECT status FROM swaps WHERE queue_id=?',(qid,))['status']=='cancelled'
    assert call.responses == [(None, {})]


def test_group_help_is_deleted_without_a_private_reply(handler_module):
    handler_module.setup(None,None,None,'test_bot')
    bot=FakeBot()
    message=SimpleNamespace(from_user=SimpleNamespace(id=101),
                            chat=SimpleNamespace(id=-10042,type='supergroup'),
                            text='/help', message_id=555, ephemeral_message_id=None)
    asyncio.run(handler_module.discard_group_command(message,bot))
    assert [call[0] for call in bot.calls] == ['delete']


def test_group_commands_are_ephemeral(handler_module):
    bot=FakeBot()
    asyncio.run(handler_module.register_commands(bot))
    assert len(bot.calls)==4
    assert all(getattr(cmd, 'is_ephemeral', False) for call in bot.calls[1:] for cmd in call[3])
    assert all(not getattr(cmd, 'is_ephemeral', False) for cmd in bot.calls[0][3])
    assert 'info' in [x.command for x in bot.calls[0][3]]


def test_queue_posted_in_same_forum_topic(tmp_path, handler_module):
    db=Database(str(tmp_path/'topic.sqlite3'))
    svc=QueueService(db,Settings())
    cfg=Settings()
    cfg.admins={101}
    handler_module.setup(db,cfg,svc,'test_bot')
    bot=FakeBot()
    future=(utc_now()+timedelta(days=2)).astimezone(cfg.tz).strftime('%d.%m.%Y %H:%M')
    msg=SimpleNamespace(from_user=SimpleNamespace(id=101),
                        chat=SimpleNamespace(id=-10042,type='supergroup'),
                        text=f'/queue ОС {future} 3м',message_id=555,
                        message_thread_id=234,ephemeral_message_id=None)
    asyncio.run(handler_module.create_queue(msg,bot))
    sent=[x for x in bot.calls if x[0]=='send' and x[1]==-10042]
    assert len(sent)==1
    assert sent[0][4]['message_thread_id']==234
    q=db.one('SELECT * FROM queues LIMIT 1')
    assert q['message_thread_id']==234
    assert [x[0] for x in bot.calls][-1]=='delete'


def test_queue_posted_in_normal_chat_without_topic_arg(tmp_path,handler_module):
    db=Database(str(tmp_path/'normal.sqlite3'))
    svc=QueueService(db,Settings())
    cfg=Settings()
    cfg.admins={101}
    handler_module.setup(db,cfg,svc,'test_bot')
    bot=FakeBot()
    future=(utc_now()+timedelta(days=2)).astimezone(cfg.tz).strftime('%d.%m.%Y %H:%M')
    msg=SimpleNamespace(from_user=SimpleNamespace(id=101),
                        chat=SimpleNamespace(id=-10042,type='supergroup'),
                        text=f'/queue ОС {future} 3м',message_id=556,
                        message_thread_id=None,ephemeral_message_id=None)
    asyncio.run(handler_module.create_queue(msg,bot))
    sent=[x for x in bot.calls if x[0]=='send' and x[1]==-10042]
    assert len(sent)==1
    assert 'message_thread_id' not in sent[0][4]


def test_finish_deletes_all_panels_and_keeps_group_list(tmp_path, handler_module):
    db, svc, qid = make_queue(tmp_path)
    db.exec('UPDATE queues SET group_message_id=? WHERE id=?', (777, qid))
    db.exec('INSERT INTO personal_panels(queue_id,user_id,message_id) VALUES(?,?,?)', (qid,101,701))
    db.exec('INSERT INTO personal_panels(queue_id,user_id,message_id) VALUES(?,?,?)', (qid,202,702))
    handler_module.setup(db, Settings(), svc, 'test_bot')
    bot = FakeBot()
    asyncio.run(handler_module.finish_and_notify(bot, qid))
    assert db.queue(qid)['status'] == 'closed'
    assert ('delete', 101, 701, None, {'chat_id':101,'message_id':701}) in bot.calls
    assert ('delete', 202, 702, None, {'chat_id':202,'message_id':702}) in bot.calls
    assert not db.one('SELECT 1 FROM personal_panels WHERE queue_id=?', (qid,))
    edits = [x for x in bot.calls if x[0]=='edit' and x[1]==-10042]
    assert edits and 'Завершена' in edits[0][3] and 'Порядок сдачи' in edits[0][3]
    calls_so_far = len(bot.calls)
    asyncio.run(handler_module.refresh_panel(bot, qid, 101, view='main'))
    assert len(bot.calls) == calls_so_far

def test_info_content_explains_rules(handler_module):
    assert '95%' in handler_module.INFO
    assert 'вернуться' in handler_module.INFO
    assert '1 час 40 минут' in handler_module.INFO
    assert '/info' in handler_module.INFO
    assert 'опрос' not in handler_module.HELP.lower()
    assert '/setresult' not in handler_module.INFO

