"""Nonblocking Linux directory notifications, without idle filesystem scans."""
import ctypes
import errno
import os
import stat
import struct
import sys
from pathlib import Path


_MODIFY = 0x00000002
_ATTRIB = 0x00000004
_CLOSE_WRITE = 0x00000008
_MOVED_FROM = 0x00000040
_MOVED_TO = 0x00000080
_CREATE = 0x00000100
_DELETE = 0x00000200
_DELETE_SELF = 0x00000400
_MOVE_SELF = 0x00000800
_UNMOUNT = 0x00002000
_Q_OVERFLOW = 0x00004000
_IGNORED = 0x00008000
_ONLYDIR = 0x01000000
_DONT_FOLLOW = 0x02000000
_ISDIR = 0x40000000
_CONTENTS = _MODIFY | _ATTRIB | _CLOSE_WRITE | _MOVED_FROM | _MOVED_TO | _CREATE | _DELETE
_TOPOLOGY = _MOVED_FROM | _MOVED_TO | _CREATE | _DELETE
_LOST = _DELETE_SELF | _MOVE_SELF | _UNMOUNT | _IGNORED
_WATCH_MASK = _CONTENTS | _DELETE_SELF | _MOVE_SELF | _UNMOUNT | _ONLYDIR | _DONT_FOLLOW
_EVENT = struct.Struct('iIII')


def _inotify_functions():
    if not sys.platform.startswith('linux'):
        raise NotImplementedError('recording notifications require Linux inotify')
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        init, add = libc.inotify_init1, libc.inotify_add_watch
    except AttributeError as exc:
        raise NotImplementedError('inotify is unavailable') from exc
    init.argtypes, init.restype = [ctypes.c_int], ctypes.c_int
    add.argtypes, add.restype = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32], ctypes.c_int
    return init, add


def _visible(name):
    return not name.startswith(('.', '@')) and name.lower() != '#recycle'


class RecordingWatcher:
    """Watch real visible directories under root; call poll() from one thread.

    Construction visits the directory tree once. An idle poll only reads the
    nonblocking inotify descriptor. Topology events rebuild watches before
    returning True, including already populated directories moved into root.
    OSError means coverage is uncertain: use polling and create a new watcher
    when retrying. close() releases the descriptor and is safe to repeat.
    """

    def __init__(self, root):
        self.root = Path(os.path.abspath(os.fspath(root)))
        self._fd = None
        self._watches = {}
        self._init, self._add_watch = _inotify_functions()
        self._rebuild()

    def _rebuild(self):
        # Checking ancestors prevents following a symlink in the mount path.
        for path in (self.root, *self.root.parents):
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise OSError(errno.ELOOP, 'symlink recordings path is forbidden', str(path))
            if path == self.root and not stat.S_ISDIR(mode):
                raise OSError(errno.ENOTDIR, 'recordings root is not a directory', str(path))
        # Defaults let portable tests inject inotify on non-Linux hosts; real
        # Linux uses its architecture's O_NONBLOCK and O_CLOEXEC values.
        fd = self._init(getattr(os, 'O_NONBLOCK', 0x800) |
                        getattr(os, 'O_CLOEXEC', 0x80000))
        if fd < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        watches = {}
        try:
            pending = [self.root]
            while pending:
                path = pending.pop()
                wd = self._add_watch(fd, os.fsencode(path), _WATCH_MASK)
                if wd < 0:
                    code = ctypes.get_errno()
                    raise OSError(code, os.strerror(code), str(path))
                if wd in watches:
                    raise OSError(errno.ELOOP, 'directory is reachable by multiple paths', str(path))
                watches[wd] = path
                with os.scandir(path) as entries:
                    for entry in entries:
                        if _visible(entry.name) and entry.is_dir(follow_symlinks=False):
                            pending.append(path / entry.name)
        except BaseException:
            os.close(fd)
            raise
        old = self._fd
        self._fd, self._watches = fd, watches
        if old is not None:
            os.close(old)

    def poll(self):
        if self._fd is None:
            raise OSError(errno.EBADF, 'recording watcher is closed')
        changed, rebuild = False, False
        try:
            while True:
                try:
                    data = os.read(self._fd, 65536)
                except InterruptedError:
                    continue
                except BlockingIOError:
                    break
                if not data:
                    raise OSError(errno.EIO, 'unexpected end of inotify stream')
                offset = 0
                while offset < len(data):
                    if len(data) - offset < _EVENT.size:
                        raise OSError(errno.EIO, 'truncated inotify event')
                    wd, mask, _cookie, length = _EVENT.unpack_from(data, offset)
                    end = offset + _EVENT.size + length
                    if end > len(data):
                        raise OSError(errno.EIO, 'truncated inotify event name')
                    name = os.fsdecode(data[offset + _EVENT.size:end].split(b'\0', 1)[0])
                    offset = end
                    if mask & _Q_OVERFLOW:
                        raise OSError(errno.EOVERFLOW, 'inotify queue overflow; recording coverage lost')
                    path = self._watches.get(wd)
                    if path is None:
                        raise OSError(errno.EIO, 'unknown inotify watch; recording coverage lost')
                    if mask & _LOST:
                        if path == self.root:
                            raise OSError(errno.ENOENT, 'recordings root watch was lost', str(self.root))
                        changed = rebuild = True
                    if name and not _visible(name):
                        continue
                    if mask & _CONTENTS:
                        changed = True
                        if mask & _ISDIR and mask & _TOPOLOGY:
                            rebuild = True
            if rebuild:
                self._rebuild()
            return changed
        except OSError:
            self.close()
            raise

    def close(self):
        fd, self._fd = self._fd, None
        self._watches = {}
        if fd is not None:
            os.close(fd)
