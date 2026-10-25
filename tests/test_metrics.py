"""CPU-only deterministic trace and honest streaming metric tests."""
import copy
from pathlib import Path
import unittest

from analyze import analyze, percentile, request_metrics
from aggregate import aggregate
from client import compact_state, failure_path, idle
from make_trace import build
from benchmark_config import MIXED_RESOURCE_POLL_S,SNAPSHOT_TIMEOUT_S
from campaign import fingerprint,valid_name
from run_pilot import validate_pilot_summary, validate_stream_measurement


class MetricTests(unittest.TestCase):
    def test_trace_is_deterministic_and_exact_mix(self):
        first = build(10, 80, .25, 2026)
        self.assertEqual(first, build(10, 80, .25, 2026))
        self.assertEqual(sum(r['class'] == 'short' for r in first['requests']), 8)
        self.assertEqual(sum(r['class'] == 'long' for r in first['requests']), 2)
        self.assertEqual({r['output_tokens'] for r in first['requests']}, {128, 512})

    def test_linear_percentile(self):
        self.assertEqual(percentile([1, 2, 3, 4], .5), 2.5)
        self.assertIsNone(percentile([], .5))

    def row(self, deltas=(1, 1, 1)):
        counts = []
        total = 0
        for delta in deltas:
            total += delta; counts.append(total)
        frames = [{'arrival_ns': 2_000_000_000 + i * 100_000_000,
                   'completion_tokens': count, 'usage': {'completion_tokens': count}}
                  for i, count in enumerate(counts)]
        return {'request_id': 'r', 'class': 'short', 'input_tokens': 512,
                'output_tokens': total, 'status': 200, 'done': True, 'error': None,
                'scheduled_ns': 900_000_000, 'sent_ns': 1_000_000_000,
                'ended_ns': 2_400_000_000, 'client_lateness_s': .1, 'frames': frames}

    def test_itl_uses_only_single_token_frame_increments(self):
        value = request_metrics(self.row((1, 2, 1)), [])
        self.assertEqual(value['single_token_intervals'], 1)
        self.assertEqual(value['batched_token_increments'], 2)
        self.assertEqual(len(value['itl_s']), 1)

    def test_queue_time_comes_from_gateway_events(self):
        events = [{'event': 'queued', 'request_id': 'r', 'monotonic_ns': 10},
                  {'event': 'dispatched', 'request_id': 'r', 'monotonic_ns': 110}]
        self.assertEqual(request_metrics(self.row(), events)['queue_s'], 1e-7)

    def test_failed_stream_not_counted_as_throughput(self):
        row = self.row(); row['done'] = False
        raw = {'topology': 'x', 'repeat': 1, 'trace_sha256': 'x',
               'started_ns': 0, 'ended_ns': row['ended_ns'], 'gateway_events': [],
               'requests': [row]}
        value = analyze(raw)
        self.assertEqual(value['groups']['all']['completed'], 0)
        self.assertEqual(value['completed_throughput_rps'], 0)

    def test_native_phase_windows_use_engine_clocks(self):
        events=[]
        for role,stages in {'prefill':[('prefill_started',10),('prefill_completed',30),('transfer_completed',50)],
                            'decode':[('kv_preallocated',5),('kv_ready',15),('decode_started',20),('terminal',80)]}.items():
            for stage,stamp in stages:
                events.append({'event':'engine_event','request_id':'r','native':{
                    'role':role,'stage':stage,'monotonic_ns':stamp}})
        value=request_metrics(self.row(),events)
        self.assertEqual(value['prefill_s'],2e-8)
        self.assertEqual(value['transfer_s'],2e-8)
        self.assertEqual(value['decode_kv_wait_s'],1e-8)
        self.assertEqual(value['decode_s'],6e-8)

    def test_aggregate_combines_request_rows_not_percentiles(self):
        raw={'topology':'x','repeat':1,'trace_sha256':'same','started_ns':0,
             'ended_ns':3_000_000_000,'gateway_events':[],'requests':[self.row()]}
        first=analyze(raw);second=copy.deepcopy(first);second['repeat']=2
        third=copy.deepcopy(first);third['repeat']=3
        value=aggregate('campaign-x','x','case',[first,second,third])
        self.assertEqual(value['campaign'],'campaign-x')
        self.assertEqual(value['groups']['all']['requests'],3)
        self.assertEqual(value['repeats'],3)

    def test_drain_diagnostic_keeps_empty_reason_and_actual_resources(self):
        value=compact_state({'ready':False,'reason':'','queue':0,
            'budget':{'prefill':{'reserved_tokens':0,'reserved_slots':0}},
            'engines':{'prefill':{'resources':{'kv_used_tokens':0,
                'kv_evictable_tokens':0,'queues':{'running':0},
                'transport_status_entries':0,'transport_info_entries':0,
                'transport_queued_chunks':0}}}})
        self.assertFalse(value['ready']);self.assertEqual(value['reason'],'')
        self.assertEqual(value['resources']['prefill']['queues'],{'running':0})

    def test_failed_measurement_uses_nonaccepted_sidecar(self):
        self.assertEqual(failure_path(Path('run/raw.json'),'20261013T010203Z').name,
                         'raw.failed-20261013T010203Z.json')

    def test_pilot_reports_coverage_instead_of_imposing_arbitrary_ratio(self):
        value={'groups':{'all':{'completed':10,'failed':0,'rejected':0,
            'itl_single_token_coverage':.1,'itl_s':{'count':10},
            'batched_token_increments':90}},
            'requests':[{'success':True,'single_token_intervals':1} for _ in range(10)]}
        self.assertTrue(validate_stream_measurement(value))
        value['requests'][0]['single_token_intervals']=0
        with self.assertRaises(ValueError):validate_stream_measurement(value)

    def test_saved_pilot_requires_topology_and_native_phase_evidence(self):
        value={'schema':'pdserve.measurement.summary.v1','topology':'pd_fair',
            'groups':{'all':{'completed':10,'failed':0,'rejected':0,
                'itl_single_token_coverage':.63,'itl_s':{'count':20},
                'batched_token_increments':10,
                'prefill_s':{'count':10},'transfer_s':{'count':10},
                'decode_s':{'count':10}}},
            'requests':[{'success':True,'single_token_intervals':2} for _ in range(10)]}
        self.assertEqual(validate_pilot_summary(value,'pd_fair'),
                         {'prefill_s':10,'transfer_s':10,'decode_s':10})
        value['groups']['all']['transfer_s']['count']=9
        with self.assertRaises(ValueError):validate_pilot_summary(value,'pd_fair')

    def test_mixed_and_pd_benchmarks_share_loaded_snapshot_timeout(self):
        self.assertEqual(SNAPSHOT_TIMEOUT_S,30)
        self.assertEqual(MIXED_RESOURCE_POLL_S,1)

    def test_mixed_idle_does_not_require_pd_only_resource_fields(self):
        value={'ready':True,'queue':0,
            'budget':{'replica0':{'reserved_tokens':0,'reserved_slots':0},
                      'replica1':{'reserved_tokens':0,'reserved_slots':0}},
            'engines':{name:{'resources':{'kv_used_tokens':0,
                'queues':{'running':0,'waiting':0}}}
                for name in ('replica0','replica1')}}
        self.assertTrue(idle(value))
        value['engines']['replica1']['resources']['queues']['running']=1
        self.assertFalse(idle(value))

    def test_campaign_name_and_fingerprint_are_stable(self):
        self.assertEqual(valid_name('stage3d-v2-host1'),'stage3d-v2-host1')
        self.assertEqual(fingerprint({'b':2,'a':1}),fingerprint({'a':1,'b':2}))
        for unsafe in ('stage3d-v1','../escape','UPPER',''):
            with self.assertRaises(ValueError):valid_name(unsafe)


if __name__ == '__main__':
    unittest.main(verbosity=2)
