import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as dashboard


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.client = dashboard.app.test_client()
        self.temp = tempfile.TemporaryDirectory()
        self.old_html = dashboard.EMPLOYEE_DASHBOARD_HTML
        self.old_data = dashboard.EMPLOYEE_DASHBOARD_DATA
        self.old_report_root = dashboard.REPORT_ARCHIVE_ROOT
        root = Path(self.temp.name)
        dashboard.EMPLOYEE_DASHBOARD_HTML = root / "employee.html"
        dashboard.EMPLOYEE_DASHBOARD_DATA = root / "employee.json"
        dashboard.REPORT_ARCHIVE_ROOT = root / "reports"

    def tearDown(self):
        dashboard.EMPLOYEE_DASHBOARD_HTML = self.old_html
        dashboard.EMPLOYEE_DASHBOARD_DATA = self.old_data
        dashboard.REPORT_ARCHIVE_ROOT = self.old_report_root
        self.temp.cleanup()

    def test_root_is_unified_workspace_and_daily_keeps_original_dashboard(self):
        workspace = self.client.get("/").get_data(as_text=True)
        daily = self.client.get("/daily?embed=1").get_data(as_text=True)
        self.assertIn("工单运营工作台", workspace)
        self.assertIn('data-view="employee"', workspace)
        self.assertIn('data-view="daily"', workspace)
        self.assertIn('data-view="reports"', workspace)
        self.assertIn('id="dispatchPageLink"', workspace)
        self.assertIn("每日新增工单分析", daily)
        self.assertIn('class="workspace-embedded"', daily)

    def test_employee_page_rewrites_refresh_endpoints_and_embedded_style(self):
        dashboard.EMPLOYEE_DASHBOARD_HTML.write_text(
            "<html><head></head><body><script>fetch('/api/status');fetch('/api/refresh',{method:'POST'});</script></body></html>",
            encoding="utf-8",
        )
        response = self.client.get("/employee-dashboard?embed=1")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("/api/employee/status", html)
        self.assertIn("/api/employee/refresh", html)
        self.assertIn("workspace-embedded-style", html)

    @patch("app.build_dashboard")
    @patch("app.latest_ticket_date", return_value="2026-10-22")
    def test_overview_combines_daily_and_employee_summaries(self, _latest, build_dashboard):
        build_dashboard.return_value = {
            "last_sync": "2026-10-22 18:00:00",
            "summary": {
                "total": 289,
                "previous": 300,
                "pct": -3.7,
                "risk_ticket_count": 2,
                "record_anomaly_count": 19,
                "top_driver": {"category": "回访工单", "delta": 12},
            },
            "daily_trend": [{"date": "2026-10-22", "total": 289}],
        }
        dashboard.EMPLOYEE_DASHBOARD_DATA.write_text(
            json.dumps(
                {
                    "generated_at": "2026-10-22 18:50:03",
                    "flow_days": ["2026-10-22"],
                    "totals": {
                        "tickets": 461,
                        "employees": 15,
                        "over48": 309,
                        "over120": 183,
                        "created_daily": {"2026-10-22": 99},
                        "completed_daily": {"2026-10-22": 194},
                    },
                    "employees": [{"name": "员工甲", "total": 55}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        payload = self.client.get("/api/workspace/overview").get_json()
        self.assertEqual(payload["daily"]["total"], 289)
        self.assertTrue(payload["employee"]["available"])
        self.assertEqual(payload["employee"]["tickets"], 461)
        self.assertEqual(payload["employee"]["busiest"]["name"], "员工甲")

    @patch("app.build_dashboard")
    @patch("app.latest_ticket_date", return_value="2026-10-22")
    def test_overview_marks_missing_employee_snapshot_as_unavailable(self, _latest, build_dashboard):
        build_dashboard.return_value = {"summary": {}, "daily_trend": []}
        payload = self.client.get("/api/workspace/overview").get_json()
        self.assertFalse(payload["employee"]["available"])
        self.assertIsNone(payload["employee"]["tickets"])
        self.assertIsNone(payload["employee"]["completed_today"])

    def write_report(self, slot="1800", errors=None):
        run = dashboard.REPORT_ARCHIVE_ROOT / "2026-10-22" / slot
        run.mkdir(parents=True)
        (run / "analysis.html").write_text(
            '<html><head></head><body><main><h1>双看板日报</h1><img src="merchant-overview.png"></main></body></html>',
            encoding="utf-8",
        )
        (run / "analysis.md").write_text("# 双看板日报\n", encoding="utf-8")
        (run / "analysis-data.json").write_text("{}\n", encoding="utf-8")
        (run / "merchant-overview.png").write_bytes(b"fake-png")
        (run / "run-metadata.json").write_text(
            json.dumps(
                {
                    "planned_at": f"2026-10-22 {slot[:2]}:{slot[2:]}",
                    "started_at": "2026-10-22T18:00:01+08:00",
                    "finished_at": "2026-10-22T18:04:01+08:00",
                    "merchant_refresh_ok": True,
                    "employee_refresh_ok": True,
                    "screenshots": {"overview": "merchant-overview.png"},
                    "errors": errors or [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return run

    def test_report_archive_lists_only_formal_runs(self):
        self.write_report()
        partial = self.write_report("2359", errors=["员工截图失败"])
        verify = dashboard.REPORT_ARCHIVE_ROOT / "2026-10-22" / "verify-120000"
        verify.mkdir(parents=True)
        (verify / "analysis.html").write_text("verify", encoding="utf-8")
        payload = self.client.get("/api/reports").get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["report_count"], 2)
        self.assertEqual(payload["reports"][0]["id"], "2026-10-22-2359")
        self.assertEqual(payload["reports"][0]["status"], "partial")
        self.assertEqual(payload["reports"][1]["status"], "success")
        (partial / "run-metadata.json").write_text(
            json.dumps({"planned_at": "2026-10-22 手动验证", "finished_at": "2026-10-22T23:59:00+08:00"}),
            encoding="utf-8",
        )
        self.assertEqual(self.client.get("/api/reports").get_json()["summary"]["report_count"], 1)

    def test_report_document_assets_and_downloads_are_served_safely(self):
        self.write_report()
        embedded = self.client.get("/reports/2026-10-22/1800/?embed=1")
        self.assertEqual(embedded.status_code, 200)
        self.assertIn("workspace-report-embedded-style", embedded.get_data(as_text=True))
        screenshot = self.client.get("/reports/2026-10-22/1800/merchant-overview.png")
        self.assertEqual(screenshot.status_code, 200)
        screenshot.close()
        download = self.client.get("/reports/2026-10-22/1800/analysis-data.json?download=1")
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers.get("Content-Disposition", ""))
        download.close()
        self.assertEqual(self.client.get("/reports/2026-10-22/verify-120000/").status_code, 404)
        self.assertEqual(self.client.get("/reports/2026-10-22/1800/private.txt").status_code, 404)
        self.assertEqual(self.client.get("/reports/2026-10-22/1800/../private.txt").status_code, 404)


if __name__ == "__main__":
    unittest.main()
