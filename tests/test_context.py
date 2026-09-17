"""Argument errors must fail before database configuration or dependencies are read."""
import os
import importlib.util
import json
import time
from unittest.mock import patch
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNNER = Path(__file__).resolve().parents[1] / 'skills/sql-context/scripts/context.py'


spec = importlib.util.spec_from_file_location('context_runner', RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def sleeping_worker(args, pipe):
    time.sleep(30)


class SerializationTests(unittest.TestCase):
    def test_unicode_byte_limits_and_continuation(self):
        rows = [{'body': '界🐛\\"' * 100, 'id': i} for i in range(5)]
        payload = runner.encode_result('search', rows, limit=3, offset=7, max_bytes=2048)
        result = json.loads(payload)
        self.assertLessEqual(len(payload), 2048)
        self.assertEqual(result['output_bytes'], len(payload))
        self.assertTrue(result['truncated'])
        self.assertEqual(result['next_offset'], 7 + len(result['rows']))

    def test_single_oversized_row_explains_continuation(self):
        payload = runner.encode_result('section', [{'body': '界' * 10000}], max_bytes=1024)
        result = json.loads(payload)
        self.assertEqual(result['rows'], [])
        self.assertTrue(result['truncated'])
        self.assertIn('--length', result['hint'])
        self.assertEqual(result['output_bytes'], len(payload))
        with self.assertRaises(ValueError):
            runner.encode_result('start', [{'task': '界' * 10000}], max_bytes=1024)

    def test_deadline_terminates_unresponsive_worker(self):
        started = time.monotonic()
        with patch.object(runner, 'worker', sleeping_worker):
            status, payload = runner.run_with_deadline(None, deadline=0.1)
        self.assertNotEqual(status, 0)
        self.assertIn('deadline', json.loads(payload)['error'])
        self.assertLess(time.monotonic() - started, 3)


class ArgumentTests(unittest.TestCase):
    def test_help_requires_no_configuration(self):
        result = subprocess.run([sys.executable, str(RUNNER), '--help'],
                                text=True, capture_output=True, timeout=10,
                                env={**os.environ, 'CONTEXT_SQL_CONFIG': '/absent/context-config.json'})
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ['catalog', 'start', 'restore', 'note', 'section', 'file']:
            self.assertIn(command, result.stdout)

    def test_invalid_bounds_fail_without_database_access(self):
        cases = [
            ['catalog', '--limit', '0'], ['catalog', '--limit', '51'],
            ['catalog', '--offset', '-1'], ['catalog', '--max-bytes', '1023'],
            ['catalog', '--max-bytes', '65537'],
            ['restore', '--run', 'bad-uuid'],
            ['section', '--skill', 'redis-single-node', '--path', 'SKILL.md',
             '--ordinal', '0', '--length', '8193'],
        ]
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, 'CONTEXT_SQL_CONFIG': str(Path(directory) / 'absent.json')}
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    result = subprocess.run([sys.executable, str(RUNNER), *arguments],
                                            text=True, capture_output=True, env=env, timeout=10)
                    self.assertEqual(result.returncode, 2)
                    self.assertNotIn('Traceback', result.stderr)
                    self.assertNotIn('absent.json', result.stderr)


if __name__ == '__main__':
    unittest.main()
