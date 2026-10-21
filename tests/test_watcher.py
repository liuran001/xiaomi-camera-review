import ctypes
import errno
import importlib
import importlib.util
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def event(wd, mask, name='', cookie=0):
    name = os.fsencode(name) + b'\0' if name else b''
    name += b'\0' * (-len(name) % 4)
    return struct.pack('iIII', wd, mask, cookie, len(name)) + name


class FakeInotify:
    def __init__(self):
        self.fds = []
        self.flags = []
        self.watches = {}
        self.masks = []

    def init(self, flags):
        self.flags.append(flags)
        self.fds.append(200 + len(self.fds))
        self.watches[self.fds[-1]] = {}
        return self.fds[-1]

    def add(self, fd, path, mask):
        path = Path(os.fsdecode(path))
        watches = self.watches[fd]
        watches.setdefault(path, len(watches) + 1)
        self.masks.append(mask)
        return watches[path]

    def wd(self, path, fd=None):
        return self.watches[self.fds[-1] if fd is None else fd][path]


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('camera_review.watcher'),
                             'RecordingWatcher module must exist')
        self.module = importlib.import_module('camera_review.watcher')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake = FakeInotify()
        self.addCleanup(patch.stopall)
        patch.object(self.module, '_inotify_functions',
                     return_value=(self.fake.init, self.fake.add)).start()
        self.closed = patch.object(self.module.os, 'close').start()
        self.read = patch.object(self.module.os, 'read',
                                 side_effect=BlockingIOError(errno.EAGAIN, 'empty')).start()

    def watcher(self):
        watcher = self.module.RecordingWatcher(self.root)
        self.addCleanup(watcher.close)
        return watcher

    def feed(self, *events):
        self.read.side_effect = [*events, BlockingIOError(errno.EAGAIN, 'empty')]

    def test_constructor_watches_only_visible_real_directories(self):
        for name in ['camera/day', '.hidden/sub', '@eaDir/sub', '#ReCyClE/sub']:
            (self.root / name).mkdir(parents=True)
        self.watcher()
        self.assertEqual(set(self.fake.watches[self.fake.fds[-1]]),
                         {self.root, self.root / 'camera', self.root / 'camera/day'})
        # Reading/scanning the recordings must not generate feedback events.
        for mask in self.fake.masks:
            self.assertFalse(mask & (0x00000001 | 0x00000010 | 0x00000020))
            self.assertTrue(mask & 0x01000000)  # IN_ONLYDIR
            self.assertTrue(mask & 0x02000000)  # IN_DONT_FOLLOW

    def test_idle_poll_only_reads_the_nonblocking_descriptor(self):
        watcher = self.watcher()
        with patch.object(self.module.os, 'scandir', side_effect=AssertionError('idle walk')), \
                patch.object(self.module.os, 'stat', side_effect=AssertionError('idle stat')), \
                patch.object(self.module.os, 'lstat', side_effect=AssertionError('idle lstat')):
            for _ in range(5):
                self.assertFalse(watcher.poll())
        self.assertEqual(self.read.call_count, 5)

    def test_constructor_uses_platform_nonblocking_and_cloexec_flags(self):
        # Linux flag values vary by architecture (for example MIPS).
        with patch.object(self.module.os, 'O_NONBLOCK', 0x80, create=True), \
                patch.object(self.module.os, 'O_CLOEXEC', 0x800, create=True):
            self.watcher()
        self.assertEqual(self.fake.flags, [0x880])

    def test_poll_drains_and_reports_visible_file_changes(self):
        watcher = self.watcher()
        wd = self.fake.wd(self.root)
        for mask in [0x2, 0x4, 0x8, 0x40, 0x80, 0x100, 0x200]:
            with self.subTest(mask=mask):
                self.feed(event(wd, mask, 'clip.mp4'), event(wd, mask, 'other.mp4'))
                self.assertTrue(watcher.poll())
                self.assertEqual(self.read.side_effect.__length_hint__(), 0)

    def test_hidden_file_and_directory_events_do_not_scan_or_report_dirty(self):
        watcher = self.watcher()
        wd = self.fake.wd(self.root)
        self.feed(b''.join(event(wd, 0x40000100, name)
                           for name in ['.hidden', '@eaDir', '#RECYCLE']))
        with patch.object(self.module.os, 'scandir', side_effect=AssertionError('noise walk')):
            self.assertFalse(watcher.poll())
        self.assertEqual(len(self.fake.fds), 1)

    def test_new_populated_directory_is_watched_before_reporting_dirty(self):
        watcher = self.watcher()
        (self.root / 'incoming/day/hour').mkdir(parents=True)
        (self.root / 'incoming/day/hour/clip.mp4').write_bytes(b'video')
        self.feed(event(self.fake.wd(self.root), 0x40000080, 'incoming'))
        self.assertTrue(watcher.poll())
        self.assertTrue({self.root / 'incoming', self.root / 'incoming/day',
                         self.root / 'incoming/day/hour'} <=
                        set(self.fake.watches[self.fake.fds[-1]]))

    def test_directory_rename_replaces_stale_paths(self):
        (self.root / 'old/sub').mkdir(parents=True)
        watcher = self.watcher()
        wd, child = self.fake.wd(self.root), self.fake.wd(self.root / 'old')
        (self.root / 'old').rename(self.root / 'new')
        self.feed(event(wd, 0x40000040, 'old', 7) +
                  event(wd, 0x40000080, 'new', 7) + event(child, 0x800))
        self.assertTrue(watcher.poll())
        self.assertEqual(set(self.fake.watches[self.fake.fds[-1]]),
                         {self.root, self.root / 'new', self.root / 'new/sub'})
        self.closed.assert_any_call(self.fake.fds[0])

    def test_unexpected_child_watch_loss_rebuilds_coverage(self):
        (self.root / 'camera').mkdir()
        watcher = self.watcher()
        self.feed(event(self.fake.wd(self.root / 'camera'), 0x8000))
        self.assertTrue(watcher.poll())
        self.assertIn(self.root / 'camera', self.fake.watches[self.fake.fds[-1]])
        self.assertEqual(len(self.fake.fds), 2)

    def test_overflow_and_root_loss_fail_closed_for_polling_fallback(self):
        for mask, use_root in [(0x4000, False), (0x8000, True),
                               (0x800, True), (0x400, True), (0x2000, True)]:
            with self.subTest(mask=mask):
                watcher = self.watcher()
                fd = self.fake.fds[-1]
                self.feed(event(self.fake.wd(self.root) if use_root else -1, mask))
                with self.assertRaises(OSError):
                    watcher.poll()
                self.closed.assert_any_call(fd)
                with self.assertRaises(OSError):
                    watcher.poll()

    def test_interrupted_read_is_retried(self):
        watcher = self.watcher()
        self.feed(InterruptedError(), event(self.fake.wd(self.root), 0x8, 'clip.mp4'))
        self.assertTrue(watcher.poll())

    def test_invalid_event_or_unexpected_eof_fails_closed(self):
        for data in [b'', b'bad', struct.pack('iIII', 1, 0x100, 0, 12)]:
            with self.subTest(data=data):
                watcher = self.watcher()
                self.feed(data)
                with self.assertRaises(OSError):
                    watcher.poll()

    def test_constructor_failure_closes_descriptor(self):
        self.fake.add = lambda *_args: -1
        ctypes.set_errno(errno.ENOSPC)
        with patch.object(self.module, '_inotify_functions',
                          return_value=(self.fake.init, self.fake.add)):
            with self.assertRaises(OSError):
                self.module.RecordingWatcher(self.root)
        self.closed.assert_called_once_with(self.fake.fds[-1])

    def test_missing_or_non_directory_root_is_rejected_without_opening_descriptor(self):
        file = self.root / 'file'
        file.write_bytes(b'not a directory')
        for path in [file, self.root / 'missing']:
            with self.subTest(path=path), self.assertRaises(OSError):
                self.module.RecordingWatcher(path)
        self.assertFalse(self.fake.fds)

    def test_failed_rebuild_closes_both_old_and_partial_new_descriptor(self):
        watcher = self.watcher()
        old = self.fake.fds[-1]
        (self.root / 'new').mkdir()
        self.feed(event(self.fake.wd(self.root), 0x40000100, 'new'))
        original = self.fake.add

        def fail_new_watch(fd, path, mask):
            if Path(os.fsdecode(path)) == self.root / 'new':
                ctypes.set_errno(errno.ENOSPC)
                return -1
            return original(fd, path, mask)

        watcher._add_watch = fail_new_watch
        with self.assertRaises(OSError):
            watcher.poll()
        for fd in [old, self.fake.fds[-1]]:
            self.closed.assert_any_call(fd)

    def test_deleted_subtree_rebuilds_without_retaining_old_watches(self):
        camera = self.root / 'camera'
        camera.mkdir()
        watcher = self.watcher()
        root_wd, child_wd = self.fake.wd(self.root), self.fake.wd(camera)
        camera.rmdir()
        self.feed(event(child_wd, 0x400) + event(child_wd, 0x8000) +
                  event(root_wd, 0x40000200, 'camera'))
        self.assertTrue(watcher.poll())
        self.assertEqual(set(self.fake.watches[self.fake.fds[-1]]), {self.root})

    def test_descriptor_read_error_closes_watcher(self):
        watcher = self.watcher()
        self.read.side_effect = OSError(errno.EIO, 'failed')
        with self.assertRaises(OSError):
            watcher.poll()
        self.closed.assert_called_once_with(self.fake.fds[-1])

    def test_non_linux_platform_reports_notifications_unavailable(self):
        # Restore the real function that the normal portable fixture replaces.
        patch.stopall()
        with patch.object(self.module.sys, 'platform', 'win32'):
            with self.assertRaises(NotImplementedError):
                self.module.RecordingWatcher(self.root)

    def test_close_is_idempotent_and_poll_after_close_fails(self):
        watcher = self.watcher()
        watcher.close()
        watcher.close()
        self.closed.assert_called_once_with(self.fake.fds[-1])
        with self.assertRaises(OSError):
            watcher.poll()


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux inotify required')
class LinuxWatcherTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('camera_review.watcher')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def watcher(self):
        watcher = self.module.RecordingWatcher(self.root)
        self.addCleanup(watcher.close)
        self.assertFalse(watcher.poll())
        return watcher

    def test_real_file_creation_modification_and_deletion(self):
        watcher = self.watcher()
        clip = self.root / 'clip.mp4'
        clip.write_bytes(b'video')
        self.assertTrue(watcher.poll())
        self.assertFalse(watcher.poll())
        clip.write_bytes(b'changed')
        self.assertTrue(watcher.poll())
        clip.unlink()
        self.assertTrue(watcher.poll())

    def test_directory_creation_and_populated_move_in(self):
        watcher = self.watcher()
        camera = self.root / 'camera'
        camera.mkdir()
        self.assertTrue(watcher.poll())
        (camera / 'clip.mp4').write_bytes(b'video')
        self.assertTrue(watcher.poll())
        with tempfile.TemporaryDirectory(dir=self.root.parent) as outside:
            incoming = Path(outside) / 'incoming'
            (incoming / 'day/hour').mkdir(parents=True)
            incoming.rename(self.root / 'incoming')
            self.assertTrue(watcher.poll())
            (self.root / 'incoming/day/hour/clip.mp4').write_bytes(b'video')
            self.assertTrue(watcher.poll())

    def test_directory_rename_and_move_to_hidden_stop_old_coverage(self):
        (self.root / 'camera/day').mkdir(parents=True)
        watcher = self.watcher()
        (self.root / 'camera').rename(self.root / 'renamed')
        self.assertTrue(watcher.poll())
        (self.root / 'renamed/day/clip.mp4').write_bytes(b'video')
        self.assertTrue(watcher.poll())
        (self.root / 'renamed').rename(self.root / '.hidden')
        self.assertTrue(watcher.poll())
        (self.root / '.hidden/day/clip.mp4').write_bytes(b'ignored')
        self.assertFalse(watcher.poll())

    def test_hidden_dirs_symlinks_and_reads_do_not_trigger_changes(self):
        outside = self.root / '.outside'
        outside.mkdir()
        (self.root / 'link').symlink_to(outside, target_is_directory=True)
        clip = self.root / 'clip.mp4'
        clip.write_bytes(b'video')
        watcher = self.watcher()
        clip.read_bytes()
        list(self.root.iterdir())
        (outside / 'ignored.mp4').write_bytes(b'ignored')
        self.assertFalse(watcher.poll())

    def test_root_or_ancestor_symlink_is_rejected(self):
        target = self.root / 'target'
        target.mkdir()
        link = self.root / 'link'
        link.symlink_to(target, target_is_directory=True)
        (target / 'sub').mkdir()
        for path in [link, link / 'sub']:
            with self.subTest(path=path), self.assertRaises(OSError):
                self.module.RecordingWatcher(path)

    def test_root_removal_invalidates_watcher(self):
        root = self.root / 'recordings'
        root.mkdir()
        watcher = self.module.RecordingWatcher(root)
        self.addCleanup(watcher.close)
        root.rmdir()
        with self.assertRaises(OSError):
            watcher.poll()


if __name__ == '__main__':
    unittest.main()
