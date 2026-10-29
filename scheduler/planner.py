import time

from storage.workspace import tree_bytes


def reservation(file):
    # Selected payload + both boundary pieces + bounded resume/partfile headers,
    # block rounding and directory entries. Separate storage per file.
    size, piece = file['size'], file['piece']
    return ((size + 4095) // 4096 * 4096 + 2 * piece
            + 16 * 1024**2 + 4096 * (file['path'].count('/') + 4))


def batches(files, budget):
    """Best-fit decreasing estimate; actual execution refills after every upload."""
    bins = []
    for f in sorted(files, key=reservation, reverse=True):
        cost = reservation(f)
        if cost > budget:
            continue
        for i in sorted(range(len(bins)), key=lambda j: bins[j]):
            if bins[i] >= cost:
                bins[i] -= cost
                break
        else:
            bins.append(budget - cost)
    return len(bins)


class SmartStorageScheduler:
    def __init__(self, store, workspace, settings):
        self.db, self.ws, self.s = store, workspace, settings

    def claim(self):
        # BEGIN IMMEDIATE makes reserve+state transition atomic across DB clients.
        # The process-wide filesystem lock excludes a second actual worker.
        with self.db.transaction():
            active = self.db.one('SELECT COALESCE(SUM(reservation),0) n, '
                                 'SUM(CASE WHEN reservation>0 THEN 1 ELSE 0 END) c FROM files')
            if (active['c'] or 0) >= self.s.slots:
                return None
            reserved = active['n']
            covered = 0
            for f in self.db.all('SELECT job,idx,reservation FROM files WHERE reservation>0'):
                slot = self.ws.slot(f['job'], f['idx'])
                covered += min(f['reservation'], tree_bytes(slot) if slot.exists() else 0)
            # Read global usage AFTER per-slot coverage. libtorrent can grow files
            # concurrently, but deletion runs synchronously on this event loop.
            used = self.ws.used()
            unreserved = max(0, used - covered)
            budget = min(self.s.limit - self.s.reserve - unreserved - reserved,
                         self.ws.free() - self.s.min_free - reserved)
            candidates = self.db.all('''SELECT f.* FROM files f JOIN jobs j ON j.id=f.job
              WHERE j.state='running' AND f.selected=1 AND f.state='queued'
              AND f.retry_at<=? AND f.reservation=0 ORDER BY j.created,f.idx''', (time.time(),))
            fits = [f for f in candidates if reservation(f) <= budget]
            if not fits:
                return None
            # Prefer older jobs, largest fitting file within that job.
            job = fits[0]['job']
            f = max((f for f in fits if f['job'] == job), key=reservation)
            f['reservation'] = reservation(f)
            self.db.update_file(f['job'], f['idx'], reservation=f['reservation'], state='downloading')
            return f
