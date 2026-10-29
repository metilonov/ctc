import os
import re
import shutil
from pathlib import Path, PurePosixPath

def safe_path(value: str) -> str:
    if '\\' in value or ':' in value or '\x00' in value or any(ord(c) < 32 for c in value):
        raise ValueError('Unsafe torrent path')
    p = PurePosixPath(value)
    if p.is_absolute() or not p.parts or any(x in ('..', '.') for x in value.split('/')):
        raise ValueError('Unsafe torrent path')
    for part in p.parts:
        if len(part) > 240 or part.endswith((' ', '.')) or re.match(
            r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', part, re.I
        ):
            raise ValueError('Unsupported torrent path')
    return p.as_posix()


def tree_bytes(root: Path) -> int:
    """Conservative logical sizes (including sparse files) plus entry overhead."""
    total = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        total += (len(dirs) + len(files) + 1) * 4096
        for name in files:
            p = Path(base) / name
            if p.is_symlink():
                raise RuntimeError('Symlink in workspace')
            total += p.stat().st_size
    return total


class Workspace:
    def __init__(self, settings):
        self.s = settings
        self.root = settings.root
        self.root.mkdir(parents=True, exist_ok=True)
        self.slots = self.root / 'slots'
        self.slots.mkdir(exist_ok=True)
        self.lock = None

    def acquire(self):
        self.lock = (self.root / '.lock').open('a+b')
        self.lock.seek(0)
        self.lock.write(b'0')
        self.lock.flush()
        self.lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError('Another process owns DATA_DIR') from None

    def slot(self, job, idx):
        if not re.fullmatch('[a-f0-9]{12}', job) or not isinstance(idx, int) or idx < 0:
            raise ValueError('Invalid slot')
        return self.slots / f'{job}-{idx}'

    def clean(self, job, idx):
        original = self.slot(job, idx)
        path = original.resolve()
        if path.parent != self.slots.resolve() or original.is_symlink():
            raise RuntimeError('Unsafe cleanup target')
        if path.exists():
            shutil.rmtree(path)

    def used(self):
        return tree_bytes(self.root)

    def free(self):
        return shutil.disk_usage(self.root).free
