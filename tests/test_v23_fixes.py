"""Regression tests for personal queue controls."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

from queuebot.config import utc_now, utc_iso
from queuebot.db import Database
from queuebot.service import QueueService
from test_handlers_v21 import FakeBot, Settings, mock_call, handler_module


def _private(uid, text):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=uid),
        chat=SimpleNamespace(type='private', id=uid),
        text=text, message_id=121, ephemeral_message_id=None,
    )


def _fixture(tmp_path, mod):
    cfg=Settings()
    db=Database(str(tmp_path / 'bot.sqlite3'))
    svc=QueueService(db,cfg)
    mod.setup(db,cfg,svc,'test_bot')
    return db,svc,cfg,FakeBot()


def test_leave_during_registration_edits_to_confirm_then_withdraws(tmp_path, handler_module):
    mod=handler_module
    db,svc,cfg,bot=_fixture(tmp_path,mod)
    db.exec('INSERT INTO users(id,name,created_at) VALUES(?,?,?)', (101,'Леонид',utc_iso(utc_now())))
    qid=svc.create(-10042,101,'Прога',utc_now()+timedelta(days=2),6)
    svc.signup(qid,101)
    db.exec('INSERT INTO personal_panels(queue_id,user_id,message_id,view) VALUES(?,?,?,?)',
            (qid,101,777,'main'))
    asyncio.run(mod.ask_leave(mock_call(101,f'leave:{qid}'),bot))
    confirm_edits=[x for x in bot.calls if x[0]=='edit' and x[1]==101 and x[2]==777]
    assert confirm_edits, 'Leave confirmation must actually be edited into the personal card'
    kb=confirm_edits[-1][4]['reply_markup']
    assert any(btn.callback_data==f'leaveyes:{qid}' for row in kb.inline_keyboard for btn in row)
    asyncio.run(mod.do_leave(mock_call(101,f'leaveyes:{qid}'),bot))
    assert db.one('SELECT status FROM participants WHERE queue_id=? AND user_id=?',(qid,101))['status']=='withdrawn'
    latest=[x for x in bot.calls if x[0]=='edit' and x[1]==101][-1][3]
    assert 'не участвуешь' in latest.lower()


def test_invalid_private_queue_link_is_deleted_without_reply(tmp_path, handler_module):
    _, _, _, bot = _fixture(tmp_path, handler_module)
    for text in ('/start wrong', '/start q_123', '/start q_99999999999999999999'):
        asyncio.run(handler_module.start_cmd(_private(101, text), bot))
    assert [call[0] for call in bot.calls] == ['delete'] * 3


def test_invalid_private_position_is_deleted_without_error_or_panel_edit(tmp_path, handler_module):
    db, svc, _, bot = _fixture(tmp_path, handler_module)
    db.exec("INSERT INTO users(id,name,created_at) VALUES(101,'Леонид',?)", (utc_iso(utc_now()),))
    qid = svc.create(-10042, 101, 'Прога', utc_now() + timedelta(days=2), 6)
    svc.signup(qid, 101)
    db.exec("INSERT INTO personal_panels(queue_id,user_id,message_id,view) VALUES(?,101,777,'number')",
            (qid,))

    for text in ('zero', '0', '999'):
        asyncio.run(handler_module.save_exact(_private(101, text), bot))
    assert [call[0] for call in bot.calls] == ['delete'] * 3
    assert db.one('SELECT view FROM personal_panels WHERE queue_id=?', (qid,))['view'] == 'number'

    asyncio.run(handler_module.save_exact(_private(101, '3'), bot))
    assert db.members(qid)[0]['desired'] == 3
    assert [call[0] for call in bot.calls[-2:]] == ['edit', 'delete']


def test_classic_buttons_and_cards_restored(tmp_path, handler_module):
    mod=handler_module
    db,svc,cfg,bot=_fixture(tmp_path,mod)
    db.exec('INSERT INTO users(id,name,created_at) VALUES(?,?,?)',(101,'Леонид',utc_iso(utc_now())))
    qid=svc.create(-10042,101,'Прога',utc_now()+timedelta(days=3),6)
    svc.signup(qid,101)
    from queuebot.presentation import personal_message,group_message
    text,buttons=personal_message(db,qid,101,cfg)
    assert '<blockquote>' not in text
    assert 'Ты в очереди.' in text
    assert any(getattr(x,'style',None) in ('primary','success','danger') for row in buttons.inline_keyboard for x in row)
    group,kb=group_message(db,qid,cfg,'test_bot')
    assert '<blockquote>' not in group
    assert f'<code>№{qid}</code> <b>Очередь: Прога</b>' in group
    assert any(getattr(x,'style',None)=='success' for row in kb.inline_keyboard for x in row)
