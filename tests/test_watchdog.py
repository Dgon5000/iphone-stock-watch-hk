"""Recovery decisions with a fake GitHub API; no live workflows are started."""
import contextlib
import importlib.util
import io
import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location('watchdog', Path(__file__).resolve().parents[1] / '.github/scripts/ensure_stock_watch.py')
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


class Recovery(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
        self.workflows = [{'id': 10, 'state': 'active', 'path': watchdog.WORKFLOW_PATH}]
        self.runs = []
        self.dispatches = []
        self.failures = 0
        self.sleeps = []

    def gh(self, *args):
        if args[0] == 'api':
            if args[1].endswith('/actions/workflows'):
                return json.dumps({'workflows': self.workflows})
            self.assertTrue(args[1].endswith('/10/runs?branch=main&per_page=50'))
            return json.dumps({'workflow_runs': self.runs})
        self.dispatches.append(args)
        if self.failures:
            self.failures -= 1
            raise subprocess.CalledProcessError(1, ['gh', *args])
        return ''

    def ensure(self):
        with mock.patch.object(watchdog, 'gh', self.gh), \
             mock.patch.object(watchdog.time, 'sleep', self.sleeps.append), \
             contextlib.redirect_stdout(io.StringIO()):
            return watchdog.ensure_running('owner/repo', self.now)

    def completed(self, conclusion, age=1800):
        return {'status': 'completed', 'conclusion': conclusion,
                'created_at': (self.now - timedelta(seconds=age)).isoformat()}

    def test_active_or_queued_run_prevents_duplicate_dispatch(self):
        for status in ('in_progress', 'pending', 'queued', 'waiting', 'requested'):
            with self.subTest(status=status):
                self.runs = [{'status': status}]
                self.assertEqual(self.ensure(), 0)
        self.assertEqual(self.dispatches, [])

    def test_completion_failure_timeout_or_cancellation_can_restart(self):
        for conclusion in ('success', 'failure', 'timed_out', 'cancelled'):
            with self.subTest(conclusion=conclusion):
                self.runs = [self.completed(conclusion)]
                self.assertEqual(self.ensure(), 0)
        self.assertEqual(self.dispatches, [('workflow', 'run', 'stock-watch.yml', '--repo', 'owner/repo', '--ref', 'main')] * 4)

    def test_quick_failures_are_throttled_until_ten_minutes_since_start(self):
        self.runs = [self.completed('failure', age=599)]
        self.ensure()
        self.assertEqual(self.dispatches, [])
        self.runs = [self.completed('failure', age=600)]
        self.ensure()
        self.assertEqual(len(self.dispatches), 1)

    def test_disabled_or_missing_workflow_is_not_restarted(self):
        self.workflows[0]['state'] = 'disabled_manually'
        self.ensure()
        self.workflows = []
        self.ensure()
        self.assertEqual(self.dispatches, [])

    def test_never_run_workflow_is_started(self):
        self.assertEqual(self.ensure(), 0)
        self.assertEqual(len(self.dispatches), 1)

    def test_dispatch_retries_transient_failures(self):
        self.failures = 2
        self.assertEqual(self.ensure(), 0)
        self.assertEqual(len(self.dispatches), 3)
        self.assertEqual(self.sleeps, [5, 10])

    def test_permanent_dispatch_failure_is_reported(self):
        self.failures = 3
        with self.assertRaises(subprocess.CalledProcessError):
            self.ensure()
        self.assertEqual(len(self.dispatches), 3)
        self.assertEqual(self.sleeps, [5, 10])
