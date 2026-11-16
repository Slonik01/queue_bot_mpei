from dataclasses import dataclass
from datetime import timedelta
import sqlite3
from queuebot.db import Database
from queuebot.config import utc_now, utc_iso
from queuebot.service import QueueService, DomainError


@dataclass
class Cfg:
    max_participants:int=35
    feedback_hours:int=12


def setup(tmp_path):
    db=Database(str(tmp_path/'test.sqlite3'))
    svc=QueueService(db,Cfg())
    return db,svc


def make_user(db,uid):
    with db.tx() as c:
        c.execute('INSERT INTO users(id,name,created_at) VALUES(?,?,?)',(uid,'Student '+str(uid),utc_iso(utc_now())))


def create_future(svc,title='Math',days=1):
    return svc.create(-100,42,title,utc_now()+timedelta(days=days),.05)


def close_and_draw(db,svc,qid):
    db.exec('UPDATE queues SET closes_at=? WHERE id=?',(utc_iso(utc_now()-timedelta(seconds=2)),qid))
    svc.draw(qid)


def test_draw_then_swap_exit(tmp_path):
    db,svc=setup(tmp_path)
    for x in range(1,5): make_user(db,x)
    qid=create_future(svc)
    for uid in range(1,5):
        svc.signup(qid,uid)
        svc.preference(qid,uid,'exact',uid)
    close_and_draw(db,svc,qid)
    positions={m['user_id']:m['current_position'] for m in db.members(qid)}
    sid=svc.swap_request(qid,1,2)
    svc.swap_decision(sid,2,True)
    assert db.one('SELECT current_position FROM participants WHERE queue_id=? AND user_id=1',(qid,))['current_position']==positions[2]
    assert db.one('SELECT current_position FROM participants WHERE queue_id=? AND user_id=2',(qid,))['current_position']==positions[1]
    svc.withdraw(qid,2)
    pos=[p['current_position'] for p in db.members(qid)]
    assert sorted(pos)==[1,2,3]
    assert db.one('SELECT exit_penalties FROM fairness WHERE subject_id=1 AND user_id=2')['exit_penalties']==0


def test_no_carry_over_after_previous_feedback(tmp_path):
    db, svc = setup(tmp_path)
    for uid in (1, 2, 3): make_user(db, uid)
    first = create_future(svc, days=1)
    for uid in (1, 2, 3): svc.signup(first, uid)
    close_and_draw(db, svc, first)
    # Historical data from v2.6 must never give guaranteed positions.
    db.exec("INSERT INTO feedback(queue_id,user_id,result,updated_at) VALUES(?,?,?,?)",
            (first, 2, 'no', utc_iso(utc_now())))
    svc.finish(first)
    second = create_future(svc, days=2)
    for uid in (1, 2, 3): svc.signup(second, uid)
    assert not any(p.priority for p in svc.candidates(second))
    close_and_draw(db, svc, second)
    assert all(m['priority'] == 0 for m in db.members(second))

def test_can_rejoin_after_leaving_during_registration(tmp_path):
    db,svc=setup(tmp_path)
    make_user(db,1)
    q=create_future(svc)
    assert svc.signup(q,1)
    assert not svc.signup(q,1)
    svc.withdraw(q,1)
    assert svc.signup(q,1)
    assert len(db.members(q))==1


def test_cancel_after_draw_restores_old_fairness(tmp_path):
    db,svc=setup(tmp_path)
    for uid in (1,2):make_user(db,uid)
    q=create_future(svc)
    for uid in (1,2):
        svc.signup(q,uid)
        svc.preference(q,uid,'exact',1)
    with db.tx() as conn:
        conn.execute('INSERT INTO fairness(subject_id,user_id,balance,exit_penalties) VALUES(1,1,70,0)')
    close_and_draw(db,svc,q)
    svc.withdraw(q,1)
    svc.cancel(q)
    f=db.one('SELECT * FROM fairness WHERE subject_id=1 AND user_id=1')
    assert f['balance']==70 and f['exit_penalties']==0
    assert db.one('SELECT * FROM fairness WHERE subject_id=1 AND user_id=2') is None
    assert db.queue(q)['status']=='cancelled'


def test_no_special_treatment_for_skipped_lessons(tmp_path):
    db, svc = setup(tmp_path)
    for uid in (1, 2): make_user(db, uid)
    first = create_future(svc, days=1)
    svc.signup(first, 1)
    close_and_draw(db, svc, first)
    svc.finish(first)
    second = create_future(svc, days=2)
    svc.signup(second, 2)
    close_and_draw(db, svc, second)
    svc.finish(second)
    third = create_future(svc, days=3)
    svc.signup(third, 1)
    assert not svc.candidates(third)[0].priority

def test_cannot_make_second_pending_swap(tmp_path):
    db,svc=setup(tmp_path)
    for uid in (1,2,3):make_user(db,uid)
    q=create_future(svc)
    for uid in (1,2,3):svc.signup(q,uid)
    close_and_draw(db,svc,q)
    first=svc.swap_request(q,1,2)
    try:
        svc.swap_request(q,3,2)
        assert False, 'Must refuse concurrent pending swap'
    except DomainError:
        pass
    svc.swap_decision(first,2,False)
    assert svc.swap_request(q,3,2)>first


def test_cancelled_lesson_has_no_future_priority(tmp_path):
    db, svc = setup(tmp_path)
    make_user(db, 1)
    old = create_future(svc, days=1)
    svc.signup(old, 1)
    close_and_draw(db, svc, old)
    svc.finish(old)
    cancelled = create_future(svc, days=2)
    svc.cancel(cancelled)
    future = create_future(svc, days=3)
    svc.signup(future, 1)
    assert not svc.candidates(future)[0].priority

def test_exit_never_changes_future_history_weight(tmp_path):
    db, svc = setup(tmp_path)
    for uid in (1, 2, 3):
        make_user(db, uid)
    queue = create_future(svc)
    for uid in (1, 2, 3):
        svc.signup(queue, uid)
        svc.preference(queue, uid, 'exact', 1)
    close_and_draw(db, svc, queue)
    before = db.one('SELECT balance, exit_penalties FROM fairness WHERE subject_id=1 AND user_id=2')
    svc.withdraw(queue, 2)
    after = db.one('SELECT balance, exit_penalties FROM fairness WHERE subject_id=1 AND user_id=2')
    assert dict(before)==dict(after)


def test_only_exact_or_random_preference(tmp_path):
    db, svc = setup(tmp_path)
    make_user(db, 1)
    queue = create_future(svc)
    svc.signup(queue, 1)
    for mode in ('start','middle','end'):
        import pytest
        with pytest.raises(DomainError):
            svc.preference(queue, 1, mode)
    svc.preference(queue, 1, 'any')
    svc.preference(queue, 1, 'exact', 7)
    assert db.one('SELECT desired FROM participants WHERE queue_id=? AND user_id=1', (queue,))['desired']==7


def test_migration_keeps_history_and_clears_legacy_penalty(tmp_path):
    path = str(tmp_path / 'old.sqlite3')
    db = Database(path)
    make_user(db, 1)
    with db.tx() as conn:
        conn.execute("INSERT INTO subjects(chat_id,title) VALUES(-100,'Math')")
        conn.execute('INSERT INTO fairness(subject_id,user_id,balance,exit_penalties) VALUES(1,1,42,3)')
    upgraded = Database(path)
    assert upgraded.one('SELECT balance,exit_penalties FROM fairness WHERE user_id=1')['balance']==42
    assert upgraded.one('SELECT balance,exit_penalties FROM fairness WHERE user_id=1')['exit_penalties']==0
    tables={r[0] for r in upgraded.all('SELECT name FROM sqlite_master WHERE type=?',('table',))}
    assert 'personal_panels' in tables


def test_rejoin_active_queue_at_tail_before_lesson(tmp_path):
    db,svc=setup(tmp_path)
    for uid in (1,2,3,4): make_user(db,uid)
    q=create_future(svc)
    for uid in (1,2,3,4): svc.signup(q,uid)
    close_and_draw(db,svc,q)
    person = db.one('SELECT user_id FROM participants WHERE queue_id=? AND current_position=2',(q,))['user_id']
    svc.withdraw(q,person)
    assert sorted(m['current_position'] for m in db.members(q))==[1,2,3]
    newpos=svc.rejoin(q,person)
    assert newpos==4
    assert db.one('SELECT current_position FROM participants WHERE queue_id=? AND user_id=?',(q,person))['current_position']==4
    assert sorted(m['current_position'] for m in db.members(q))==[1,2,3,4]
    import pytest
    with pytest.raises(DomainError):
        svc.rejoin(q,person)


def test_rejoin_requires_prior_membership_and_before_lesson(tmp_path):
    db,svc=setup(tmp_path)
    for uid in (1,2): make_user(db,uid)
    q=create_future(svc)
    svc.signup(q,1)
    close_and_draw(db,svc,q)
    import pytest
    with pytest.raises(DomainError):
        svc.rejoin(q,2)
    svc.withdraw(q,1)
    db.exec('UPDATE queues SET lesson_at=? WHERE id=?',(utc_iso(utc_now()-timedelta(seconds=1)),q))
    with pytest.raises(DomainError):
        svc.rejoin(q,1)


def test_topic_id_migrates_and_persists(tmp_path):
    path=str(tmp_path/'topics.sqlite3')
    db=Database(path)
    with db.tx() as c:
        c.execute('INSERT INTO users(id,name,created_at) VALUES(?,?,?)',(42,'Test',utc_iso(utc_now())))
    svc=QueueService(db,Cfg())
    q=svc.create(-100,42,'Теория',utc_now()+timedelta(days=2),.05,message_thread_id=198)
    assert db.queue(q)['message_thread_id']==198
    reloaded=Database(path)
    assert reloaded.queue(q)['message_thread_id']==198


def test_finish_closes_without_survey(tmp_path):
    db, svc = setup(tmp_path)
    make_user(db, 1)
    q = create_future(svc)
    svc.signup(q, 1)
    close_and_draw(db, svc, q)
    svc.finish(q)
    assert db.queue(q)['status'] == 'closed'
    assert db.one('SELECT * FROM feedback WHERE queue_id=?', (q,)) is None
    import pytest
    with pytest.raises(DomainError):
        svc.finish(q)

def test_finish_cancels_pending_swaps(tmp_path):
    db, svc = setup(tmp_path)
    for uid in (1, 2): make_user(db, uid)
    q = create_future(svc)
    svc.signup(q, 1)
    svc.signup(q, 2)
    close_and_draw(db, svc, q)
    sid = svc.swap_request(q, 1, 2)
    svc.finish(q)
    assert db.one('SELECT status FROM swaps WHERE id=?', (sid,))['status'] == 'cancelled'
    import pytest
    with pytest.raises(DomainError):
        svc.swap_request(q, 2, 1)

def test_v21_database_adds_forum_thread_column(tmp_path):
    path=str(tmp_path/'v21.sqlite3')
    import sqlite3
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE queues (
            id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL, creator_id INTEGER NOT NULL,
            lesson_at TEXT NOT NULL, closes_at TEXT NOT NULL,
            feedback_deadline TEXT, status TEXT NOT NULL,
            group_message_id INTEGER, seed TEXT NOT NULL, seed_hash TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            UNIQUE(subject_id,lesson_at))""")
    db=Database(path)
    names={row['name'] for row in db.all('PRAGMA table_info(queues)')}
    assert 'message_thread_id' in names
    Database(path)  # Migration must also be idempotent.
