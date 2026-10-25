"""Fail closed at scheduler checkpoints; this module never frees KV or retries."""
import math
import os
import threading
import time


class FaultGuard:
    def __init__(self, observer, transfer_timeout=30, kv_ready_timeout=60):
        if not all(math.isfinite(value) and value > 0 for value in (transfer_timeout, kv_ready_timeout)):
            raise ValueError('Fault deadlines must be positive finite seconds')
        self.observer = observer
        self.transfer_timeout = transfer_timeout
        self.kv_ready_timeout = kv_ready_timeout
        self.lock = threading.RLock()
        self.fault = None
        self.fault_event_emitted = False
        self.test_delayed = set()

    def quarantine(self, reason, room=None):
        with self.lock:
            if self.fault is not None:
                return
            self.fault = {'reason': reason, 'room': room, 'monotonic_ns': time.monotonic_ns(),
                          'recovery_requires_new_epoch': True}
        # Do not hold this lock when acquiring the observer lock.
        self.observer.error = 'Fault quarantine: ' + reason
        # Transport callbacks may hold tracker.lock. Emit from scheduler tick,
        # after releasing transport locks, so callbacks never acquire observer.lock.

    def publish_fault(self):
        with self.lock:
            if self.fault is None or self.fault_event_emitted:
                return
            fault = dict(self.fault)
            self.fault_event_emitted = True
        if fault['room'] is not None:
            self.observer.room_trace(fault['room'], 'fault_quarantined', reason=fault['reason'],
                                     allocation_release_confirmed=False,
                                     fault_monotonic_ns=fault['monotonic_ns'])

    def snapshot(self):
        with self.lock:
            return {'enabled': True, 'quarantined': self.fault is not None,
                    'fault': dict(self.fault) if self.fault is not None else None,
                    'transfer_worker_timeout_s': self.transfer_timeout,
                    'decode_kv_ready_timeout_s': self.kv_ready_timeout,
                    'test_hooks_enabled': os.getenv('PDSERVE_ENABLE_TEST_HOOKS', '0') == '1',
                    'automatic_recovery_supported': False,
                    'scope': 'local scheduler quarantine; peer monitoring and coordinated recovery pending'}

    def test_timeout(self, room):
        with self.observer.lock:
            rid = self.observer.rooms.get(room, '')
        return (os.getenv('PDSERVE_ENABLE_TEST_HOOKS', '0') == '1'
                and rid.startswith('pdserve-fault-timeout-'))

    def controlled_delay(self, room):
        if not self.test_timeout(room):
            return
        with self.lock:
            if room in self.test_delayed:
                return
            self.test_delayed.add(room)
        self.observer.room_trace(room, 'test_delay_fault_transfer', milliseconds=1000, test_only=True)
        time.sleep(1)

    def tick(self):
        if self.fault is not None:
            self.publish_fault()
            return True
        observer = self.observer
        if observer.error is not None:
            self.quarantine('Observer unhealthy: ' + observer.error)
            self.publish_fault()
            return True
        manager = observer.manager
        tracker = getattr(manager, 'pdserve_transport', None)
        if tracker is None:
            self.quarantine('Transport tracker missing')
            self.publish_fault()
            return True
        with tracker.lock:
            error = tracker.error
            active = [(room, row.get('active_started_ns')) for room, row in tracker.work.items() if row['active']]
        if error:
            self.quarantine(error)
            self.publish_fault()
            return True
        now = time.monotonic_ns()
        if observer.role == 'prefill':
            for room, began in active:
                deadline = 0.2 if self.test_timeout(room) else self.transfer_timeout
                if began is not None and now - began > deadline * 1e9:
                    self.quarantine('Prefill transfer worker deadline exceeded', room)
                    self.publish_fault()
                    return True
        else:
            with observer.lock:
                waiting = [(record['bootstrap_room'], record['first_stage_ns'].get('kv_preallocated'))
                           for record in observer.records.values()
                           if not record['terminal_observed'] and 'kv_ready' not in record['first_stage_ns']]
            for room, began in waiting:
                deadline = 0.5 if self.test_timeout(room) else self.kv_ready_timeout
                if began is not None and now - began > deadline * 1e9:
                    self.quarantine('Decode preallocated KV-ready deadline exceeded', room)
                    self.publish_fault()
                    return True
        return False


def init_fault_guard(observer):
    if os.getenv('PDSERVE_ENGINE_FAULT_GUARD', '0') != '1':
        return None
    if not observer.lifecycle_enabled:
        raise ValueError('Fault guard requires lifecycle observation')
    return FaultGuard(observer, float(os.getenv('PDSERVE_TRANSFER_TIMEOUT_S', '30')),
                      float(os.getenv('PDSERVE_KV_READY_TIMEOUT_S', '60')))


def pause_dispatch(scheduler):
    observer = getattr(scheduler, 'pdserve_observer', None)
    guard = getattr(observer, 'fault_guard', None)
    return bool(guard and guard.tick())


def hold_failed(scheduler, req):
    observer = getattr(scheduler, 'pdserve_observer', None)
    guard = getattr(observer, 'fault_guard', None)
    if guard is None:
        return False
    guard.quarantine('Transport reported Failed; other layer writes may still be active', req.bootstrap_room)
    return True


def sticky_failure(manager, room, status):
    observer = getattr(manager, 'pdserve_observer', None)
    guard = getattr(observer, 'fault_guard', None)
    if guard is None:
        return False
    current = manager.request_status.get(room)
    if status == 0 or current == 0:
        manager.request_status[room] = 0
        guard.quarantine('Transport reported Failed; other layer writes may still be active', room)
        return True
    return False


def controlled_fault_delay(manager, room):
    guard = getattr(getattr(manager, 'pdserve_observer', None), 'fault_guard', None)
    if guard is not None:
        guard.controlled_delay(room)
