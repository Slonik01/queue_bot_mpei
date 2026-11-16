"""v2.8: exact visual stages, /my, disposable help, roster and one-time reset."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from queuebot import roster

from queuebot.config import utc_iso, utc_now
from queuebot.db import Database
from queuebot.roster import preferred_names, display_name
from queuebot.service import QueueService
from queuebot.presentation import group_message
from test_handlers_v21 import FakeBot, Settings, handler_module


@pytest.fixture(autouse=True)
def example_roster(tmp_path, monkeypatch):
    path = tmp_path / 'roster.csv'
    path.write_text(
        'username,first_name,last_name\n'
        'sample_student_one,Алексей,Тестов\n'
        '@sample_student_two,Мария,Примерова\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(roster, 'ROSTER_PATH', path)
    preferred_names.cache_clear()
    yield
    preferred_names.cache_clear()


def config_fixture(tmp_path):
    db=Database(str(tmp_path/'database.sqlite3'))
    cfg=Settings()
    svc=QueueService(db,cfg)
    for uid, name, username in ((101,'Алексей Т.','sample_student_one'),(202,'Мария','Sample_Student_Two'),
                                (303,'Некто','unknown_user')):
        db.exec('INSERT INTO users(id,name,username,created_at) VALUES(?,?,?,?)',
                (uid,name,username,utc_iso(utc_now())))
    qid=svc.create(-100,101,'ООАиП',utc_now()+timedelta(days=2),.05)
    for uid in (101,202,303): svc.signup(qid,uid)
    return db,svc,cfg,qid


def test_group_three_stages_and_monospaced_number(tmp_path):
    db,svc,cfg,qid=config_fixture(tmp_path)
    # New names will be applied by a fresh user interaction or process restart.
    db.user(SimpleNamespace(id=101,username='SAMPLE_STUDENT_ONE',full_name='Old'),utc_iso(utc_now()))
    db.user(SimpleNamespace(id=202,username='sample_student_two',full_name='Old'),utc_iso(utc_now()))
    text,kb=group_message(db,qid,cfg,'testbot')
    assert f'<code>№{qid}</code> <b>Очередь: ООАиП</b>' in text
    assert '⏳ Запись открыта до:' in text
    assert '• Алексей Тестов' in text and '• Мария Примерова' in text
    assert '• Некто' in text  # unlisted: use Telegram name
    assert 'Порядок сдачи' not in text
    db.exec('UPDATE queues SET closes_at=? WHERE id=?',
            (utc_iso(utc_now()-timedelta(seconds=1)),qid))
    svc.draw(qid)
    text,kb=group_message(db,qid,cfg,'testbot')
    assert '⏳ Запись открыта до:' not in text
    assert 'Порядок сдачи:' in text
    assert 'Завершена' not in text
    assert 'Обмены и выход открыты' not in text
    assert '<code>№' in text
    svc.finish(qid)
    text,kb=group_message(db,qid,cfg,'testbot')
    assert '☑️ <u>Завершена</u>' in text
    assert 'Порядок сдачи:' in text
    assert kb is None


def test_roster_names_and_fallback():
    assert preferred_names() == {
        'sample_student_one': 'Алексей Тестов',
        'sample_student_two': 'Мария Примерова',
    }
    assert display_name('@SAMPLE_STUDENT_ONE', 'Other') == 'Алексей Тестов'
    assert display_name(None, 'Имя из Telegram') == 'Имя из Telegram'
    assert display_name('not_in_list', 'Имя из Telegram') == 'Имя из Telegram'


def test_bot_can_open_database_without_private_roster(tmp_path, monkeypatch):
    monkeypatch.setattr(roster, 'ROSTER_PATH', tmp_path / 'missing.csv')
    db = Database(str(tmp_path / 'without-roster.sqlite3'))
    user = SimpleNamespace(id=101, username='sample_student_one', full_name='Имя из Telegram')
    db.user(user, utc_iso(utc_now()))
    assert db.one('SELECT name FROM users WHERE id=101')['name'] == 'Имя из Telegram'


def test_reset_existing_stats_once_and_preserves_queues(tmp_path):
    db,svc,cfg,qid=config_fixture(tmp_path)
    db.exec('UPDATE queues SET closes_at=? WHERE id=?',(utc_iso(utc_now()-timedelta(seconds=1)),qid))
    svc.draw(qid)
    before=db.members(qid)
    assert db.all('SELECT * FROM fairness')
    db.exec('DELETE FROM app_metadata WHERE key=?',('statistics_reset_v28',))
    db.exec('UPDATE users SET name=? WHERE id=?',('Алексей',101))
    Database(db.path) # simulates first boot of updated version
    after=Database(db.path)
    assert not after.all('SELECT * FROM fairness')
    assert not after.all('SELECT * FROM feedback')
    assert {m['user_id']:m['current_position'] for m in before} == {
        m['user_id']:m['current_position'] for m in after.members(qid)
    }
    assert all(m['expected_satisfaction'] is None and m['balance_before'] is None
               for m in after.members(qid))
    assert after.one('SELECT name FROM users WHERE id=101')['name']=='Алексей Тестов'
    assert after.one('SELECT COUNT(*) AS n FROM app_metadata WHERE key=?',('statistics_reset_v28',))['n']==1
    sub=after.queue(qid)['subject_id']
    after.exec('INSERT INTO fairness(subject_id,user_id,balance) VALUES(?,?,?)', (sub,101,18))
    Database(db.path) # every subsequent restart MUST retain new statistics
    assert after.one('SELECT balance FROM fairness WHERE subject_id=? AND user_id=?',(sub,101))['balance']==18
    # Cancellation of an old pre-reset draw must never undo the new balance.
    svc.cancel(qid)
    assert after.one('SELECT balance FROM fairness WHERE subject_id=? AND user_id=?',(sub,101))['balance']==18


def private_message(uid, text):
    return SimpleNamespace(from_user=SimpleNamespace(id=uid,username='sample_student_one',full_name='Алексей'),
                           chat=SimpleNamespace(id=uid,type='private'),
                           text=text,message_id=775,ephemeral_message_id=None)


def test_my_deletes_old_cards_and_recreates_only_joined_queues(tmp_path,handler_module):
    db,svc,cfg,qid=config_fixture(tmp_path)
    other=svc.create(-100,101,'Математика',utc_now()+timedelta(days=3),.05)
    svc.signup(other,101)
    db.exec('INSERT INTO personal_panels(queue_id,user_id,message_id,view) VALUES(?,?,?,?)',
            (qid,101,901,'main'))
    db.exec('INSERT INTO personal_panels(queue_id,user_id,message_id,view) VALUES(?,?,?,?)',
            (other,101,902,'main'))
    handler_module.setup(db,cfg,svc,'testbot')
    bot=FakeBot()
    asyncio.run(handler_module.my_cmd(private_message(101,'/my'),bot))
    deleted={(c[1],c[2]) for c in bot.calls if c[0]=='delete'}
    assert (101,901) in deleted and (101,902) in deleted
    sends=[c for c in bot.calls if c[0]=='send' and c[1]==101]
    assert len(sends)==2
    assert all(c[4].get('reply_markup') is not None for c in sends)
    assert len(db.all('SELECT * FROM personal_panels WHERE user_id=101'))==2
    assert all(r['message_id']==12345 for r in db.all('SELECT * FROM personal_panels WHERE user_id=101'))


def test_info_help_always_new_with_dismiss_button(tmp_path,handler_module):
    db,svc,cfg,qid=config_fixture(tmp_path)
    db.exec('INSERT INTO personal_panels(queue_id,user_id,message_id) VALUES(?,?,?)',(qid,101,901))
    handler_module.setup(db,cfg,svc,'testbot')
    bot=FakeBot()
    for cmd in ('/info','/help'):
        asyncio.run(handler_module.help_cmd(private_message(101,cmd),bot))
    sends=[c for c in bot.calls if c[0]=='send']
    assert len(sends)==2
    for c in sends:
        assert any(b.text=='✅ Понятно' for row in c[4]['reply_markup'].inline_keyboard for b in row)
    assert not any(c[0]=='edit' and c[2]==901 for c in bot.calls)
    # The dismiss callback deletes that specific help message.
    answers=[]
    async def answer(*args,**kwargs): answers.append((args,kwargs))
    call=SimpleNamespace(from_user=SimpleNamespace(id=101),data='dismiss:101',answer=answer,
                         message=SimpleNamespace(chat=SimpleNamespace(id=101),message_id=999))
    asyncio.run(handler_module.dismiss_help(call,bot))
    assert any(c[0]=='delete' and c[1]==101 and c[2]==999 for c in bot.calls)


def test_group_help_rejects_non_admin(handler_module):
    handler_module.setup(None,None,None,'testbot')
    bot=FakeBot(administrators={101})
    message=SimpleNamespace(from_user=SimpleNamespace(id=303),
                            chat=SimpleNamespace(id=-100,type='supergroup'),
                            text='/help',message_id=808,ephemeral_message_id=None)
    asyncio.run(handler_module.discard_group_command(message,bot))
    assert any(c[0]=='delete' and c[1]==-100 and c[2]==808 for c in bot.calls)
    assert not any(c[0]=='send' for c in bot.calls)
