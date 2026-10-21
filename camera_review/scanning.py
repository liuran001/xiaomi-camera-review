"""Event-driven scans, with a one-shot check when an upload becomes stable."""
import logging
import time

from .watcher import RecordingWatcher

LOG = logging.getLogger('camera-review')


class ScanScheduler:
    def __init__(self, catalog, interval, watcher_factory=RecordingWatcher):
        self.cat, self.interval = catalog, interval
        self.factory = watcher_factory
        self.watcher = None
        self.last_scan = None
        self.dirty = True
        self.stable_at = None
        self.retry_scan = False

    def tick(self):
        now = time.monotonic()
        due = self.last_scan is None or now - self.last_scan >= self.interval
        if self.watcher is None and due:
            try:
                self.watcher = self.factory(self.cat.root)
                self.dirty = True  # Reconcile the time without notification coverage.
                LOG.info('recording notifications enabled')
            except (OSError, NotImplementedError):
                LOG.warning('recording notifications unavailable; using periodic scans')
        if self.watcher is not None:
            try:
                self.dirty = self.watcher.poll() or self.dirty
            except OSError:
                LOG.warning('recording notifications lost; rescan and retry', exc_info=True)
                self.close()
                self.dirty = True
        stable_due = self.stable_at is not None and now >= self.stable_at
        if not due or not (self.dirty or stable_due or self.retry_scan or self.watcher is None):
            return False
        # Preserve a retry if scan/query raises partway through reconciliation.
        self.retry_scan = True
        self.last_scan = now
        success = self.cat.scan()
        if success:
            deadline = self.cat.next_stability_check()
            self.stable_at = None if deadline is None else time.monotonic() + max(0, deadline - time.time())
            self.dirty = self.retry_scan = False
        return True

    def close(self):
        if self.watcher is not None:
            self.watcher.close()
            self.watcher = None
