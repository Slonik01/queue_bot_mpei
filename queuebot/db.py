"""SQLite persistence. Short BEGIN IMMEDIATE transactions protect concurrent callbacks."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from queuebot.roster import preferred_names, display_name


SUBJECT_COLUMNS = """
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
 title TEXT NOT NULL COLLATE NOCASE, message_thread_id INTEGER
"""

QUEUE_COLUMNS = """
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 subject_id INTEGER NOT NULL REFERENCES subjects(id),
 chat_id INTEGER NOT NULL, creator_id INTEGER NOT NULL,
 lesson_at TEXT NOT NULL, closes_at TEXT NOT NULL,
 feedback_deadline TEXT,
 status TEXT NOT NULL CHECK(status IN ('registration','active','feedback','closed','cancelled')),
 group_message_id INTEGER, message_thread_id INTEGER, seed TEXT NOT NULL, seed_hash TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, title TEXT NOT NULL,
 group_synced_revision INTEGER NOT NULL DEFAULT -1, group_retry_at TEXT,
 use_preferences INTEGER NOT NULL DEFAULT 1 CHECK(use_preferences IN (0,1))
"""

SCHEMA = f"""
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS app_metadata (
 key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subjects ({SUBJECT_COLUMNS});
CREATE TABLE IF NOT EXISTS queues ({QUEUE_COLUMNS});
CREATE TABLE IF NOT EXISTS participants (
 queue_id INTEGER NOT NULL REFERENCES queues(id),
 user_id INTEGER NOT NULL REFERENCES users(id),
 pref_mode TEXT NOT NULL DEFAULT 'any', desired INTEGER,
 original_position INTEGER, current_position INTEGER,
 original_satisfaction INTEGER, expected_satisfaction REAL,
 balance_before REAL, penalties_before INTEGER, fairness_existed INTEGER,
 priority INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'joined' CHECK(status IN ('joined','withdrawn')),
 joined_at TEXT NOT NULL, withdrawn_at TEXT,
 PRIMARY KEY(queue_id, user_id)
);
CREATE TABLE IF NOT EXISTS fairness (
 subject_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 balance REAL NOT NULL DEFAULT 0,
 exit_penalties INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT,
 PRIMARY KEY(subject_id, user_id)
);
CREATE TABLE IF NOT EXISTS feedback (
 queue_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 result TEXT NOT NULL CHECK(result IN ('yes','no','absent')),
 updated_at TEXT NOT NULL, reviewed_by INTEGER,
 PRIMARY KEY(queue_id, user_id)
);
CREATE TABLE IF NOT EXISTS swaps (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 queue_id INTEGER NOT NULL, from_user INTEGER NOT NULL, to_user INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','cancelled','expired')),
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, handled_at TEXT
);
CREATE TABLE IF NOT EXISTS personal_panels (
 queue_id INTEGER NOT NULL REFERENCES queues(id),
 user_id INTEGER NOT NULL REFERENCES users(id),
 message_id INTEGER NOT NULL,
 view TEXT NOT NULL DEFAULT 'main',
 PRIMARY KEY(queue_id, user_id)
);
CREATE TABLE IF NOT EXISTS forum_topics (
 chat_id INTEGER NOT NULL, message_thread_id INTEGER NOT NULL,
 title TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(chat_id, message_thread_id)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, queue_id INTEGER NOT NULL,
 actor_id INTEGER, kind TEXT NOT NULL, details TEXT,
 occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_participants_queue ON participants(queue_id, status, current_position);
CREATE INDEX IF NOT EXISTS idx_swaps_pending ON swaps(queue_id, status);
"""

QUEUE_INDEXES = (
    'CREATE INDEX IF NOT EXISTS idx_queues_status ON queues(status, closes_at)',
    'CREATE INDEX IF NOT EXISTS idx_queues_subject ON queues(subject_id, lesson_at)',
    """CREATE INDEX IF NOT EXISTS idx_queues_group_pending ON queues(group_retry_at, id)
        WHERE group_message_id IS NOT NULL AND group_synced_revision < revision""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_subjects_chat_title ON subjects(chat_id, title)
        WHERE message_thread_id IS NULL""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_subjects_chat_thread ON subjects(chat_id, message_thread_id)
        WHERE message_thread_id IS NOT NULL""",
)

# Old topics can have two historical queues for the same lesson after their
# subject IDs are merged. Keep both rows and prohibit any new conflict.
QUEUE_CONFLICT = """
    SELECT RAISE(ABORT, 'duplicate_queue') WHERE EXISTS (
        SELECT 1 FROM queues WHERE subject_id=NEW.subject_id AND lesson_at=NEW.lesson_at
        AND status!='cancelled');
"""
QUEUE_TRIGGERS = (
    f"""CREATE TRIGGER IF NOT EXISTS prevent_duplicate_queue
        BEFORE INSERT ON queues WHEN NEW.status!='cancelled'
        BEGIN {QUEUE_CONFLICT} END""",
    f"""CREATE TRIGGER IF NOT EXISTS prevent_queue_key_conflict
        BEFORE UPDATE OF subject_id,lesson_at,status ON queues
        WHEN NEW.status!='cancelled' AND (OLD.status='cancelled'
            OR NEW.subject_id!=OLD.subject_id OR NEW.lesson_at!=OLD.lesson_at)
        BEGIN {QUEUE_CONFLICT} END""",
    """CREATE TRIGGER IF NOT EXISTS refresh_queue_member_name
        AFTER UPDATE OF name ON users WHEN NEW.name!=OLD.name
        BEGIN
            UPDATE queues SET revision=revision+1 WHERE id IN (
                SELECT queue_id FROM participants WHERE user_id=NEW.id AND status='joined');
        END""",
)

MEMBERS_QUERY = """SELECT p.*,u.name,u.username FROM participants p
    JOIN users u ON u.id=p.user_id WHERE p.queue_id=? {status_filter}
    ORDER BY COALESCE(p.current_position, 99999),p.joined_at,p.user_id"""
JOINED_MEMBERS = MEMBERS_QUERY.format(status_filter="AND p.status='joined'")


def migrate_queue_identity(conn, columns):
    """Replace the v2.8 unique keys while preserving IDs and dependent rows."""
    sequences = conn.execute(
        "SELECT name,seq FROM sqlite_sequence WHERE name IN ('queues','subjects')"
    ).fetchall()
    conn.execute(f'CREATE TABLE subjects_new ({SUBJECT_COLUMNS})')
    conn.execute('INSERT INTO subjects_new(id,chat_id,title) SELECT id,chat_id,title FROM subjects')
    conn.execute(f'CREATE TABLE queues_new ({QUEUE_COLUMNS})')
    thread = 'q.message_thread_id' if 'message_thread_id' in columns else 'NULL'
    conn.execute(f"""INSERT INTO queues_new(
        id,subject_id,chat_id,creator_id,lesson_at,closes_at,feedback_deadline,
        status,group_message_id,message_thread_id,seed,seed_hash,revision,created_at,title)
        SELECT q.id,q.subject_id,q.chat_id,q.creator_id,q.lesson_at,q.closes_at,q.feedback_deadline,
        q.status,q.group_message_id,{thread},q.seed,q.seed_hash,q.revision,q.created_at,s.title
        FROM queues q LEFT JOIN subjects s ON s.id=q.subject_id""")
    conn.execute('DROP TABLE queues')
    conn.execute('DROP TABLE subjects')
    conn.execute('ALTER TABLE subjects_new RENAME TO subjects')
    conn.execute('ALTER TABLE queues_new RENAME TO queues')
    for name, seq in sequences:
        conn.execute('UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?', (seq, name))

    rows = conn.execute("""SELECT subject_id,chat_id,message_thread_id,MAX(id) AS latest_queue
        FROM queues GROUP BY subject_id,chat_id,message_thread_id ORDER BY latest_queue DESC""").fetchall()
    usages = {}
    topics = {}
    for row in rows:
        subject_id = row['subject_id']
        topic = (row['chat_id'], row['message_thread_id'])
        usages[subject_id] = usages.get(subject_id, 0) + 1
        if row['message_thread_id'] is not None:
            topics.setdefault(topic, []).append(subject_id)

    for (chat_id, thread_id), sources in topics.items():
        title = conn.execute("""SELECT title FROM queues WHERE chat_id=? AND message_thread_id=?
            ORDER BY id DESC LIMIT 1""", (chat_id, thread_id)).fetchone()['title']
        subject_id = next((sid for sid in sources if usages[sid] == 1), None)
        if subject_id is None:
            subject_id = conn.execute('INSERT INTO subjects(chat_id,title,message_thread_id) VALUES(?,?,?)',
                                      (chat_id, title, thread_id)).lastrowid
        else:
            conn.execute('UPDATE subjects SET title=?,message_thread_id=? WHERE id=?',
                         (title, thread_id, subject_id))

        # Legacy titles may carry different balances for one user. Keep the
        # most recently updated balance instead of adding unrelated values.
        placeholders = ','.join('?' for _ in sources)
        conn.execute(f"""INSERT OR REPLACE INTO fairness(subject_id,user_id,balance,exit_penalties,updated_at)
            SELECT ?,user_id,balance,exit_penalties,updated_at FROM (
                SELECT *,ROW_NUMBER() OVER (
                    PARTITION BY user_id ORDER BY updated_at DESC,subject_id DESC
                ) AS rank FROM fairness WHERE subject_id IN ({placeholders})
            ) WHERE rank=1""", (subject_id, *sources))
        conn.execute('UPDATE queues SET subject_id=? WHERE chat_id=? AND message_thread_id=?',
                     (subject_id, chat_id, thread_id))

    # Remove old name-based subjects whose queues all moved to forum subjects.
    for subject_id in {sid for sources in topics.values() for sid in sources}:
        removed = conn.execute("""DELETE FROM subjects WHERE id=? AND message_thread_id IS NULL
            AND NOT EXISTS (SELECT 1 FROM queues WHERE subject_id=subjects.id)""", (subject_id,))
        if removed.rowcount:
            conn.execute('DELETE FROM fairness WHERE subject_id=?', (subject_id,))
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise sqlite3.IntegrityError('Queue migration would leave invalid foreign keys')


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            # SQLite requires foreign-key checks to be disabled while replacing
            # the parent tables. Check all references before committing.
            conn.execute('PRAGMA foreign_keys=OFF')
            with self.transaction(conn):
                columns = {row['name'] for row in conn.execute('PRAGMA table_info(queues)')}
                if 'group_synced_revision' not in columns:
                    migrate_queue_identity(conn, columns)
                elif 'use_preferences' not in columns:
                    conn.execute('''ALTER TABLE queues ADD COLUMN use_preferences INTEGER
                        NOT NULL DEFAULT 1 CHECK(use_preferences IN (0,1))''')
                for statement in (*QUEUE_INDEXES, *QUEUE_TRIGGERS):
                    conn.execute(statement)
            conn.execute('PRAGMA foreign_keys=ON')
            # Upgrade old post-lesson survey queues: no more surveys or carry-over.
            # They will be closed by the deadline worker at (lesson_at + 100 minutes).
            conn.execute("""UPDATE queues SET status='active',feedback_deadline=NULL,revision=revision+1
                WHERE status='feedback'""")
            conn.execute("UPDATE participants SET priority=0 WHERE priority!=0")
            # Migration for existing v1 databases: old exit penalties no longer apply.
            conn.execute('UPDATE fairness SET exit_penalties=0 WHERE exit_penalties!=0')
            # Old v1 relative preferences were removed; keep valid exact selections.
            conn.execute("UPDATE participants SET pref_mode='any',desired=NULL WHERE pref_mode NOT IN ('exact','any')")
            # v2.8: clear historical fairness and metrics just once per database.
            # Positions, queue statuses and group message IDs stay unchanged.
            if not conn.execute("SELECT 1 FROM app_metadata WHERE key='statistics_reset_v28'").fetchone():
                conn.execute('BEGIN IMMEDIATE')
                try:
                    conn.execute('DELETE FROM fairness')
                    conn.execute('DELETE FROM feedback')
                    conn.execute('UPDATE participants SET original_satisfaction=NULL, '
                                 'expected_satisfaction=NULL,balance_before=NULL, '
                                 'penalties_before=0,fairness_existed=NULL')
                    conn.execute("INSERT INTO app_metadata(key,value) VALUES('statistics_reset_v28','done')")
                    conn.execute('COMMIT')
                except BaseException:
                    conn.execute('ROLLBACK')
                    raise
            # Apply the list to existing participants too, immediately on upgrade.
            for username, person_name in preferred_names().items():
                conn.execute('UPDATE users SET name=? WHERE lower(username)=?',
                             (person_name, username))

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=30000')
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self, conn):
        conn.execute('BEGIN IMMEDIATE')
        try:
            yield conn
            conn.execute('COMMIT')
        except BaseException:
            conn.execute('ROLLBACK')
            raise

    @contextmanager
    def tx(self):
        with self.connect() as conn, self.transaction(conn):
            yield conn

    def one(self, query, params=()):
        with self.connect() as conn:
            return conn.execute(query, params).fetchone()

    def all(self, query, params=()):
        with self.connect() as conn:
            return conn.execute(query, params).fetchall()

    def exec(self, query, params=()):
        with self.tx() as conn:
            return conn.execute(query, params).rowcount

    def remember_topic(self, chat_id, thread_id, title, updated_at):
        self.exec("""INSERT INTO forum_topics(chat_id,message_thread_id,title,updated_at)
            VALUES(?,?,?,?) ON CONFLICT(chat_id,message_thread_id) DO UPDATE SET
                title=excluded.title, updated_at=excluded.updated_at
            WHERE excluded.updated_at>=forum_topics.updated_at""",
            (chat_id, thread_id, title, updated_at))

    def topic_title(self, chat_id, thread_id):
        topic = self.one('SELECT title FROM forum_topics WHERE chat_id=? AND message_thread_id=?',
                         (chat_id, thread_id))
        return topic['title'] if topic else None

    def user(self, tg_user, now: str):
        name = display_name(tg_user.username, tg_user.full_name or str(tg_user.id))[:120]
        with self.tx() as conn:
            conn.execute('''INSERT INTO users(id,name,username,created_at) VALUES(?,?,?,?)
                 ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username''',
                 (tg_user.id, name, tg_user.username, now))

    def queue(self, qid):
        return self.one('SELECT * FROM queues WHERE id=?', (qid,))

    def members(self, qid, active_only=True):
        query = JOINED_MEMBERS if active_only else MEMBERS_QUERY.format(status_filter='')
        return self.all(query, (qid,))

    def group_snapshot(self, qid):
        with self.connect() as conn:
            conn.execute('BEGIN')
            queue = conn.execute('SELECT * FROM queues WHERE id=?', (qid,)).fetchone()
            members = conn.execute(JOINED_MEMBERS, (qid,)).fetchall()
            return queue, members

    def event(self, conn, qid, actor, kind, details, now):
        conn.execute('INSERT INTO events(queue_id,actor_id,kind,details,occurred_at) VALUES(?,?,?,?,?)',
                     (qid,actor,kind,details,now))

    def bump(self, conn, qid):
        conn.execute('UPDATE queues SET revision=revision+1 WHERE id=?', (qid,))
