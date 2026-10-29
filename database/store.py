import json
import sqlite3
import time
from contextlib import contextmanager


class Store:
    """One process, short synchronous transactions; never hold one over await."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, metadata BLOB NOT NULL,
          state TEXT NOT NULL DEFAULT 'selecting', created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS files(
          job TEXT NOT NULL REFERENCES jobs(id), idx INTEGER NOT NULL,
          path TEXT NOT NULL, size INTEGER NOT NULL, offset INTEGER NOT NULL,
          piece INTEGER NOT NULL, selected INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL DEFAULT 'queued', reservation INTEGER NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
          progress INTEGER NOT NULL DEFAULT 0, uploaded INTEGER NOT NULL DEFAULT 0,
          drive_id TEXT, upload_session TEXT, md5 TEXT, error TEXT,
          PRIMARY KEY(job,idx));
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def execute(self, sql, args=()):
        return self.db.execute(sql, args)

    def all(self, sql, args=()):
        return [dict(r) for r in self.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        row = self.execute(sql, args).fetchone()
        return dict(row) if row else None

    def update_file(self, job, idx, **fields):
        allowed = {'selected', 'state', 'reservation', 'attempts', 'retry_at', 'progress',
                   'uploaded', 'drive_id', 'upload_session', 'md5', 'error'}
        if not fields or not fields.keys() <= allowed:
            raise ValueError('Invalid fields')
        self.execute('UPDATE files SET ' + ','.join(f'{k}=?' for k in fields)
                     + ' WHERE job=? AND idx=?', (*fields.values(), job, idx))

    def get(self, key, default=None):
        r = self.one('SELECT value FROM kv WHERE key=?', (key,))
        return json.loads(r['value']) if r else default

    def put(self, key, value):
        self.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (key, json.dumps(value)))

    def add_job(self, job, name, metadata, files):
        with self.transaction():
            self.execute('INSERT INTO jobs(id,name,metadata,created) VALUES(?,?,?,?)',
                         (job, name, metadata, time.time()))
            self.db.executemany('INSERT INTO files(job,idx,path,size,offset,piece) VALUES(?,?,?,?,?,?)',
                                [(job, f['idx'], f['path'], f['size'], f['offset'], f['piece'])
                                 for f in files])
