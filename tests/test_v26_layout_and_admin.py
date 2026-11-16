"""Requested UX and group-admin restrictions, including real side-effect guards."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from queuebot.config import utc_now, utc_iso
from queuebot.db import Database
from queuebot.service import QueueService
from queuebot.presentation import lesson_label, group_message, personal_message, LESSON_TIMES, ik
from test_handlers_v21 import FakeBot, Settings, handler_module


def test_pair_number_and_weekday_and_unknown_time():
    cfg = Settings()
    assert LESSON_TIMES == ('09:20', '11:10', '13:45', '15:35')
    assert 'четверг, 3 пара' in lesson_label('2026-11-19T10:45:00+00:00', cfg)
    assert 'понедельник, 1 пара' in lesson_label('2026-11-16T06:20:00+00:00', cfg)
    assert 'вне расписания' in lesson_label('2026-11-19T11:20:00+00:00', cfg)


def test_parse_lesson_date_and_time(handler_module):
    handler_module.CFG = Settings()
    parsed = handler_module.parse_lesson_time('19.11.2026 13:45')
    assert parsed.astimezone(ZoneInfo('Europe/Moscow')).strftime('%H:%M') == '13:45'
    assert handler_module.parse_lesson_time('2026-11-19 13:45') == parsed


def test_group_reference_layout_and_colored_buttons(tmp_path):
    cfg = Settings()
    db = Database(str(tmp_path / 'layout.sqlite3'))
    svc = QueueService(db, cfg)
    db.exec('INSERT INTO users(id,name,created_at) VALUES(?,?,?)', (101,'Фёдор',utc_iso(utc_now())))
    lesson = utc_now() + timedelta(days=2)
    qid = svc.create(-10042,101,'ООАиП',lesson,.05)
    svc.signup(qid,101)
    group, kb = group_message(db,qid,cfg,'test_bot')
    assert f'<code>№{qid}</code> <b>Очередь: ООАиП</b>' in group
    assert '🗓 Пара:' in group and '👥 Участников: 1' in group
    assert '⏳ Запись открыта до:' in group
    assert '<b>Записались:</b>\n• Фёдор' in group
    assert kb.inline_keyboard[0][0].style == 'success'
    card, buttons = personal_message(db,qid,101,cfg)
    styles = {btn.callback_data: btn.style for row in buttons.inline_keyboard for btn in row}
    assert styles[f'exact:{qid}'] == 'primary'
    assert styles[f'pref:{qid}:any'] == 'success'
    assert styles[f'leave:{qid}'] == 'danger'


def test_unprivileged_user_cannot_create_even_if_legacy_global_admin(tmp_path, handler_module):
    cfg = Settings(); cfg.admins = {202}
    db = Database(str(tmp_path / 'denied.sqlite3'))
    svc = QueueService(db, cfg)
    handler_module.setup(db,cfg,svc,'test_bot')
    bot = FakeBot(administrators=set())
    when=(utc_now()+timedelta(days=2)).astimezone(cfg.tz).strftime('%d.%m.%Y %H:%M')
    msg=SimpleNamespace(from_user=SimpleNamespace(id=202),
                        chat=SimpleNamespace(id=-10042,type='supergroup'),
                        text=f'/queue Прога {when} 3м',message_id=350,
                        message_thread_id=999,ephemeral_message_id=None)
    asyncio.run(handler_module.create_queue(msg,bot))
    assert not db.one('SELECT * FROM queues')
    assert [call[0] for call in bot.calls] == ['get_member', 'delete']


def test_unprivileged_user_cannot_close_existing_queue(tmp_path,handler_module):
    cfg=Settings(); cfg.admins={202}
    db=Database(str(tmp_path / 'secured.sqlite3'))
    svc=QueueService(db,cfg)
    qid=svc.create(-10042,101,'Прога',utc_now()+timedelta(days=2),.05)
    handler_module.setup(db,cfg,svc,'test_bot')
    bot=FakeBot(administrators=set())
    msg=SimpleNamespace(from_user=SimpleNamespace(id=202),
                        chat=SimpleNamespace(id=-10042,type='supergroup'),
                        text=f'/close_reg {qid}',message_id=222,ephemeral_message_id=None)
    asyncio.run(handler_module.close_reg_cmd(msg,bot))
    assert db.queue(qid)['status']=='registration'
    assert any(c[0]=='delete' for c in bot.calls)


def test_stats_removed_from_menu_and_private_info(handler_module):
    bot=FakeBot()
    asyncio.run(handler_module.register_commands(bot))
    menus=[c[3] for c in bot.calls if c[0]=='set_commands']
    assert all(cmd.command != 'stats' for menu in menus for cmd in menu)
    assert menus[2] == [] and menus[3] == []
    assert '/stats' not in handler_module.INFO
    assert not hasattr(handler_module,'stats_cmd')
    assert not hasattr(handler_module,'stats_panel')


def test_existing_saved_stats_view_falls_back_to_main(tmp_path, handler_module):
    cfg=Settings();db=Database(str(tmp_path/'upgrade.sqlite3'));svc=QueueService(db,cfg)
    db.exec('INSERT INTO users(id,name,created_at) VALUES(?,?,?)',(101,'Федя',utc_iso(utc_now())))
    qid=svc.create(-10042,101,'Прога',utc_now()+timedelta(days=2),.05)
    svc.signup(qid,101)
    db.exec('INSERT INTO personal_panels(queue_id,user_id,message_id,view) VALUES(?,?,?,?)',(qid,101,700,'stats'))
    handler_module.setup(db,cfg,svc,'test_bot')
    bot=FakeBot()
    asyncio.run(handler_module.refresh_panel(bot,qid,101))
    assert db.one('SELECT view FROM personal_panels WHERE queue_id=? AND user_id=?',(qid,101))['view']=='main'
    assert any(c[0]=='edit' and c[2]==700 and 'Ты в очереди' in c[3] for c in bot.calls)
