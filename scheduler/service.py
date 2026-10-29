import asyncio
import logging
import time
import uuid

from scheduler.planner import SmartStorageScheduler, reservation
from drive.client import md5_file
from storage.workspace import tree_bytes
from torrent.engine import StorageSynchronizationError, inspect

log = logging.getLogger(__name__)


class Service:
    def __init__(self, db, ws, settings, engine, drive):
        self.db, self.ws, self.s = db, ws, settings
        self.engine, self.drive = engine, drive
        self.planner = SmartStorageScheduler(db, ws, settings)
        self.workers = {}
        self.rates = {}
        self.stopping = False
        self.disk_blocked = False

    def add(self, data):
        ti, files = inspect(data)
        if self.db.one('SELECT COUNT(*) n FROM jobs')['n'] >= 100:
            raise ValueError('100 job history limit: archive data offline before adding more')
        estimate = len(data) * 3 + sum(len(f['path'].encode()) + 512 for f in files) + 32768
        used = self.ws.used()
        control = used - tree_bytes(self.ws.slots)
        if (control + estimate > self.s.reserve or used + estimate > self.s.limit
                or self.ws.free() < self.s.min_free + estimate):
            raise ValueError('Metadata/control reserve exhausted')
        job = uuid.uuid4().hex[:12]
        self.db.add_job(job, ti.name(), data, files)
        return job

    def start(self, job):
        files = self.db.all('SELECT * FROM files WHERE job=? AND selected=1', (job,))
        if not files:
            raise ValueError('Сначала выберите файлы')
        if any(reservation(f) > self.s.limit - self.s.reserve for f in files):
            raise ValueError('Один из файлов не помещается с техническим резервом. Снимите его выбор.')
        self.db.execute("UPDATE jobs SET state='running' WHERE id=? AND state!='cancelled'", (job,))
        self.db.execute("UPDATE files SET state='queued',attempts=0,retry_at=0,error=NULL "
                        "WHERE job=? AND state='error'", (job,))
        self.db.execute("UPDATE files SET attempts=0,retry_at=0,error=NULL "
                        "WHERE job=? AND state='confirmed'", (job,))

    def cleanup_confirmed(self, f):
        if f['attempts'] >= self.s.attempts or f['retry_at'] > time.time():
            return
        try:
            self.ws.clean(f['job'], f['idx'])
        except OSError as exc:
            attempts = f['attempts'] + 1
            self.db.update_file(f['job'], f['idx'], attempts=attempts,
                retry_at=time.time() + min(900, 2**attempts * 5), error=type(exc).__name__)
        else:
            self.db.update_file(f['job'], f['idx'], state='done', reservation=0, error=None)

    def recover(self):
        # Handles do not exist at startup. Never remove unconfirmed payloads.
        for f in self.db.all('SELECT * FROM files'):
            slot = self.ws.slot(f['job'], f['idx'])
            if f['state'] == 'confirmed':
                self.cleanup_confirmed(f)
            elif f['state'] in ('downloading', 'uploading', 'ready'):
                self.db.update_file(f['job'], f['idx'], state='queued')
            if slot.exists() and f['state'] not in ('done', 'confirmed') and not f['reservation']:
                self.db.update_file(f['job'], f['idx'], reservation=reservation(f))
        # Unrecognized directories are retained and count against the budget.

    def interrupted(self, job, upload=False):
        state = self.db.one('SELECT state FROM jobs WHERE id=?', (job,))
        return (self.stopping or (self.disk_blocked and not upload)
                or not state or state['state'] != 'running')

    async def process(self, f):
        job, idx = f['job'], f['idx']
        try:
            slot = self.ws.slot(job, idx)
            slot.mkdir(parents=True, exist_ok=True)
            path = slot / 'payload' / f['path']
            if f.get('md5'):
                # A durable digest is recorded only after verified torrent pieces
                # and closed disk handles. Resume uploads without new torrent I/O.
                if (not path.is_file() or path.stat().st_size != f['size']
                        or await asyncio.to_thread(md5_file, path) != f['md5']):
                    raise RuntimeError('Retained verified payload changed; operator action required')
                digest = f['md5']
            else:
                meta = self.db.one('SELECT metadata FROM jobs WHERE id=?', (job,))['metadata']
                path = await self.engine.download(meta, f, slot,
                    lambda n, rate: self.progress(f, n, rate, False), lambda: self.interrupted(job))
                digest = await asyncio.to_thread(md5_file, path)
            self.db.update_file(job, idx, state='uploading', progress=f['size'], md5=digest)
            current = self.db.one('SELECT * FROM files WHERE job=? AND idx=?', (job, idx))
            fid = await self.drive.upload(current, path, lambda: self.interrupted(job, upload=True),
                lambda n, rate: self.progress(f, n, rate, True))
            # Durable remote confirmation MUST precede deletion.
            self.db.update_file(job, idx, state='confirmed', drive_id=fid,
                                uploaded=f['size'], upload_session=None, attempts=0, retry_at=0)
            self.cleanup_confirmed(self.db.one('SELECT * FROM files WHERE job=? AND idx=?', (job, idx)))
        except InterruptedError:
            self.db.update_file(job, idx, state='queued')
        except StorageSynchronizationError:
            self.stopping = True
            raise
        except Exception as exc:
            current = self.db.one('SELECT * FROM files WHERE job=? AND idx=?', (job, idx))
            if current['state'] == 'confirmed':
                # Cleanup retry, never download/upload an already confirmed file.
                return
            attempts = current['attempts'] + 1
            self.db.update_file(job, idx, state='error' if attempts >= self.s.attempts else 'queued',
                attempts=attempts, retry_at=time.time() + min(900, 2**attempts * 5),
                error=type(exc).__name__)
            log.warning('File operation failed job=%s file=%s type=%s', job, idx, type(exc).__name__)
        finally:
            self.rates.pop((job, idx), None)

    def progress(self, f, n, rate, upload):
        self.db.update_file(f['job'], f['idx'], **({'uploaded': n} if upload else {'progress': n}))
        self.rates[(f['job'], f['idx'])] = (0 if upload else rate, rate if upload else 0)

    async def run(self):
        self.recover()
        try:
            while not self.stopping:
                for key, worker in list(self.workers.items()):
                    if worker.done():
                        worker.result()
                        del self.workers[key]
                self.disk_blocked = (self.ws.used() >= self.s.limit - self.s.reserve // 2
                                     or self.ws.free() < self.s.min_free)
                if self.disk_blocked:
                    self.engine.pause_all()
                # Cleanup confirmed files, including after crash between confirm/unlink.
                for f in self.db.all("SELECT * FROM files WHERE state='confirmed'"):
                    if (f['job'], f['idx']) not in self.workers:
                        self.cleanup_confirmed(f)
                # Resume retained reservations before admitting new payloads.
                retained = self.db.all('''SELECT f.* FROM files f JOIN jobs j ON f.job=j.id
                  WHERE f.reservation>0 AND f.state='queued' AND j.state='running'
                  AND f.retry_at<=?''', (time.time(),))
                for f in retained:
                    key = (f['job'], f['idx'])
                    if (key not in self.workers and len(self.workers) < self.s.slots
                            and (not self.disk_blocked or f['md5'])):
                        self.db.update_file(*key, state='uploading' if f['md5'] else 'downloading')
                        self.workers[key] = asyncio.create_task(self.process(f))
                if not self.disk_blocked:
                    if len(self.workers) < self.s.slots:
                        f = self.planner.claim()
                        if f:
                            key = (f['job'], f['idx'])
                            self.workers[key] = asyncio.create_task(self.process(f))
                self.db.execute("UPDATE jobs SET state='done' WHERE state='running' AND "
                    "NOT EXISTS(SELECT 1 FROM files WHERE files.job=jobs.id AND selected=1 AND state!='done')")
                await asyncio.sleep(.5)
        finally:
            self.stopping = True
            await asyncio.gather(*self.workers.values(), return_exceptions=True)

    async def control(self, job, action):
        if action not in ('paused', 'cancelled'):
            raise ValueError('Invalid action')
        self.db.execute('UPDATE jobs SET state=? WHERE id=?', (action, job))
        targets = [w for (j, _), w in self.workers.items() if j == job]
        if targets:
            await asyncio.gather(*targets)
        if action == 'cancelled':
            for f in self.db.all('SELECT * FROM files WHERE job=?', (job,)):
                # Cancellation explicitly discards local unconfirmed data.
                self.ws.clean(job, f['idx'])
                self.db.update_file(job, f['idx'], reservation=0,
                                    state='done' if f['state'] in ('confirmed', 'done') else 'cancelled')
