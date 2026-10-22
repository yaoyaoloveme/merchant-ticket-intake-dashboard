import unittest
import subprocess
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import publish_snapshot as publisher


class PublicPublisherTests(unittest.TestCase):
    def test_public_report_summary_uses_only_approved_aggregates(self):
        payload = {
            'merchant': {'deep': {
                'summary': {'total': 280, 'tianrun_count': 235, 'merchant_app_count': 45, 'description': '私密原文'},
                'completion_rate': {'completed': 100, 'rate_24h': 91.2, 'pending_verification': 7, 'categories': [{'handler': '员工甲'}]},
                'ka': {'count': 75, 'ratio': 31.9, 'brand_details': [{'tickets': ['私密']} ]},
                'merchant_app': {'count': 45, 'current_pending_count': 7, 'overdue_24h_count': 2, 'records': [{'source_url': 'http://172.25.1.1'}]},
            }},
            'employee': {
                'generated_at': '2026-10-22 18:00:02',
                'totals': {'tickets': 455, 'employees': 15, 'over48': 303, 'over120': 183,
                           'created_daily': {'2026-10-22': 92}, 'completed_daily': {'2026-10-22': 193}},
                'employees': [{'name': '员工甲'}], 'tickets': [{'description': '私密'}],
            },
        }

        result = publisher.public_report_summary(payload, '2026-10-22')
        text = json.dumps(result, ensure_ascii=False)

        self.assertEqual(result['merchant']['total'], 280)
        self.assertEqual(result['employee']['created_today'], 92)
        self.assertNotIn('员工甲', text)
        self.assertNotIn('私密', text)
        self.assertNotIn('172.25.', text)
        self.assertNotIn('description', text)

    def test_public_report_export_does_not_copy_private_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            docs = Path(directory) / 'docs'
            run = Path(directory) / 'report'
            run.mkdir()
            (run / 'analysis-data.json').write_text(json.dumps({
                'merchant': {'deep': {'summary': {'total': 3}}},
                'employee': {'totals': {'tickets': 4}},
                'private': {'description': '不应公开'},
            }), encoding='utf-8')
            report = {'id': '2026-10-22-1800', 'date': '2026-10-22', 'slot': '1800',
                      'slot_label': '18:00版', 'finished_at': '2026-10-22T18:01:00+08:00',
                      'status': 'success', 'status_label': '生成成功'}
            with patch.object(publisher, 'DOCS', docs), \
                 patch.object(publisher.dashboard, 'list_daily_reports', return_value=[report]), \
                 patch.object(publisher.dashboard, 'completed_report', return_value=(run, {})):
                publisher.export_public_reports()
            exported = (docs / 'data/reports/index.json').read_text(encoding='utf-8')
            self.assertIn('2026-10-22-1800', exported)
            self.assertNotIn('不应公开', exported)
            self.assertFalse((docs / 'analysis-data.json').exists())

    @patch.object(publisher.os, 'killpg')
    @patch.object(publisher.subprocess, 'Popen')
    def test_timed_out_git_process_group_is_terminated(self, popen, killpg):
        process = popen.return_value
        process.pid = 4321
        process.communicate.side_effect = [subprocess.TimeoutExpired(['git'], 1), ('', None)]

        with self.assertRaises(subprocess.TimeoutExpired):
            publisher.run_command(['git', 'status'], timeout=1)

        killpg.assert_called_once_with(4321, publisher.signal.SIGTERM)
        self.assertEqual(process.communicate.call_count, 2)

    @patch.object(publisher.requests, 'Session')
    def test_pending_deployment_defers_push(self, session_factory):
        response = Mock()
        response.json.return_value = {'workflow_runs': [{'status': 'pending'}]}
        session_factory.return_value.get.return_value = response
        self.assertTrue(publisher.deployment_pending())

        with patch.object(publisher, 'git', side_effect=['https://github.com/yaoyaoloveme/merchant-ticket-intake-dashboard.git',
                                                        'main', '']) as git:
            publisher.publish()
        self.assertEqual([call.args[0] for call in git.call_args_list], ['remote', 'branch', 'diff'])

    @patch.object(publisher.requests, 'Session')
    def test_completed_deployment_allows_push(self, session_factory):
        response = Mock()
        response.json.return_value = {'workflow_runs': [{'status': 'completed'}]}
        session_factory.return_value.get.return_value = response
        self.assertFalse(publisher.deployment_pending())

    @patch.object(publisher.requests, 'Session')
    def test_api_failure_does_not_stop_publication(self, session_factory):
        session_factory.return_value.get.side_effect = requests.Timeout('timed out')
        self.assertFalse(publisher.deployment_pending())


if __name__ == '__main__':
    unittest.main()
