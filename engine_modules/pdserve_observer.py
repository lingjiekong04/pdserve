"""Opt-in PDServe observation for the pinned SGLang build; no allocator mutation.

Scheduler snapshots run on the scheduler thread. Mooncake worker events acquire
the same observer lock but never read allocator state. A terminal response is
not a cleanup acknowledgement. Normal-success cleanup requires explicit release and compute/transport completion hooks.
"""
from collections import OrderedDict, deque
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid
from sglang.srt.pdserve_fault_guard import init_fault_guard

PATCH_ID = "pdserve-stage2c-guard-v1"
CANCEL_MESSAGE = "PDServe cooperatively cancelled"


class PDObserver:
    def __init__(self, scheduler, log_dir=None, limit=4096, lifecycle_enabled=False, cancel_enabled=False):
        self.scheduler = scheduler
        self.lifecycle_enabled = lifecycle_enabled
        self.cancel_enabled = cancel_enabled
        self.fault_guard = None
        self.live_requests = {}
        self.pending_cancels = {}
        self.pending_cancel_limit = 128
        self.cancel_rejections = 0
        self.cancel_expirations = 0
        self.role = scheduler.server_args.disaggregation_mode
        self.epoch = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.limit = limit
        self.records = OrderedDict()
        self.rooms = {}
        self.events = deque(maxlen=limit)
        self.sequence = 0
        self.dropped_events = 0
        self.error = None
        self.log = None
        if log_dir:
            path = Path(log_dir)
            path.mkdir(parents=True, exist_ok=True)
            self.log = (path / f"{self.role}-{os.getpid()}-{self.epoch}.jsonl").open(
                "a", encoding="utf-8", buffering=1
            )
        queue = getattr(scheduler, "disagg_prefill_bootstrap_queue", None)
        if queue is None:
            queue = getattr(scheduler, "disagg_decode_prealloc_queue", None)
        self.manager = queue.kv_manager if queue is not None else None
        if queue is not None:
            queue.kv_manager.pdserve_observer = self
        self.fault_guard = init_fault_guard(self)

    def _emit(self, record, stage, details):
        self.sequence += 1
        event = dict(patch_id=PATCH_ID, epoch=self.epoch, seq=self.sequence,
                     role=self.role, rid=record["rid"], bootstrap_room=record["bootstrap_room"],
                     stage=stage, monotonic_ns=time.monotonic_ns(), unix_ns=time.time_ns(),
                     details=details)
        if len(self.events) == self.events.maxlen:
            self.dropped_events += 1
        self.events.append(event)
        record["stage"] = stage
        record["last_seq"] = self.sequence
        record["first_stage_ns"].setdefault(stage, event["monotonic_ns"])
        if self.log:
            try:
                self.log.write(json.dumps(event, ensure_ascii=False) + "\n")
            except OSError as error:
                self.error = f"Event log write failed: {error}"

    def trace(self, req, stage, repeat=False, **details):
        if getattr(req, "bootstrap_host", None) == "2.2.2.2":
            return  # Stock health/warmup uses fake KV transfers.
        with self.lock:
            rid = req.rid
            record = self.records.get(rid)
            if record is None:
                if len(self.records) >= self.limit:
                    old = next((key for key, obj in self.records.items()
                                if (obj["cleanup_confirmed"] if self.lifecycle_enabled
                                    else obj["terminal_observed"])), None)
                    if old is None:
                        self.error = "Active request observation limit exceeded"
                        return
                    previous = self.records.pop(old)
                    self.live_requests.pop(old, None)
                    if self.rooms.get(previous["bootstrap_room"]) == old:
                        self.rooms.pop(previous["bootstrap_room"], None)
                record = dict(rid=rid, bootstrap_room=getattr(req, "bootstrap_room", None),
                              prompt_tokens=len(req.origin_input_ids),
                              output_limit=req.sampling_params.max_new_tokens,
                              first_stage_ns={}, terminal_observed=False,
                              cleanup_confirmed=False, compute_pending=0, last_compute_result_ns=None,
                              allocator_release_seen=False, metadata_release_seen=False,
                              finish_reason=None, cancel_requested=False, cancel_issued_ns=None,
                              cleanup_scope=None)
                owner = self.rooms.get(record["bootstrap_room"])
                if self.lifecycle_enabled and owner is not None and owner != rid:
                    self.error = "Bootstrap room reused by a different request"
                    return
                self.records[rid] = record
                self.rooms[record["bootstrap_room"]] = rid
            elif record["bootstrap_room"] != getattr(req, "bootstrap_room", None):
                self.error = "Request ID reused with a different bootstrap room"
                return
            if not repeat and stage in record["first_stage_ns"]:
                return
            if stage == "terminal":
                record["terminal_observed"] = True
                details["finish_reason"] = req.finished_reason.to_json()
                record["finish_reason"] = details["finish_reason"]
            if stage == "allocator_released":
                record["allocator_release_seen"] = True
            if stage == "metadata_released":
                record["metadata_release_seen"] = True
            self._emit(record, stage, details)
            if self.cancel_enabled:
                if not record["cleanup_confirmed"]:
                    self.live_requests[rid] = req
                if stage == "received":
                    self.expire_pending_cancels()
                    pending = self.pending_cancels.pop(rid, None)
                    if pending is not None:
                        self._cancel_known(record, req, pending["issued_ns"])
                elif stage == "kv_ready" and record["cancel_requested"]:
                    self._arm_decode_cancel(record, req)

    def room_trace(self, room, stage, **details):
        with self.lock:
            rid = self.rooms.get(room)
            if rid is not None and rid in self.records:
                self._emit(self.records[rid], stage, details)

    def expire_pending_cancels(self):
        now = time.monotonic_ns()
        with self.lock:
            expired = [rid for rid, row in self.pending_cancels.items() if row["expires_ns"] <= now]
            for rid in expired:
                self.pending_cancels.pop(rid)
                self.cancel_expirations += 1

    def _arm_decode_cancel(self, record, req):
        if self.role == "decode" and "kv_ready" in record["first_stage_ns"]:
            req.to_abort = True
            req.to_abort_message = CANCEL_MESSAGE
            if "cancel_armed" not in record["first_stage_ns"]:
                self._emit(record, "cancel_armed", {"kv_transfer_success_observed": True})

    def _cancel_known(self, record, req, issued_ns):
        if record["terminal_observed"] or record["cleanup_confirmed"] or req.finished():
            self._emit(record, "cancel_too_late", {"issued_ns": issued_ns})
            return "too_late"
        if record["cancel_requested"]:
            self._emit(record, "cancel_duplicate", {"issued_ns": issued_ns})
            return "already_requested"
        record["cancel_requested"] = True
        record["cancel_issued_ns"] = issued_ns
        self._emit(record, "cancel_requested", {"issued_ns": issued_ns, "mode": "drain_prefill_and_transfer"})
        self._arm_decode_cancel(record, req)
        return "requested"

    def request_cancel(self, rid, issued_ns=0):
        if not self.cancel_enabled:
            return "unsupported"
        if not isinstance(rid, str) or not rid or len(rid) > 128:
            self.cancel_rejections += 1
            return "invalid_id"
        self.expire_pending_cancels()
        issued_ns = issued_ns or time.monotonic_ns()
        with self.lock:
            record = self.records.get(rid)
            if record is not None:
                req = self.live_requests.get(rid)
                if req is None:
                    self._emit(record, "cancel_too_late", {"issued_ns": issued_ns})
                    return "too_late"
                return self._cancel_known(record, req, issued_ns)
            if rid in self.pending_cancels:
                return "already_pending"
            if len(self.pending_cancels) >= self.pending_cancel_limit:
                self.cancel_rejections += 1
                return "pending_limit"
            self.pending_cancels[rid] = {"issued_ns": issued_ns,
                                         "expires_ns": time.monotonic_ns() + 60_000_000_000}
            return "pending_registration"

    def controlled_test_delay(self, rid, phase):
        # Only deterministic cancellation demos opt in. Never enabled in benchmarks.
        if (os.getenv("PDSERVE_ENABLE_TEST_HOOKS", "0") == "1" and
                rid.startswith("pdserve-cancel-" + phase + "-")):
            with self.lock:
                record = self.records.get(rid)
                marker = "test_delay_" + phase
                if record is None or marker in record["first_stage_ns"]:
                    return
                milliseconds = int(os.getenv("PDSERVE_TEST_STAGE_DELAY_MS", "200"))
                if not 1 <= milliseconds <= 1000:
                    raise ValueError("Test delay must be between 1 and 1000 ms")
                self._emit(record, marker, {"milliseconds": milliseconds, "test_only": True})
            time.sleep(milliseconds / 1000)

    def batch_start(self, batch):
        if self.role == "prefill" and batch.forward_mode.is_extend():
            stage = "prefill_started"
        elif self.role == "decode" and batch.forward_mode.is_decode():
            stage = "decode_started"
        else:
            return
        for req in batch.reqs or []:
            self.trace(req, stage)
            if self.lifecycle_enabled:
                with self.lock:
                    record = self.records.get(req.rid)
                    if record is not None:
                        if record["cleanup_confirmed"]:
                            self.error = "Compute submitted after cleanup acknowledgement"
                        record["compute_pending"] += 1
            if self.role == "prefill":
                self.controlled_test_delay(req.rid, "prefill")

    def batch_done(self, batch):
        if not self.lifecycle_enabled:
            return
        with self.lock:
            for req in batch.reqs or []:
                record = self.records.get(req.rid)
                if record is not None:
                    if getattr(req, "is_retracted", False):
                        self.error = "Retracted request requires a separate cleanup path"
                    if record["compute_pending"] <= 0:
                        self.error = "Compute result accounting mismatch"
                    else:
                        record["compute_pending"] -= 1
                        record["last_compute_result_ns"] = time.monotonic_ns()
                        if record["terminal_observed"] or record["allocator_release_seen"]:
                            self._emit(record, "compute_result_processed",
                                       {"compute_pending": record["compute_pending"]})

    def settle_cleanup(self):
        self.expire_pending_cancels()
        if not self.lifecycle_enabled or self.error is not None:
            return
        tracker = getattr(self.manager, "pdserve_transport", None)
        if tracker is None:
            self.error = "Lifecycle transport tracker missing"
            return
        with self.lock:
            candidates = [record for record in self.records.values()
                          if record["terminal_observed"] and not record["cleanup_confirmed"]
                          and record["allocator_release_seen"]
                          and record["metadata_release_seen"]
                          and record["compute_pending"] == 0
                          and ((record["finish_reason"] or {}).get("type") in ("length", "stop")
                               or (self.cancel_enabled and record["cancel_requested"]
                                   and (record["finish_reason"] or {}).get("type") == "abort"
                                   and (record["finish_reason"] or {}).get("message") == CANCEL_MESSAGE))]
        # Never hold observer lock while obtaining the transport lock.
        for record in candidates:
            if tracker.retire(self.manager, record["bootstrap_room"]):
                with self.lock:
                    record["cleanup_confirmed"] = True
                    record["cleanup_scope"] = "cooperative_cancel" if record["cancel_requested"] else "normal_success"
                    self.live_requests.pop(record["rid"], None)
                    self._emit(record, "cleanup_confirmed", {
                        "scope": record["cleanup_scope"], "compute_pending": 0,
                        "allocator_release_seen": True, "metadata_release_seen": True,
                        "transport_retired": True})

    def _resources(self):
        s = self.scheduler
        allocator = s.token_to_kv_pool_allocator
        free = int(allocator.available_size())
        evictable = int(s.tree_cache.evictable_size())
        pool = allocator.get_kvcache()
        buffers = list(getattr(pool, "k_buffer", [])) + list(getattr(pool, "v_buffer", []))
        bytes_per_token = sum(math.prod(buffer.shape[1:]) * buffer.element_size()
                              for buffer in buffers) if buffers else None
        capacity = dict(kv_tokens=int(s.max_total_num_tokens), page_size=int(allocator.page_size),
                        kv_bytes_per_token=bytes_per_token,
                        request_slots=int(s.req_to_token_pool.size))
        queues = dict(waiting=len(s.waiting_queue), grammar=len(s.grammar_queue),
                      running=len(s.running_batch.reqs))
        role_queue = getattr(s, "disagg_prefill_bootstrap_queue", None)
        if role_queue is not None:
            queues.update(prefill_bootstrap=len(role_queue.queue),
                          prefill_inflight=len(s.disagg_prefill_inflight_queue))
        else:
            role_queue = s.disagg_decode_prealloc_queue
            queues.update(decode_prealloc=len(role_queue.queue),
                          decode_transfer=len(s.disagg_decode_transfer_queue.queue))
        metadata = role_queue.req_to_metadata_buffer_idx_allocator
        capacity["metadata_slots"] = int(metadata.size)
        manager = role_queue.kv_manager
        transfer_queue = getattr(manager, "transfer_queue", None)
        resources = dict(kv_free_tokens=free, kv_evictable_tokens=evictable,
                         kv_used_tokens=capacity["kv_tokens"] - free - evictable,
                         request_free_slots=len(s.req_to_token_pool.free_slots),
                         metadata_free_slots=int(metadata.available_size()), queues=queues,
                         # Manager maps are touched by worker threads; these are point observations,
                         # not a transport quiescence or per-request cleanup acknowledgement.
                         transport_status_entries=len(manager.request_status),
                         transport_info_entries=len(getattr(manager, "transfer_infos", {})),
                         transport_queued_chunks=transfer_queue.qsize() if transfer_queue else 0)
        return capacity, resources

    def snapshot(self):
        capacity, resources = self._resources()
        tracker = getattr(self.manager, "pdserve_transport", None)
        transport = tracker.snapshot() if tracker is not None else None
        with self.lock:
            return dict(enabled=True, patch_id=PATCH_ID, epoch=self.epoch, role=self.role,
                        captured_monotonic_ns=time.monotonic_ns(), capacity=capacity, resources=resources,
                        healthy=self.error is None and (transport is None or transport["healthy"]),
                        observer_error=self.error or (transport or {}).get("error"),
                        cleanup_confirmation_supported=self.lifecycle_enabled,
                        cleanup_confirmation_scope=("normal_success_and_cooperative_cancel"
                                                    if self.cancel_enabled else "normal_success")
                                                   if self.lifecycle_enabled else None,
                        fault_guard=self.fault_guard.snapshot() if self.fault_guard else None,
                        cancellation_supported=self.cancel_enabled,
                        cancellation_scope="drain_prefill_and_transfer" if self.cancel_enabled else None,
                        pending_cancels=len(self.pending_cancels), cancel_rejections=self.cancel_rejections,
                        cancel_expirations=self.cancel_expirations, transport_lifecycle=transport,
                        events_oldest_seq=self.events[0]["seq"] if self.events else self.sequence + 1,
                        events_latest_seq=self.sequence, events_overwritten=self.dropped_events,
                        requests=json.loads(json.dumps(list(self.records.values()))),
                        events=json.loads(json.dumps(list(self.events))))


def init_observer(scheduler):
    if os.getenv("PDSERVE_ENGINE_OBSERVER", "0") != "1":
        return None
    args = scheduler.server_args
    if (args.tp_size, args.pp_size, args.dp_size) != (1, 1, 1):
        raise ValueError("PDServe observer currently requires TP=PP=DP=1")
    if args.disaggregation_mode not in ("prefill", "decode") or not args.disable_radix_cache:
        raise ValueError("PDServe observer requires P/D mode with prefix cache disabled")
    lifecycle = os.getenv("PDSERVE_ENGINE_LIFECYCLE", "0") == "1"
    cancel = os.getenv("PDSERVE_ENGINE_CANCEL", "0") == "1"
    if cancel and not lifecycle:
        raise ValueError("Cooperative cancellation requires lifecycle enabled")
    if lifecycle and (scheduler.token_to_kv_pool_allocator.page_size != 1
                      or getattr(args, "speculative_algorithm", None) is not None):
        raise ValueError("Normal lifecycle currently requires page_size=1 and no speculative decoding")
    return PDObserver(scheduler, os.getenv("PDSERVE_ENGINE_EVENT_LOG_DIR",
                                         str(Path.cwd() / "logs/engine-events")),
                      lifecycle_enabled=lifecycle, cancel_enabled=cancel)


def observe(scheduler, req, stage, repeat=False, **details):
    observer = getattr(scheduler, "pdserve_observer", None)
    if observer is not None:
        observer.trace(req, stage, repeat=repeat, **details)


def observe_batch(scheduler, batch):
    observer = getattr(scheduler, "pdserve_observer", None)
    if observer is not None:
        observer.batch_start(batch)


def observe_room(manager, room, stage, **details):
    observer = getattr(manager, "pdserve_observer", None)
    if observer is not None:
        observer.room_trace(room, stage, **details)


def batch_result_done(scheduler, batch):
    observer = getattr(scheduler, "pdserve_observer", None)
    if observer is not None:
        observer.batch_done(batch)


def settle_cleanup(scheduler):
    observer = getattr(scheduler, "pdserve_observer", None)
    if observer is not None:
        observer.settle_cleanup()


def cancellation_requested(scheduler, req):
    observer = getattr(scheduler, "pdserve_observer", None)
    return bool(observer and observer.cancel_enabled and
                observer.records.get(req.rid, {}).get("cancel_requested"))


def controlled_transfer_delay(manager, room):
    observer = getattr(manager, "pdserve_observer", None)
    if observer is not None:
        rid = observer.rooms.get(room)
        if rid is not None:
            observer.controlled_test_delay(rid, "transfer")
