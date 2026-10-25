"""Deterministic single-event-loop admission and budget bookkeeping."""
from collections import deque
from dataclasses import dataclass, field
import math

ROLES = ('prefill', 'decode')
PATCH_ID = 'pdserve-stage2c-guard-v1'


class ServeError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


@dataclass
class Config:
    queue_limit: int = 128
    queue_timeout: float = 30
    short_limit: int = 2048
    long_age: float = 5
    fraction: float = .9
    active_limit: int = 32
    context: int = 16384
    poll_interval: float = .25
    snapshot_timeout: float = 3
    cleanup_timeout: float = 30
    generation_timeout: float = 300
    mode: str = 'fair'
    history: int = 1024
    stream_buffer: int = 128

    def __post_init__(self):
        for name in ('queue_limit', 'short_limit', 'active_limit', 'context', 'history', 'stream_buffer'):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f'{name} must be a positive integer')
        for name in ('queue_timeout', 'long_age', 'fraction', 'poll_interval', 'snapshot_timeout',
                     'cleanup_timeout', 'generation_timeout'):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be positive and finite')
        if self.fraction > .9 or self.mode not in ('fair', 'fifo'):
            raise ValueError('Keep at least 10% spare capacity; mode must be fair or fifo')


def validate_state(state, role):
    guard = state.get('fault_guard') or {}
    if (state.get('patch_id') != PATCH_ID or state.get('role') != role or not state.get('enabled')
            or not state.get('healthy') or not state.get('cleanup_confirmation_supported')
            or not state.get('cancellation_supported') or not guard.get('enabled')
            or guard.get('quarantined') or guard.get('test_hooks_enabled')):
        raise ServeError(503, f'{role}: healthy Stage 2c1 observer/lifecycle/cancel/guard with test hooks disabled required')
    for name in ('kv_tokens', 'page_size', 'request_slots', 'metadata_slots', 'kv_bytes_per_token'):
        value = state['capacity'].get(name)
        if type(value) is not int or value <= 0:
            raise ServeError(503, f'{role}: invalid actual capacity {name}')


def idle(state):
    cap, resources = state['capacity'], state['resources']
    return (resources['kv_free_tokens'] == cap['kv_tokens'] and resources['kv_used_tokens'] == 0
            and resources['kv_evictable_tokens'] == 0
            and resources['request_free_slots'] == cap['request_slots']
            and resources['metadata_free_slots'] == cap['metadata_slots']
            and all(value == 0 for value in resources['queues'].values())
            and all(resources[key] == 0 for key in ('transport_status_entries', 'transport_info_entries', 'transport_queued_chunks'))
            and state['transport_lifecycle']['active_chunks'] == 0
            and state['transport_lifecycle']['pending_chunks'] == 0
            and state['pending_cancels'] == 0)


@dataclass
class Node:
    epoch: str
    capacity: dict
    token_limit: int
    slot_limit: int
    used_tokens: int = 0
    used_slots: int = 0
    leases: dict = field(default_factory=dict)


class Ledger:
    def __init__(self, states, config):
        self.nodes = {}
        for role in ROLES:
            state = states[role]
            validate_state(state, role)
            cap = dict(state['capacity'])
            tokens = math.floor(cap['kv_tokens'] * config.fraction / cap['page_size']) * cap['page_size']
            slots = min(config.active_limit, math.floor(cap['request_slots'] * config.fraction),
                        math.floor(cap['metadata_slots'] * config.fraction))
            if tokens < cap['page_size'] or slots < 1:
                raise ServeError(503, 'No usable capacity after spare margin')
            self.nodes[role] = Node(state['epoch'], cap, tokens, slots)

    def demand(self, prompt_tokens, output_tokens):
        return {role: math.ceil((prompt_tokens + (output_tokens if role == 'decode' else 0)) /
                               node.capacity['page_size']) * node.capacity['page_size']
                for role, node in self.nodes.items()}

    def fits(self, job):
        demand = self.demand(len(job.input_ids), job.output_limit)
        return all(node.used_tokens + demand[role] <= node.token_limit and node.used_slots < node.slot_limit
                   for role, node in self.nodes.items())

    def check_single(self, job):
        if any(value > self.nodes[role].token_limit for role, value in
               self.demand(len(job.input_ids), job.output_limit).items()):
            raise ServeError(413, 'Request exceeds a single-node KV budget including output reservation')

    def reserve(self, job):
        # No await: P and D reservations form one atomic event-loop operation.
        if any(job.engine_id in node.leases for node in self.nodes.values()):
            raise ValueError('Duplicate reservation')
        if not self.fits(job):
            raise ValueError('Insufficient paired budget')
        for role, tokens in self.demand(len(job.input_ids), job.output_limit).items():
            node = self.nodes[role]
            node.leases[job.engine_id] = tokens
            node.used_tokens += tokens
            node.used_slots += 1

    def release_ack(self, role, job, epoch, record):
        node = self.nodes[role]
        if job.engine_id not in node.leases:
            return False
        if (epoch != node.epoch or record.get('rid') != job.engine_id or record.get('bootstrap_room') != job.room
                or record.get('cleanup_confirmed') is not True or record.get('compute_pending') != 0
                or not record.get('allocator_release_seen') or not record.get('metadata_release_seen')
                or record.get('cleanup_scope') not in ('normal_success', 'cooperative_cancel')):
            raise ValueError('Unmatched or incomplete engine cleanup acknowledgement')
        tokens = node.leases.pop(job.engine_id)
        node.used_tokens -= tokens
        node.used_slots -= 1
        return True

    def held(self, job):
        return any(job.engine_id in node.leases for node in self.nodes.values())

    def snapshot(self):
        return {role: {'epoch': node.epoch, 'actual_capacity': node.capacity,
                      'token_limit': node.token_limit, 'slot_limit': node.slot_limit,
                      'reserved_tokens': node.used_tokens, 'reserved_slots': node.used_slots}
                for role, node in self.nodes.items()}


class WaitQueue:
    def __init__(self, config):
        self.config = config
        self.queues = {'short': deque(), 'long': deque()}
        self.cursor = 0

    def __len__(self):
        return sum(map(len, self.queues.values()))

    def add(self, job):
        if len(self) >= self.config.queue_limit:
            raise ServeError(429, 'Waiting queue is full')
        self.queues[job.category].append(job)

    def remove(self, job):
        queue = self.queues[job.category]
        if job in queue:
            queue.remove(job)

    def expired(self, now):
        return [job for queue in self.queues.values() for job in queue
                if now - job.enqueued_at >= self.config.queue_timeout]

    def choose(self, ledger, now):
        heads = {kind: queue[0] for kind, queue in self.queues.items() if queue}
        if not heads:
            return None
        if self.config.mode == 'fifo':
            first = min(heads.values(), key=lambda job: job.sequence)
            return first if ledger.fits(first) else None
        long = heads.get('long')
        if long and now - long.enqueued_at >= self.config.long_age and ledger.fits(long):
            self.cursor = 0
            return long
        cycle = ('short', 'short', 'short', 'long')
        for offset in range(4):
            position = (self.cursor + offset) % 4
            candidate = heads.get(cycle[position])
            if candidate and ledger.fits(candidate):
                self.cursor = (position + 1) % 4
                return candidate
        return None
