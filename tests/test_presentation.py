"""Presentation can be checked without network or aiogram installed on the test machine."""
import importlib.util
import sys
from types import ModuleType
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo

if importlib.util.find_spec('aiogram') is None:
    package = ModuleType('aiogram')
    package.__path__ = []
    types = ModuleType('aiogram.types')

    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None, url=None, style=None):
            self.text, self.callback_data, self.url, self.style = text, callback_data, url, style

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    types.InlineKeyboardButton = InlineKeyboardButton
    types.InlineKeyboardMarkup = InlineKeyboardMarkup
    sys.modules['aiogram'] = package
    sys.modules['aiogram.types'] = types

from queuebot.presentation import personal_message, group_message
from queuebot.config import utc_now, utc_iso
from queuebot.db import Database
from queuebot.service import QueueService


@dataclass
class Cfg:
    max_participants: int = 35
    feedback_hours: int = 12
    @property
    def tz(self):
        return ZoneInfo('Europe/Moscow')


def prepare(tmp_path):
    db = Database(str(tmp_path/'db.sqlite3'))
    svc = QueueService(db, Cfg())
    for uid in (1, 2):
        with db.tx() as conn:
            conn.execute('INSERT INTO users(id,name,created_at) VALUES(?,?,?)',
                         (uid,f'Студент {uid}',utc_iso(utc_now())))
    qid = svc.create(-100, 1, 'ОС', utc_now()+timedelta(days=2), 0.05)
    svc.signup(qid, 1)
    svc.signup(qid, 2)
    return db, svc, qid


def buttons(markup):
    return [b.text for row in markup.inline_keyboard for b in row] if markup else []


def test_personal_card_has_only_exact_and_random(tmp_path):
    db, svc, qid = prepare(tmp_path)
    text, kb = personal_message(db,qid,1,Cfg())
    assert any('Конкретный номер' in x for x in buttons(kb))
    assert any('Случайное место' in x for x in buttons(kb))
    assert all('середину' not in x and 'В начало' not in x and 'В конец' not in x for x in buttons(kb))
    assert 'шанс' not in str(kb).lower()
    text, kb = personal_message(db,qid,1,Cfg(),view='number')
    assert 'Напиши число' in text
    assert len(buttons(kb)) == 1


def test_group_never_displays_hash_or_seed(tmp_path):
    db, svc, qid = prepare(tmp_path)
    text, markup = group_message(db,qid,Cfg(),'fake_username')
    assert 'хеш' not in text.lower() and 'seed' not in text.lower()
    assert any(b.url and b.url.endswith(f'q_{qid}') for row in markup.inline_keyboard for b in row)


def test_pending_swap_in_same_card(tmp_path):
    db, svc, qid = prepare(tmp_path)
    db.exec('UPDATE queues SET closes_at=? WHERE id=?',(utc_iso(utc_now()-timedelta(seconds=1)),qid))
    svc.draw(qid)
    swap_id = svc.swap_request(qid,1,2)
    offer, kb = personal_message(db,qid,2,Cfg())
    assert 'предлагает обменяться' in offer
    assert any(b.callback_data == f'swreply:{swap_id}:yes' for row in kb.inline_keyboard for b in row)
    assert 'Обновить моё место' not in offer
    svc.swap_decision(swap_id,2,True)
    latest, kb = personal_message(db,qid,2,Cfg())
    assert any('обмен' in x.lower() for x in buttons(kb))


def test_exit_confirmation_has_no_penalty(tmp_path):
    db, svc, qid = prepare(tmp_path)
    text, kb = personal_message(db,qid,1,Cfg(),view='leave_confirm')
    assert 'Штрафа за выход нет' in text
    assert any('Да, выйти' in x for x in buttons(kb))


def test_group_list_has_no_last_change_after_swap_or_leave(tmp_path):
    db, svc, qid = prepare(tmp_path)
    db.exec('UPDATE queues SET closes_at=? WHERE id=?',(utc_iso(utc_now()-timedelta(seconds=1)),qid))
    svc.draw(qid)
    swap_id = svc.swap_request(qid, 1, 2)
    svc.swap_decision(swap_id, 2, True)
    text, _ = group_message(db, qid, Cfg(), 'fake_username')
    assert 'Последнее изменение' not in text
    assert 'Участники обменялись' not in text
    svc.withdraw(qid, 1)
    text, _ = group_message(db, qid, Cfg(), 'fake_username')
    assert 'Последнее изменение' not in text


def test_completed_queue_is_archived_in_group_without_poll(tmp_path):
    db, svc, qid = prepare(tmp_path)
    db.exec('UPDATE queues SET closes_at=? WHERE id=?', (utc_iso(utc_now()-timedelta(seconds=1)), qid))
    svc.draw(qid)
    svc.finish(qid)
    text, kb = group_message(db, qid, Cfg(), 'fake_username')
    assert 'Завершена' in text
    assert 'Порядок сдачи' in text and 'Студент 1' in text
    assert 'Успел' not in text and 'опрос' not in text.lower()
    assert kb is None
    personal, kb = personal_message(db, qid, 1, Cfg())
    assert 'Опрос' not in personal and kb is None

def test_can_rejoin_with_button_only_before_start(tmp_path):
    db,svc,qid=prepare(tmp_path)
    db.exec('UPDATE queues SET closes_at=? WHERE id=?',(utc_iso(utc_now()-timedelta(seconds=1)),qid))
    svc.draw(qid)
    svc.withdraw(qid,1)
    text,kb=personal_message(db,qid,1,Cfg())
    assert 'в конце текущей очереди' in text
    assert any(x.startswith('↩️ Вернуться') for x in buttons(kb))
    assert 'Место выбирается в личке' not in group_message(db,qid,Cfg(),'fake_username')[0]
