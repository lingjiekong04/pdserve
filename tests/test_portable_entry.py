"""Portable entry checks never spawn a backend, touch a GPU or signal a PID."""
import ast,importlib.util
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('portable_serve',ROOT/'scripts/serve_backend.py')
serve=importlib.util.module_from_spec(spec);spec.loader.exec_module(serve)
class Tests(unittest.TestCase):
    def test_pd_flags(self):
        cmd,env,port=serve.configuration('prefill',Path('/model'))
        self.assertEqual(port,30000);self.assertIn('mooncake',cmd)
        self.assertEqual(env['PDSERVE_ENABLE_TEST_HOOKS'],'0')
        self.assertEqual(env['CUDA_VISIBLE_DEVICES'],'0,1')
    def test_mixed_flags(self):
        cmd,env,port=serve.configuration('replica1',Path('/model'))
        self.assertEqual(port,30001);self.assertEqual(env['CUDA_VISIBLE_DEVICES'],'1')
        self.assertNotIn('--disaggregation-mode',cmd)
        self.assertEqual(env['PDSERVE_ENGINE_FAULT_GUARD'],'0')
    def test_unknown_role(self):
        with self.assertRaises(ValueError):serve.configuration('bad',Path('/model'))
if __name__=='__main__':unittest.main(verbosity=2)
