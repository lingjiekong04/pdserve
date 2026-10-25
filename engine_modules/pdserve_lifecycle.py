"""Bounded successful-transfer retirement; cancellation is deliberately unsupported."""
import os
import threading
import time
from contextlib import nullcontext


class TransportLifecycle:
    def __init__(self, role, limit=65536):
        self.role = role
        self.limit = limit
        self.lock = threading.RLock()
        self.work = {}
        self.retired = set()
        self.late_messages = 0
        self.error = None

    def accepts(self, room):
        # Caller holds lock around associated manager-map access.
        if room in self.retired:
            self.late_messages += 1
            return False
        return True

    def submitted(self, room):
        with self.lock:
            if not self.accepts(room):
                self.error = 'Attempt to submit work for a retired room'
                raise RuntimeError(self.error)
            row = self.work.setdefault(room, dict(pending=0, active=0, final_done=False))
            row['pending'] += 1

    def started(self, room):
        with self.lock:
            row = self.work.get(room)
            if row is None or row['pending'] <= row['active']:
                self.error = 'Transfer worker started without a submitted chunk'
                raise RuntimeError(self.error)
            row['active'] += 1
            if row['active'] == 1:
                row['active_started_ns'] = time.monotonic_ns()

    def completed(self, room, final, success, exception=False):
        with self.lock:
            row = self.work.get(room)
            if row is None or row['active'] <= 0 or row['pending'] <= 0:
                self.error = 'Transfer worker completion accounting mismatch'
                return
            row['active'] -= 1
            if row['active'] == 0:
                row['active_started_ns'] = None
            row['pending'] -= 1
            if exception:
                self.error = 'Transfer worker exception; successful-cleanup path unavailable'
            if final and success and not exception:
                row['final_done'] = True

    def retire(self, manager, room):
        with self.lock:
            if room in self.retired:
                return True  # Idempotent, no repeated allocator mutation.
            if self.error is not None or manager.request_status.get(room) != 4:
                return False
            if room in getattr(manager, 'transfer_infos', {}):
                return False
            if self.role == 'prefill':
                row = self.work.get(room)
                if not row or row['pending'] or row['active'] or not row['final_done']:
                    return False
            if len(self.retired) >= self.limit:
                self.error = 'Retired-room limit reached; restart required before more cleanup acknowledgements'
                return False
            # Keep tombstones for the entire engine epoch: late notifications
            # cannot resurrect retired request_status or transfer_infos entries.
            self.retired.add(room)
            manager.request_status.pop(room, None)
            self.work.pop(room, None)
            return True

    def snapshot(self):
        with self.lock:
            return dict(retired_rooms=len(self.retired), late_messages=self.late_messages,
                        pending_chunks=sum(row['pending'] for row in self.work.values()),
                        active_chunks=sum(row['active'] for row in self.work.values()),
                        healthy=self.error is None, error=self.error, retired_room_limit=self.limit)


def init_transport(role):
    if os.getenv('PDSERVE_ENGINE_LIFECYCLE', '0') != '1':
        return None
    if os.getenv('PDSERVE_ENGINE_OBSERVER', '0') != '1':
        raise ValueError('Lifecycle requires PDSERVE_ENGINE_OBSERVER=1')
    limit = int(os.getenv('PDSERVE_TRANSPORT_RETIRE_LIMIT', '65536'))
    if limit <= 0:
        raise ValueError('Retired room limit must be positive')
    return TransportLifecycle(role, limit)


def submitted(manager, room):
    tracker = getattr(manager, 'pdserve_transport', None)
    if tracker is not None:
        tracker.submitted(room)


def guard(manager):
    tracker = getattr(manager, 'pdserve_transport', None)
    return tracker.lock if tracker is not None else nullcontext()


def accepts(manager, room):
    tracker = getattr(manager, 'pdserve_transport', None)
    return tracker.accepts(room) if tracker is not None else True


def started(manager, room):
    tracker = getattr(manager, 'pdserve_transport', None)
    if tracker is not None:
        tracker.started(room)


def completed(manager, chunk, exception=False):
    tracker = getattr(manager, 'pdserve_transport', None)
    if tracker is not None:
        tracker.completed(chunk.room, chunk.is_last,
                          manager.request_status.get(chunk.room) == 4, exception)
