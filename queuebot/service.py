"""Business logic with database transactions; independent of Telegram handlers."""
import json
import random
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from queuebot.config import utc_iso, utc_now
from queuebot.optimizer import Assignment, Candidate, satisfaction


class DomainError(Exception):
    pass


class QueueService:
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg

    def create(self, chat_id, creator_id, title, lesson_at, hours, message_thread_id=None, *,
               use_preferences=True):
        title = title.strip()
        if not 1 <= len(title) <= 255:
            raise DomainError('Название предмета должно содержать 1–255 символов.')
        if not 0.05 <= hours <= 168:
            raise DomainError('Время записи: от 3 минут до 7 дней.')
        now = utc_now()
        if lesson_at <= now or lesson_at <= now + timedelta(hours=hours):
            raise DomainError('Пара должна начинаться после закрытия регистрации.')
        seed = secrets.token_hex(16)
        digest = ""  # Retain legacy SQLite column; no hash is shown or needed.
        with self.db.tx() as conn:
            if message_thread_id is None:
                subject = conn.execute("""INSERT INTO subjects(chat_id,title) VALUES(?,?)
                    ON CONFLICT(chat_id,title) WHERE message_thread_id IS NULL
                    DO UPDATE SET title=subjects.title RETURNING id""", (chat_id, title)).fetchone()
            else:
                subject = conn.execute("""INSERT INTO subjects(chat_id,title,message_thread_id) VALUES(?,?,?)
                    ON CONFLICT(chat_id,message_thread_id) WHERE message_thread_id IS NOT NULL
                    DO UPDATE SET title=excluded.title RETURNING id""",
                    (chat_id, title, message_thread_id)).fetchone()
            try:
                cur = conn.execute('''INSERT INTO queues(subject_id,chat_id,creator_id,lesson_at,
                    closes_at,status,seed,seed_hash,created_at,message_thread_id,title,use_preferences)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (subject['id'],chat_id,creator_id,utc_iso(lesson_at),
                     utc_iso(now+timedelta(hours=hours)),'registration',seed,digest,utc_iso(now),
                     message_thread_id,title,int(use_preferences)))
            except sqlite3.IntegrityError as exc:
                if str(exc) == 'duplicate_queue':
                    raise DomainError('Очередь на эту дату и предмет уже существует.') from exc
                raise
            return cur.lastrowid

    def signup(self, qid, uid):
        now = utc_iso(utc_now())
        with self.db.tx() as conn:
            q = conn.execute('SELECT * FROM queues WHERE id=?', (qid,)).fetchone()
            if not q:
                raise DomainError('Очередь не найдена.')
            if q['status'] != 'registration' or now >= q['closes_at']:
                raise DomainError('Регистрация уже закрыта.')
            prev = conn.execute('SELECT * FROM participants WHERE queue_id=? AND user_id=?',
                                (qid,uid)).fetchone()
            if prev and prev['status'] == 'joined':
                return False
            count = conn.execute('SELECT COUNT(*) FROM participants WHERE queue_id=? AND status=?',
                                 (qid,'joined')).fetchone()[0]
            if count >= self.cfg.max_participants:
                raise DomainError('Достигнут лимит участников.')
            conn.execute('''INSERT INTO participants(queue_id,user_id,pref_mode,joined_at,status)
                VALUES(?,?,?,?,?) ON CONFLICT(queue_id,user_id) DO UPDATE SET
                status='joined', withdrawn_at=NULL, joined_at=excluded.joined_at''',
                (qid,uid,'any',now,'joined'))
            self.db.event(conn,qid,uid,'join','',now)
            self.db.bump(conn,qid)
            return True

    def rejoin(self, qid, uid):
        """A withdrawn member may re-enter an already drawn queue at its tail,
        but only before the lesson starts. Never rerun the draw or award a
        fresh history advantage for rejoining.
        """
        now = utc_iso(utc_now())
        with self.db.tx() as conn:
            q = conn.execute('SELECT * FROM queues WHERE id=?', (qid,)).fetchone()
            if not q:
                raise DomainError('Очередь не найдена.')
            if q['status'] == 'registration':
                # Reuse the normal signup rules without nested transactions.
                raise DomainError('Для регистрации используй обычную запись.')
            if q['status'] != 'active' or now >= q['lesson_at']:
                raise DomainError('Вернуться можно только до начала пары.')
            p = conn.execute('SELECT status FROM participants WHERE queue_id=? AND user_id=?',
                             (qid, uid)).fetchone()
            if not p or p['status'] != 'withdrawn':
                raise DomainError('Вернуться может только вышедший из этой очереди участник.')
            count = conn.execute("SELECT COUNT(*) FROM participants WHERE queue_id=? AND status='joined'",
                                 (qid,)).fetchone()[0]
            if count >= self.cfg.max_participants:
                raise DomainError('Достигнут лимит участников.')
            tail = conn.execute("SELECT COALESCE(MAX(current_position),0) FROM participants WHERE queue_id=? AND status='joined'",
                                (qid,)).fetchone()[0]
            conn.execute("""UPDATE participants SET status='joined', withdrawn_at=NULL,
                current_position=?,priority=0 WHERE queue_id=? AND user_id=? AND status='withdrawn'""",
                (tail + 1, qid, uid))
            self.db.event(conn, qid, uid, 'rejoin', f'position={tail + 1}', now)
            self.db.bump(conn, qid)
            return tail + 1

    def preference(self, qid, uid, mode, desired=None):
        if mode not in ('exact','any'):
            raise DomainError('Неизвестный тип пожелания.')
        if mode == 'exact' and (not isinstance(desired,int) or
                                desired < 1 or desired > self.cfg.max_participants):
            raise DomainError(f'Номер должен быть от 1 до {self.cfg.max_participants}.')
        with self.db.tx() as conn:
            q = conn.execute('SELECT * FROM queues WHERE id=?', (qid,)).fetchone()
            if not q or q['status'] != 'registration' or utc_iso(utc_now()) >= q['closes_at']:
                raise DomainError('Пожелания уже нельзя менять.')
            if not q['use_preferences']:
                raise DomainError('В этой очереди места распределяются случайно.')
            changed = conn.execute('''UPDATE participants SET pref_mode=?,desired=?
                WHERE queue_id=? AND user_id=? AND status='joined' ''',
                (mode,desired if mode=='exact' else None,qid,uid)).rowcount
            if not changed:
                raise DomainError('Сначала запишись в очередь.')
            self.db.bump(conn,qid)

    def candidates(self, qid):
        """No guaranteed front positions: only preferences and fairness history."""
        q = self.db.queue(qid)
        result = []
        for p in self.db.members(qid):
            f = self.db.one('SELECT balance FROM fairness WHERE subject_id=? AND user_id=?',
                            (q['subject_id'], p['user_id']))
            result.append(Candidate(user_id=p['user_id'], mode=p['pref_mode'],
                                    desired=p['desired'], balance=f['balance'] if f else 0,
                                    priority=False, priority_order=0,
                                    fairness_existed=bool(f)))
        return result

    def draw(self, qid):
        q = self.db.queue(qid)
        if not q or q['status'] != 'registration':
            raise DomainError('Эта очередь уже обработана или отменена.')
        if utc_iso(utc_now()) < q['closes_at']:
            raise DomainError('Регистрация ещё идёт.')
        seed = int(q['seed'], 16)
        if q['use_preferences']:
            people = self.candidates(qid)
            assignment = Assignment(people)
            positions = assignment.draw(seed)
            baseline = assignment.baseline_expected(seed ^ 0x5A17F00D, samples=40) if people else {}
            details = {'seed': q['seed'], 'max': assignment.max_utility, 'floor': assignment.floor}
        else:
            users = [member['user_id'] for member in self.db.members(qid)]
            random.Random(seed).shuffle(users)
            positions = {uid: position for position, uid in enumerate(users, start=1)}
            details = {'seed': q['seed'], 'mode': 'random'}
        now = utc_iso(utc_now())
        with self.db.tx() as conn:
            stage = conn.execute('SELECT status,revision FROM queues WHERE id=?', (qid,)).fetchone()
            if stage['status'] != 'registration' or stage['revision'] != q['revision']:
                raise DomainError('Состав очереди изменился. Пересчитайте жеребьёвку.')
            if q['use_preferences']:
                self._record_preference_draw(conn, q, people, positions, baseline, now)
            else:
                conn.executemany("""UPDATE participants SET original_position=?,current_position=?
                    WHERE queue_id=? AND user_id=? AND status='joined'""",
                    ((pos, pos, qid, uid) for uid, pos in positions.items()))
            conn.execute("UPDATE queues SET status='active',revision=revision+1 WHERE id=?",(qid,))
            self.db.event(conn, qid, None, 'draw', json.dumps(details), now)
        return positions

    def _record_preference_draw(self, conn, queue, people, positions, baseline, now):
        for person in people:
            uid = person.user_id
            pos = positions[uid]
            expected = baseline[uid]
            utility = satisfaction(person, pos, len(people))
            conn.execute('''UPDATE participants SET original_position=?,current_position=?,
                original_satisfaction=?,expected_satisfaction=?,priority=?,
                balance_before=?,penalties_before=?,fairness_existed=?
                WHERE queue_id=? AND user_id=? AND status='joined' ''',
                (pos, pos, utility, expected, int(person.priority),
                 person.balance, 0, int(person.fairness_existed), queue['id'], uid))
            new_balance = max(-100, min(100, round(0.9 * person.balance + expected - utility, 3)))
            conn.execute('''INSERT INTO fairness(subject_id,user_id,balance,exit_penalties,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(subject_id,user_id) DO UPDATE SET
                balance=excluded.balance,exit_penalties=0,updated_at=excluded.updated_at''',
                (queue['subject_id'], uid, new_balance, 0, now))

    def withdraw(self, qid, uid):
        now = utc_iso(utc_now())
        with self.db.tx() as conn:
            q = conn.execute('SELECT * FROM queues WHERE id=?', (qid,)).fetchone()
            if not q or q['status'] not in ('registration','active'):
                raise DomainError('Из этой очереди уже нельзя выйти.')
            if q['status']=='registration' and now >= q['closes_at']:
                raise DomainError('Регистрация закрыта; дождись жеребьёвки.')
            p = conn.execute('SELECT * FROM participants WHERE queue_id=? AND user_id=?',
                             (qid,uid)).fetchone()
            if not p or p['status']!='joined':
                raise DomainError('Тебя нет в этой очереди.')
            conn.execute("UPDATE participants SET status='withdrawn',withdrawn_at=?,current_position=NULL WHERE queue_id=? AND user_id=?",(now,qid,uid))
            if q['status']=='active':
                conn.execute('''UPDATE participants SET current_position=current_position-1
                    WHERE queue_id=? AND status='joined' AND current_position>?''',
                    (qid,p['current_position']))
            conn.execute("UPDATE swaps SET status='cancelled',handled_at=? WHERE queue_id=? AND status='pending' AND (from_user=? OR to_user=?)",(now,qid,uid,uid))
            self.db.event(conn,qid,uid,'leave',f"old_position={p['current_position']}",now)
            self.db.bump(conn,qid)
            return q['status']=='active'

    def swap_request(self,qid,from_uid,to_uid):
        if from_uid==to_uid:
            raise DomainError('Нельзя обменяться с самим собой.')
        now = utc_now()
        with self.db.tx() as conn:
            q = conn.execute('SELECT status FROM queues WHERE id=?',(qid,)).fetchone()
            if not q or q['status']!='active':
                raise DomainError('Обмены сейчас закрыты.')
            for uid in (from_uid,to_uid):
                if not conn.execute('''SELECT 1 FROM participants WHERE queue_id=? AND user_id=?
                    AND status='joined' AND current_position IS NOT NULL''',(qid,uid)).fetchone():
                    raise DomainError('Один из участников уже выбыл из очереди.')
                busy = conn.execute("""SELECT 1 FROM swaps WHERE queue_id=? AND status='pending'
                    AND expires_at>? AND (from_user=? OR to_user=?)""",(qid,utc_iso(now),uid,uid)).fetchone()
                if busy:
                    raise DomainError('У одного из участников уже есть ожидающий обмен.')
            cur=conn.execute('''INSERT INTO swaps(queue_id,from_user,to_user,status,created_at,expires_at)
                VALUES(?,?,?,'pending',?,?)''',
                (qid,from_uid,to_uid,utc_iso(now),utc_iso(now+timedelta(minutes=5))))
            return cur.lastrowid

    def swap_decision(self,swap_id,uid,accept):
        now = utc_iso(utc_now())
        with self.db.tx() as conn:
            sw = conn.execute('SELECT * FROM swaps WHERE id=?',(swap_id,)).fetchone()
            if not sw or sw['to_user'] != uid:
                raise DomainError('Это предложение не адресовано тебе.')
            if sw['status'] == 'expired':
                raise DomainError('Срок предложения истёк.')
            if sw['status'] != 'pending':
                raise DomainError('Предложение уже обработано.')
            if now < sw['expires_at']:
                qid, initiator, target = sw['queue_id'], sw['from_user'], sw['to_user']
                q = conn.execute('SELECT status FROM queues WHERE id=?', (qid,)).fetchone()
                if not q or q['status'] != 'active':
                    raise DomainError('Обмены уже закрыты.')
                if accept:
                    positions = dict(conn.execute("""SELECT user_id,current_position FROM participants
                        WHERE queue_id=? AND user_id IN (?,?) AND status='joined'""",
                        (qid, initiator, target)).fetchall())
                    first, second = positions.get(initiator), positions.get(target)
                    if first is None or second is None:
                        raise DomainError('Один из участников выбыл из очереди.')
                    conn.executemany(
                        'UPDATE participants SET current_position=? WHERE queue_id=? AND user_id=?',
                        [(second, qid, initiator), (first, qid, target)],
                    )
                    self.db.event(conn, qid, uid, 'swap', f'{initiator}:{first} <-> {target}:{second}', now)
                    self.db.bump(conn, qid)
                conn.execute('UPDATE swaps SET status=?,handled_at=? WHERE id=?',
                             ('accepted' if accept else 'rejected', now, swap_id))
                return qid, initiator, target
            conn.execute("UPDATE swaps SET status='expired',handled_at=? WHERE id=?", (now, swap_id))
        raise DomainError('Срок предложения истёк.')

    def finish(self, qid):
        """Close the lesson with no feedback stage or priority carry-over.

        The deadline worker calls this at lesson_at + 100 min. An admin may
        call it manually after the lesson has started. SQLite transition is
        atomic; personal-card cleanup is retryable and performed separately.
        """
        now = utc_iso(utc_now())
        with self.db.tx() as conn:
            q = conn.execute('SELECT status FROM queues WHERE id=?', (qid,)).fetchone()
            if not q or q['status'] != 'active':
                raise DomainError('Очередь не находится в активном состоянии.')
            conn.execute("UPDATE queues SET status='closed',feedback_deadline=NULL,revision=revision+1 WHERE id=?", (qid,))
            conn.execute("UPDATE swaps SET status='cancelled',handled_at=? WHERE queue_id=? AND status='pending'", (now,qid))
            self.db.event(conn,qid,None,'finish','Завершена без опроса',now)

    def cancel(self,qid):
        now=utc_iso(utc_now())
        with self.db.tx() as conn:
            q=conn.execute('SELECT * FROM queues WHERE id=?',(qid,)).fetchone()
            if not q or q['status'] not in ('registration','active'):
                raise DomainError('Нельзя отменить эту очередь.')
            if q['status']!='registration':
                later=conn.execute("""SELECT 1 FROM queues WHERE subject_id=? AND lesson_at>?
                    AND status IN ('active','closed') LIMIT 1""",
                    (q['subject_id'],q['lesson_at'])).fetchone()
                if later:
                    raise DomainError('Уже проведена следующая жеребьёвка по предмету; старую отменить нельзя.')
                previous=conn.execute('SELECT * FROM participants WHERE queue_id=? AND original_position IS NOT NULL',
                                      (qid,)).fetchall()
                for p in previous:
                    # Do not undo the one-time statistics reset if an old
                    # pre-migration queue is cancelled later.
                    if p['fairness_existed'] is None:
                        continue
                    if not p['fairness_existed']:
                        conn.execute('DELETE FROM fairness WHERE subject_id=? AND user_id=?',
                                     (q['subject_id'],p['user_id']))
                    else:
                        conn.execute('UPDATE fairness SET balance=?,exit_penalties=0,updated_at=?\n                            WHERE subject_id=? AND user_id=?',
                            (p['balance_before'],now,q['subject_id'],p['user_id']))
            conn.execute("UPDATE queues SET status='cancelled',revision=revision+1 WHERE id=?",(qid,))
            conn.execute("UPDATE swaps SET status='cancelled',handled_at=? WHERE queue_id=? AND status='pending'",(now,qid))
            self.db.event(conn,qid,None,'cancel','',now)
