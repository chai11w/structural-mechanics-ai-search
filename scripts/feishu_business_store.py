"""Durable inbox, task snapshots and reply outbox; short SQLite transactions only."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

from scripts.bank_writer_client import digest
from tiku_shared.bank_publication import reject_links, write_lock

def event_digest(event):
    return digest({key: value for key, value in event.items() if key != "id"})


class FeishuBusinessStore:
    def __init__(self, directory, app_id):
        self.root = reject_links(Path(directory))
        self.root.mkdir(parents=True, exist_ok=True)
        self.lease = write_lock(self.root / "service.lock")
        self.lease.__enter__()
        try:
            self.file = self.root / "business.sqlite3"
            with self.connection() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, context TEXT NOT NULL, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS contexts (id TEXT PRIMARY KEY, task_id TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, message_id TEXT UNIQUE NOT NULL,
                        body_hash TEXT NOT NULL, event TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                        response TEXT, task_id TEXT, retry_after REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, event_id TEXT NOT NULL,
                        batch_key TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                        response TEXT, retry_after REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                        dispatched INTEGER NOT NULL DEFAULT 0);
                """)
                existing = db.execute("SELECT value FROM config WHERE key='app_id'").fetchone()
                if existing and existing[0] != app_id:
                    raise ValueError("飞书业务目录属于另一个应用")
                db.execute("INSERT OR IGNORE INTO config VALUES ('app_id', ?)", (app_id,))
        except Exception:
            self.close()
            raise

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.file, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def admit(self, event):
        encoded = json.dumps(event, ensure_ascii=False, allow_nan=False)
        with self.connection() as db:
            row = db.execute("SELECT id, body_hash FROM inbox WHERE id=? OR message_id=?", (event["id"], event["message_id"])).fetchone()
            if row:
                if row["body_hash"] != event_digest(event):
                    raise ValueError("重复消息内容不一致")
                return False
            if db.execute("SELECT COUNT(*) FROM inbox WHERE state='pending'").fetchone()[0] >= 200:
                raise ValueError("飞书任务队列已满，请稍后重试")
            db.execute("INSERT INTO inbox(id,message_id,body_hash,event) VALUES (?,?,?,?)",
                       (event["id"], event["message_id"], event_digest(event), encoded))
        return True

    def next(self):
        with self.connection() as db:
            row = db.execute("SELECT * FROM inbox WHERE state IN ('pending','processed') AND retry_after<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
        return dict(row) if row else None

    def event(self, event_id):
        with self.connection() as db:
            row = db.execute("SELECT event,body_hash FROM inbox WHERE id=?", (event_id,)).fetchone()
        event = json.loads(row[0]) if row else None
        if not event or event_digest(event) != row[1]:
            raise ValueError("业务消息身份不一致")
        return event

    def schedule(self, task, event_id, kind):
        """Commit intent and its trusted source before any writer side effect."""
        job_id = digest([task["id"], task["batch"]["key"], kind])
        with self.connection() as db:
            self._save(db, task)
            db.execute("UPDATE inbox SET task_id=? WHERE id=?", (task["id"], event_id))
            db.execute("""INSERT INTO jobs(id,task_id,event_id,batch_key,kind) VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET event_id=excluded.event_id,state='pending',response=NULL,retry_after=0,attempts=0,dispatched=0""",
                (job_id, task["id"], event_id, task["batch"]["key"], kind))

    def next_job(self):
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE state IN ('pending','processed') AND retry_after<=? ORDER BY retry_after,rowid LIMIT 1", (time.time(),)).fetchone()
        return dict(row) if row else None

    def complete_job(self, job_id, task, response):
        with self.connection() as db:
            self._save(db, task, False)
            db.execute("UPDATE jobs SET state='processed',response=?,retry_after=0,attempts=0 WHERE id=?",
                       (json.dumps(response, ensure_ascii=False, allow_nan=False), job_id))

    def job_replied(self, job_id, task=None):
        with self.connection() as db:
            if task:
                self._save(db, task, False)
            db.execute("UPDATE jobs SET state='replied',retry_after=0 WHERE id=?", (job_id,))

    def defer_job(self, job_id, *, failed=False):
        with self.connection() as db:
            if failed:
                db.execute("""UPDATE jobs SET attempts=attempts+1,retry_after=?,
                    state=CASE WHEN attempts>=4 THEN 'paused' ELSE state END WHERE id=?""", (time.time() + 15, job_id))
            else:
                db.execute("UPDATE jobs SET retry_after=? WHERE id=?", (time.time() + 1, job_id))
            return db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0]

    def dispatched(self, job_id):
        with self.connection() as db:
            db.execute("UPDATE jobs SET dispatched=1 WHERE id=?", (job_id,))

    def task(self, context):
        with self.connection() as db:
            row = db.execute("SELECT t.value FROM contexts c JOIN tasks t ON t.id=c.task_id WHERE c.id=?", (context,)).fetchone()
        return json.loads(row[0]) if row else None

    def by_id(self, task_id):
        with self.connection() as db:
            row = db.execute("SELECT value FROM tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _save(db, task, activate=True):
        active = db.execute("SELECT t.value FROM contexts c JOIN tasks t ON t.id=c.task_id WHERE c.id=?", (task["context"],)).fetchone()
        current = json.loads(active[0]) if active else None
        db.execute("INSERT INTO tasks VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                   (task["id"], task["context"], json.dumps(task, ensure_ascii=False, allow_nan=False)))
        if activate and (not current or current["id"] == task["id"] or current.get("last_event_ms", 0) <= task.get("last_event_ms", 0)):
            db.execute("INSERT INTO contexts VALUES (?,?) ON CONFLICT(id) DO UPDATE SET task_id=excluded.task_id", (task["context"], task["id"]))

    def save(self, task, *, activate=True, event_id=None):
        with self.connection() as db:
            self._save(db, task, activate)
            if event_id:
                db.execute("UPDATE inbox SET task_id=? WHERE id=?", (task["id"], event_id))

    def bind(self, event_id, task_id):
        with self.connection() as db:
            db.execute("UPDATE inbox SET task_id=? WHERE id=? AND task_id IS NULL", (task_id, event_id))

    def complete(self, event_id, task, response):
        with self.connection() as db:
            if task:
                self._save(db, task)
            db.execute("UPDATE inbox SET state='processed',response=?,retry_after=0 WHERE id=?",
                       (json.dumps(response, ensure_ascii=False, allow_nan=False), event_id))

    def replied(self, event_id, task=None):
        with self.connection() as db:
            if task:
                self._save(db, task, False)
            db.execute("UPDATE inbox SET state='replied',retry_after=0 WHERE id=?", (event_id,))

    def retry(self, event_id):
        with self.connection() as db:
            db.execute("""UPDATE inbox SET attempts=attempts+1,retry_after=?,
                state=CASE WHEN attempts>=4 THEN 'paused' ELSE state END WHERE id=?""", (time.time() + 15, event_id))

    def history(self, context):
        with self.connection() as db:
            rows = db.execute("SELECT value FROM tasks WHERE context=? ORDER BY rowid DESC LIMIT 10", (context,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def close(self):
        if self.lease:
            self.lease.__exit__(None, None, None)
            self.lease = None
