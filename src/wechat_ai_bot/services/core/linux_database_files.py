"""Track SQLite sidecars and recover WALs still held by the WeChat process.

All source files are opened read-only. Orphaned WALs are queried through a
private encrypted snapshot; never restore/unlink live WeChat files.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import tempfile
import time
from pathlib import Path


class LinuxDatabaseFiles:
    def __init__(self):
        self._last_scan = float('-inf')
        self._orphans = {}
        self._snapshots = {}

    @staticmethod
    def _stat(path, *, contents=False):
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        return identity + (stat.st_size, stat.st_mtime_ns) if contents else identity

    def _wechat_processes(self):
        for proc in Path('/proc').glob('[0-9]*'):
            try:
                if int(proc.name) != os.getpid() and (proc / 'comm').read_text().strip() in {'wechat', 'WeChat', 'weixin'}:
                    yield proc
            except OSError:
                continue

    def _scan(self):
        if time.monotonic() - self._last_scan < 2:
            return
        found = {}
        for proc in self._wechat_processes():
            try:
                descriptors = {}
                for fd in (proc / 'fd').iterdir():
                    try:
                        descriptors.setdefault(os.readlink(fd), fd)
                    except OSError:
                        continue
                for target, wal in descriptors.items():
                    if not target.endswith('.db-wal (deleted)'):
                        continue
                    db = Path(target.removesuffix('-wal (deleted)'))
                    main = descriptors.get(str(db))
                    shm = descriptors.get(str(db) + '-shm (deleted)')
                    if main and shm and self._stat(main) == self._stat(db):
                        found.setdefault(db, []).append((main, wal, shm))
            except OSError:
                continue
        self._orphans = found
        self._last_scan = time.monotonic()

    def _orphan(self, path):
        self._scan()
        candidates = []
        for main, wal, shm in self._orphans.get(path, []):
            try:
                # Validate cached descriptors: fd numbers can be reused after close.
                if (os.readlink(main) == str(path)
                        and os.readlink(wal) == str(path) + '-wal (deleted)'
                        and os.readlink(shm) == str(path) + '-shm (deleted)'
                        and self._stat(main) == self._stat(path)
                        and wal.stat().st_size >= 32):
                    candidates.append((wal.stat().st_mtime_ns, main, wal, shm))
            except OSError:
                continue
        if not candidates:
            return None
        _, main, wal, shm = max(candidates)
        # A newer, nonempty WAL at the normal path takes precedence.
        current = Path(str(path) + '-wal')
        try:
            if current.stat().st_size >= 32 and current.stat().st_mtime_ns >= wal.stat().st_mtime_ns:
                return None
        except FileNotFoundError:
            pass
        return main, wal, shm

    def identity(self, path):
        path = Path(path).absolute()
        main_identity = self._stat(path)
        orphan = self._orphan(path)
        if orphan:
            main, wal, shm = orphan
            return ('orphan', self._stat(main, contents=True), self._stat(wal, contents=True), self._stat(shm))
        sidecars = []
        for suffix in ('-wal', '-shm'):
            try:
                sidecars.append(self._stat(Path(str(path) + suffix)))
            except FileNotFoundError:
                sidecars.append(None)
        return ('live', main_identity, *sidecars)

    def read_path(self, path, identity):
        if identity[0] != 'orphan':
            return path
        path = Path(path).absolute()
        orphan = self._orphan(path)
        if not orphan:
            raise OSError('WAL source changed before snapshot')
        main, wal, shm = orphan
        self.discard(path)
        directory = tempfile.TemporaryDirectory(prefix='wechat-ai-db-')
        destination = Path(directory.name) / path.name
        try:
            with shm.open('rb') as lock:
                # SQLite WAL write/checkpoint/recovery locks, bytes 120..122.
                # Nonblocking: retry through the existing polling recovery path.
                fcntl.lockf(lock, fcntl.LOCK_SH | fcntl.LOCK_NB, 3, 120)
                try:
                    if self.identity(path) != identity:
                        raise OSError('WAL changed before snapshot')
                    for source, target in ((main, destination), (wal, Path(str(destination) + '-wal'))):
                        with source.open('rb') as src, target.open('xb') as dst:
                            os.chmod(target, 0o600)
                            shutil.copyfileobj(src, dst)
                    if self.identity(path) != identity:
                        raise OSError('Database changed during WAL snapshot')
                finally:
                    fcntl.lockf(lock, fcntl.LOCK_UN, 3, 120)
        except Exception:
            directory.cleanup()
            raise
        self._snapshots[path] = directory
        return destination

    def discard(self, path):
        directory = self._snapshots.pop(Path(path).absolute(), None)
        if directory:
            directory.cleanup()

    def close(self):
        for path in list(self._snapshots):
            self.discard(path)

    @property
    def snapshot_count(self):
        return len(self._snapshots)
