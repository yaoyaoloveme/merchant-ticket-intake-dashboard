import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import app as dashboard
import publish_snapshot as publisher


class MerchantAppDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = dashboard.DB_PATH
        dashboard.DB_PATH = Path(self.temp.name) / "tickets.db"
        dashboard.get_db().close()

    def tearDown(self):
        dashboard.DB_PATH = self.old_db
        self.temp.cleanup()

    def insert_ticket(self, day: date, ticket_id: str = "T-1", *, shop_id: str = "S-1") -> None:
        conn = dashboard.get_db()
        conn.execute(
            """INSERT INTO tickets
               (ticket_id,shop_id,brand,category_raw,category,description,status,inspect_status,
                tags,handler,created_at,created_date,duration_text,duration_minutes,
                inspect_completed_at,response_text,response_minutes,pipeline_text,is_ka,fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticket_id, shop_id, "", "多付/漏付", "多付/漏付", "少付", "处理中", "已稽查",
             "", "测试处理人", f"{day.isoformat()} 09:00", day.isoformat(), "", None,
             "", "", None, "", 0, f"{day.isoformat()}T10:00:00"),
        )
        conn.commit()
        conn.close()

    def insert_merchant(self, day: date, event_id: str, *, status: str = "未稽查",
                        is_open: int = 1, completed_at: str = "", shop_id: str = "S-APP",
                        inspection_id: str | None = None) -> None:
        conn = dashboard.get_db()
        conn.execute(
            """INSERT INTO merchant_inspections
               (event_id,inspection_id,shop_id,brand,description,inspect_status,abnormal_state,
                abnormal_type,created_at,created_date,completed_at,duration_text,duration_minutes,
                pipeline_text,is_ka,is_open,fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, inspection_id or f"I-{event_id}", shop_id, "测试KA", "少付", status, "待确认",
             "疑似少付/漏付/多付", f"{day.isoformat()} 08:00", day.isoformat(), completed_at,
             "", None, "", 1, is_open, f"{day.isoformat()}T10:00:00"),
        )
        conn.commit()
        conn.close()

    def test_overdue_requires_open_uninspected_and_full_24_hours(self):
        now = datetime(2026, 10, 20, 12, 0, tzinfo=dashboard.BEIJING_TZ)
        record = {
            "event_id": "E-1", "inspection_id": "I-1", "shop_id": "S-1", "brand": "",
            "description": "少付", "inspect_status": "未稽查", "abnormal_state": "待确认",
            "abnormal_type": "疑似少付/漏付/多付", "created_at": "2026-10-19 12:00",
            "completed_at": "", "duration_text": "", "pipeline_text": "", "is_ka": 0, "is_open": 1,
        }
        self.assertTrue(dashboard.merchant_record_payload(record, now)["overdue_24h"])
        record["created_at"] = "2026-10-19 12:01"
        self.assertFalse(dashboard.merchant_record_payload(record, now)["overdue_24h"])
        record.update(created_at="2026-10-19 10:00", inspect_status="稽查中")
        self.assertFalse(dashboard.merchant_record_payload(record, now)["overdue_24h"])
        record.update(inspect_status="未稽查", completed_at="2026-10-20 11:00")
        self.assertFalse(dashboard.merchant_record_payload(record, now)["overdue_24h"])

    def test_dashboard_total_is_tianrun_plus_merchant_app(self):
        selected = date.today() - timedelta(days=2)
        previous = selected - timedelta(days=1)
        self.insert_ticket(selected)
        self.insert_merchant(selected, "E-current")
        self.insert_merchant(previous, "E-previous")
        conn = dashboard.get_db()
        conn.execute("INSERT INTO merchant_sync_state VALUES (?,?)", ("day:" + selected.isoformat(), "2026-10-20T10:00:00"))
        conn.execute("INSERT INTO merchant_sync_state VALUES (?,?)", ("open_synced_at", "2026-10-20T10:00:00"))
        conn.commit()
        conn.close()
        data = dashboard.build_dashboard(selected, 7)
        self.assertEqual(data["summary"]["tianrun_count"], 1)
        self.assertEqual(data["summary"]["merchant_app_count"], 1)
        self.assertEqual(data["summary"]["total"], 2)
        self.assertEqual(data["daily_trend"][-1]["merchant_app_count"], 1)

    def test_merchant_ranking_combines_sources_and_deduplicates_app_inspections(self):
        end_day = date(2026, 10, 21)
        self.insert_ticket(end_day - timedelta(days=1), "T-1", shop_id="S-SHARED")
        self.insert_ticket(end_day - timedelta(days=2), "T-2", shop_id="S-SHARED")
        self.insert_ticket(end_day, "T-3", shop_id="")
        self.insert_merchant(end_day - timedelta(days=1), "E-1", shop_id="S-SHARED", inspection_id="I-SHARED")
        self.insert_merchant(end_day - timedelta(days=2), "E-2", shop_id="S-SHARED", inspection_id="I-SHARED")
        self.insert_merchant(end_day, "E-3", shop_id="S-APP")

        conn = dashboard.get_db()
        start_day = end_day - timedelta(days=89)
        conn.execute(
            "INSERT INTO sync_runs(started_at,finished_at,date_from,date_to,status,message) VALUES(?,?,?,?,?,?)",
            ("2026-10-21T10:00:00", "2026-10-21T10:01:00", start_day.isoformat(), end_day.isoformat(), "ok", "test"),
        )
        for offset in range(90):
            day = (start_day + timedelta(days=offset)).isoformat()
            conn.execute("INSERT INTO merchant_sync_state VALUES (?,?)", ("day:" + day, "2026-10-21T10:01:00"))
        conn.commit()
        conn.close()

        data = dashboard.build_merchant_complaint_ranking(end_day)
        first = data["ranking"][0]
        self.assertEqual((first["shop_id"], first["tianrun_count"], first["merchant_app_count"], first["total"]),
                         ("S-SHARED", 2, 1, 3))
        self.assertEqual(len([ticket for ticket in first["tickets"] if ticket["source_label"] == "商家APP"]), 1)
        self.assertEqual(data["unmatched_tianrun_count"], 1)
        self.assertEqual(data["total_count"], 4)
        self.assertTrue(data["coverage"]["complete"])
        self.assertEqual(data["coverage"]["fully_synced_days"], 90)

    def test_merchant_ranking_coverage_does_not_assume_missing_days_are_empty(self):
        end_day = date(2026, 10, 21)
        start_day = end_day - timedelta(days=89)
        conn = dashboard.get_db()
        conn.execute(
            "INSERT INTO sync_runs(started_at,finished_at,date_from,date_to,status,message) VALUES(?,?,?,?,?,?)",
            ("2026-10-21T10:00:00", "2026-10-21T10:01:00", start_day.isoformat(), end_day.isoformat(), "ok", "test"),
        )
        for offset in range(90):
            if offset == 15:
                continue
            day = (start_day + timedelta(days=offset)).isoformat()
            conn.execute("INSERT INTO merchant_sync_state VALUES (?,?)", ("day:" + day, "2026-10-21T10:01:00"))
        conn.commit()
        conn.close()

        coverage = dashboard.build_merchant_complaint_ranking(end_day)["coverage"]
        self.assertFalse(coverage["complete"])
        self.assertEqual(coverage["fully_synced_days"], 89)
        self.assertEqual(coverage["missing_days"], 1)
        self.assertEqual(coverage["first_missing_date"], (start_day + timedelta(days=15)).isoformat())
        self.assertEqual(coverage["contiguous_days_to_end"], 74)

    def test_completion_rate_uses_first_inspection_node_and_exact_seconds(self):
        rows = [
            {"ticket_id": "exact", "category": "多付/漏付", "status": "处理中", "created_at": "2026-10-01 09:00", "inspect_completed_at": "2099-01-01", "duration_minutes": 999999, "check_meta": "稽查完成 2026-10-02 09:00:00", "detail_status": "ok"},
            {"ticket_id": "late", "category": "介入问题", "status": "已完成", "created_at": "2026-10-01 09:00", "inspect_completed_at": "2026-10-01 10:00", "duration_minutes": 60, "check_meta": "稽查完成 2026-10-02 09:00:01", "detail_status": "ok"},
            {"ticket_id": "first", "category": "批量核实", "status": "处理中", "created_at": "2026-10-01 09:00", "check_meta": "稽查人工打标 · 稽查完成 2026-10-02 14:00:00；稽查人工打标 · 稽查完成 2026-10-01 11:00:00", "detail_status": "ok"},
            {"ticket_id": "no-node", "category": "回访工单", "status": "已完成", "created_at": "2026-10-01 09:00", "inspect_completed_at": "2026-10-01 10:00", "check_meta": "稽查人工打标 2026-10-01 09:30", "detail_status": "ok"},
            {"ticket_id": "old-node", "category": "多付/漏付", "created_at": "2026-10-02 09:00", "check_meta": "稽查完成 2026-10-01 10:00:00；稽查完成 2026-10-02 10:00:00", "detail_status": "ok"},
        ]
        result = dashboard.build_completion_stats(rows, datetime(2026, 10, 3, 9, 0))
        self.assertEqual(result["completed"], 4)
        self.assertEqual(result["within_24h"], 2)
        self.assertEqual(result["rate_24h"], 50.0)
        self.assertEqual(result["pending_verification"], 1)
        self.assertEqual(result["not_completed_count"], 1)
        self.assertEqual(result["overdue_not_completed_count"], 1)
        self.assertEqual(sum(item["completed"] + item["pending_verification"] for item in result["categories"]), 5)
        self.assertEqual(sum(item["count"] for item in result["excluded_breakdown"]), 1)
        category = next(item for item in result["categories"] if item["category"] == "多付/漏付")
        self.assertEqual(category["completed"], 1)
        self.assertEqual(category["pending_verification"], 1)
        first = dashboard.completion_record_result(rows[2])
        self.assertEqual(first["first_inspect_completed_at"], "2026-10-01 11:00:00")
        self.assertEqual(dashboard.completion_record_result(rows[0])["elapsed_seconds"], 86400)
        self.assertEqual(dashboard.completion_record_result(rows[1])["result"], "超时")

    def test_feedback_marker_does_not_replace_first_completion_time(self):
        rows = [{"ticket_id": "feedback", "category": "多付/漏付", "status": "处理中", "created_at": "2026-10-01 09:00", "response_text": "稽查完成后反馈", "check_meta": "", "detail_status": "ok"},
                {"ticket_id": "unknown", "category": "多付/漏付", "status": "已完成", "created_at": "2026-10-01 09:00", "check_meta": "", "detail_status": "error"},
                {"ticket_id": "feedback-old", "category": "多付/漏付", "created_at": "2026-10-02 09:00", "response_text": "稽查完成后反馈", "check_meta": "稽查完成 2026-10-01 10:00:00", "detail_status": "ok"},
                {"ticket_id": "feedback-late", "category": "多付/漏付", "created_at": "2026-10-01 09:00", "response_text": "稽查完成后反馈", "check_meta": "稽查完成 2026-10-02 09:00:01", "detail_status": "ok"},
                {"ticket_id": "feedback-valid", "category": "多付/漏付", "created_at": "2026-10-01 09:00", "response_text": "稽查完成后反馈", "check_meta": "稽查完成 2026-10-01 10:00:00", "detail_status": "ok"}]
        result = dashboard.build_completion_stats(rows, datetime(2026, 10, 3, 9, 0))
        self.assertEqual(result["completed"], 3)
        self.assertEqual(result["within_24h"], 1)
        self.assertEqual(result["rate_24h"], 33.3)
        self.assertEqual(result["verified_time_count"], 2)
        self.assertEqual(result["not_completed_count"], 1)
        self.assertEqual(result["overdue_not_completed_count"], 1)
        self.assertEqual(dashboard.completion_record_result(rows[0], datetime(2026, 10, 3, 9, 0))["result"], "超时未完成")
        self.assertEqual(dashboard.completion_record_result(rows[2])["result"], "待核验")
        self.assertEqual(dashboard.completion_record_result(rows[3])["result"], "超时")
        self.assertEqual(result["pending_verification"], 2)

    def test_missing_completion_time_counts_as_incomplete_and_reclassifies_when_found(self):
        row = {
            "ticket_id": "still-open", "category": "批量核实", "created_at": "2026-10-20 12:00:00",
            "check_meta": "", "detail_status": "ok",
        }
        just_before_deadline = datetime(2026, 10, 21, 11, 59, 59)
        result_before = dashboard.completion_record_result(row, just_before_deadline)
        stats_before = dashboard.build_completion_stats([row], just_before_deadline)
        self.assertEqual(result_before["result"], "未完成")
        self.assertEqual(result_before["elapsed_seconds"], 86399)
        self.assertEqual((stats_before["completed"], stats_before["within_24h"], stats_before["not_completed_count"]), (1, 0, 1))
        self.assertEqual(stats_before["rate_24h"], 0.0)

        deadline = datetime(2026, 10, 21, 12, 0, 0)
        result_at_deadline = dashboard.completion_record_result(row, deadline)
        stats_at_deadline = dashboard.build_completion_stats([row], deadline)
        self.assertEqual(result_at_deadline["result"], "超时未完成")
        self.assertEqual(stats_at_deadline["overdue_not_completed_count"], 1)

        row["check_meta"] = "稽查完成 2026-10-21 11:59:59"
        corrected = dashboard.completion_record_result(row, datetime(2026, 10, 21, 12, 30, 0))
        self.assertEqual(corrected["result"], "24小时内")
        self.assertEqual(corrected["elapsed_seconds"], 86399)

    def test_dashboard_completion_joins_cached_first_inspection_evidence(self):
        selected = date.today() - timedelta(days=1)
        self.insert_ticket(selected, "completion-integration")
        conn = dashboard.get_db()
        conn.execute(
            """INSERT INTO ticket_evidence
               (ticket_id,check_count,abnormal_state,abnormal_type,check_result,check_meta,
                loss_summary,detail_status,fetched_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            ("completion-integration", 1, "无异常", "", "", "稽查人工打标 · 稽查完成 "
             + selected.isoformat() + " 12:00:00", "", "ok", selected.isoformat()),
        )
        conn.commit()
        conn.close()
        rate = dashboard.build_dashboard(selected, 7)["completion_rate"]
        self.assertEqual(rate["completed"], 1)
        self.assertEqual(rate["verified_time_count"], 1)
        self.assertEqual(rate["within_24h"], 1)

    def test_completion_day_api_only_includes_tickets_created_on_selected_day(self):
        selected = date.today() - timedelta(days=1)
        self.insert_ticket(selected, "selected-day")
        self.insert_ticket(selected - timedelta(days=1), "previous-day")
        client = dashboard.app.test_client()

        response = client.get(f"/api/completion-day?date={selected.isoformat()}")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()["data"]
        self.assertEqual(payload["date"], selected.isoformat())
        self.assertEqual([row["ticket_id"] for row in payload["records"]], ["selected-day"])
        self.assertEqual(payload["stats"]["pending_verification"], 1)

    def test_public_export_includes_app_only_date(self):
        selected = date.today() - timedelta(days=1)
        self.insert_merchant(selected, "E-only")
        conn = dashboard.get_db()
        conn.execute(
            "INSERT INTO sync_runs(started_at,finished_at,status,message) VALUES (?,?,?,?)",
            (f"{selected.isoformat()}T10:00:00", f"{selected.isoformat()}T10:01:00", "ok", "APP同步完成"),
        )
        conn.commit()
        conn.close()
        destination = Path(self.temp.name) / "public"
        with patch.object(publisher, "DOCS", destination):
            publisher.export_snapshot()
        manifest = json.loads((destination / "data/index.json").read_text())
        snapshot = json.loads((destination / f"data/{selected.isoformat()}.json").read_text())
        completion_records = json.loads((destination / "data/completion-records.json").read_text())
        week_start = dashboard.natural_week_start(selected).isoformat()
        week_snapshot = json.loads((destination / f"data/completion-week-{week_start}.json").read_text())
        public_app_js = (destination / "assets/app.js").read_text()
        public_html = (destination / "index.html").read_text()
        public_daily_html = (destination / "daily.html").read_text()
        self.assertIn(selected.isoformat(), manifest["dates"])
        self.assertIn(week_start, manifest["completion_weeks"])
        if selected.isoformat() >= week_start:
            self.assertIn(selected.isoformat(), [day["date"] for day in week_snapshot["days"]])
        self.assertIn("completionDatePicker", public_app_js)
        self.assertIn("completion-week-", public_app_js)
        self.assertIn("工单运营工作台", public_html)
        self.assertIn("assets/public-workspace.js?v=", public_html)
        self.assertIn("assets/app.js?v=", public_daily_html)
        self.assertEqual(snapshot["dashboard"]["summary"]["merchant_app_count"], 1)
        self.assertEqual(snapshot["dashboard"]["summary"]["total"], 1)
        self.assertEqual(completion_records, [])


if __name__ == "__main__":
    unittest.main()
