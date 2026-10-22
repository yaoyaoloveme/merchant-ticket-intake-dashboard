import os
import secrets
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import app as dashboard
import complaint_audit as audit


def sample(
    record_id,
    inspect_id,
    result=True,
    review_status="审核通过",
    responsibility="人工客服责任",
    agent="客服甲",
    completed="2026-10-20T10:00:00",
    updated="2026-10-20T11:00:00",
    **overrides,
):
    row = {
        "complaint_record_id": record_id,
        "inspect_id": inspect_id,
        "ticket_id": f"T-{inspect_id}",
        "shop_id": "S-1",
        "shop_name": "测试店铺",
        "brand": "测试品牌",
        "inspect_status": "已稽查",
        "inspect_completed_at": completed,
        "is_complaint_true": result,
        "review_status": review_status,
        "complained_agent": agent,
        "reason_node_id": "node-1",
        "reason_path": "正式分类 / 节点",
        "responsibility_subject": responsibility,
        "reason_description": "正式原因说明",
        "inspect_result": "正式稽查结论",
        "abnormal_state": "有异常",
        "abnormal_type": "测试异常",
        "handled_at": "2026-10-20T10:30:00",
        "reviewed_at": "2026-10-20T10:50:00",
        "source_updated_at": updated,
        "deleted": False,
    }
    row.update(overrides)
    return row


class ComplaintAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = audit.DB_PATH
        audit.DB_PATH = Path(self.temp.name) / "tickets.db"
        audit.AUTO_STATE.update(
            {"running": False, "message": "尚未同步", "last_started_at": None,
             "last_finished_at": None, "last_error": None, "next_run_at": None}
        )
        self.old_env = {
            "COMPLAINT_AUDIT_PASSWORD_HASH": os.environ.get("COMPLAINT_AUDIT_PASSWORD_HASH"),
            "COMPLAINT_AUDIT_SESSION_SECRET": os.environ.get("COMPLAINT_AUDIT_SESSION_SECRET"),
            "COMPLAINT_AUDIT_RESPONSIBILITY_NODE_MAP": os.environ.get("COMPLAINT_AUDIT_RESPONSIBILITY_NODE_MAP"),
        }
        self.password = secrets.token_urlsafe(18)
        os.environ["COMPLAINT_AUDIT_PASSWORD_HASH"] = generate_password_hash(self.password)
        os.environ["COMPLAINT_AUDIT_SESSION_SECRET"] = secrets.token_urlsafe(32)
        os.environ.pop("COMPLAINT_AUDIT_RESPONSIBILITY_NODE_MAP", None)
        audit.init_db()
        self.client = dashboard.app.test_client()
        response = self.client.post("/complaint-audit/login", data={"password": self.password})
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        audit.DB_PATH = self.old_db
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def ingest(self, *rows):
        return audit.ingest_records(rows)

    def summary(self):
        response = self.client.get("/api/complaint-audit/summary?month=2026-10")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["data"]

    def test_false_boolean_is_not_upheld_and_not_blank(self):
        self.ingest(sample("1", "100", result=False))
        summary = self.summary()["summary"]
        self.assertEqual(summary["not_upheld"], 1)
        self.assertEqual(summary["undetermined"], 0)
        self.assertEqual(summary["upheld_rate"], 0.0)

    def test_multiple_records_for_one_inspection_count_once(self):
        self.ingest(sample("1", "100", result=True), sample("2", "100", result=True, updated="2026-10-20T11:05:00"))
        data = self.summary()
        self.assertEqual(data["summary"]["inspected"], 1)
        self.assertEqual(data["summary"]["upheld"], 1)
        records = self.client.get("/api/complaint-audit/records?month=2026-10").get_json()["data"]["records"]
        self.assertTrue(records[0]["multiple_records"])
        self.assertEqual(records[0]["record_count"], 2)

    def test_conflicting_approved_results_are_excluded(self):
        self.ingest(sample("1", "100", result=True), sample("2", "100", result=False, updated="2026-10-20T11:05:00"))
        summary = self.summary()["summary"]
        self.assertEqual(summary["conflict"], 1)
        self.assertEqual(summary["upheld"], 0)
        self.assertEqual(summary["not_upheld"], 0)
        self.assertEqual(summary["upheld_rate"], 0.0)

    def test_review_boundaries_and_undetermined(self):
        self.ingest(
            sample("1", "101", result=None, review_status="审核通过"),
            sample("2", "102", result=True, review_status="待审核"),
            sample("3", "103", result=False, review_status="审核退回"),
        )
        summary = self.summary()["summary"]
        self.assertEqual(summary["approved"], 1)
        self.assertEqual(summary["undetermined"], 1)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["returned"], 1)
        self.assertEqual(summary["upheld"], 0)
        self.assertEqual(summary["not_upheld"], 0)

    def test_ai_and_system_responsibility_never_enter_agent_formal_count(self):
        self.ingest(
            sample("1", "101", responsibility="人工客服责任", agent="客服甲"),
            sample("2", "102", responsibility="AI工作台责任", agent="客服甲"),
            sample("3", "103", responsibility="系统/平台责任", agent="客服甲"),
        )
        ranking = self.summary()["rankings"][0]
        self.assertEqual(ranking["complained_count"], 3)
        self.assertEqual(ranking["complaint_upheld_count"], 3)
        self.assertEqual(ranking["formal_responsibility_count"], 1)

    def test_no_keyword_guessing_for_responsibility_or_conclusion(self):
        self.ingest(sample(
            "1", "101", responsibility="", reason_node_id="unknown-node",
            reason_path="客诉成立 / AI工作台值守 / 模型未发现异常",
            inspect_result="", abnormal_state="", abnormal_type="",
        ))
        record = self.client.get("/api/complaint-audit/records?month=2026-10").get_json()["data"]["records"][0]
        self.assertEqual(record["responsibility_subject"], "责任待确认")
        self.assertEqual(record["inspect_result"], "未填写稽查结论")
        self.assertEqual(record["abnormal_state"], "未填写")

    def test_node_id_mapping_is_structured_and_exact(self):
        os.environ["COMPLAINT_AUDIT_RESPONSIBILITY_NODE_MAP"] = '{"ai-node":"AI工作台责任"}'
        self.ingest(sample("1", "101", responsibility="", reason_node_id="ai-node"))
        record = self.client.get("/api/complaint-audit/records?month=2026-10").get_json()["data"]["records"][0]
        self.assertEqual(record["responsibility_subject"], "AI工作台责任")

    def test_excel_mapping_uses_formal_path_and_completion_time(self):
        raw = {
            "ID": 247333, "稽查单ID": 61975746, "工单ID": 1368764, "店铺ID": 1828266023,
            "稽查状态": "已稽查", "完成处理时间": "2026-10-20 21:34:25",
            "审核状态": "审核通过", "完成审核时间": "2026-10-20 21:40:00",
            "最后修改日期": "2026-10-20 21:40:00", "昵称": "客服甲",
            "原因分类别名": "客诉成立##值守客服原因##值守未发现##客服原因##明显漏付",
            "客诉分类别名": "投诉类##资金类##漏付", "客诉原因描述": "<p>正式描述</p>",
            "反馈来源": "天润工单", "反馈时间": "2026-10-20 17:00:00",
        }
        mapped = audit.excel_row_to_record(raw)
        self.ingest(mapped)
        record = self.client.get("/api/complaint-audit/records?month=2026-10").get_json()["data"]["records"][0]
        self.assertEqual(record["classification"], "客诉成立")
        self.assertEqual(record["responsibility_subject"], "人工客服责任")
        self.assertEqual(record["inspect_completed_at"], "2026-10-20T21:34:25")
        self.assertEqual(record["reason_levels"][2], "值守未发现")
        self.assertEqual(record["complaint_category"], "投诉类 / 资金类 / 漏付")

    def test_excel_audit_not_approved_is_returned(self):
        self.ingest(sample("1", "101", result=True, review_status="审核不通过"))
        self.assertEqual(self.summary()["summary"]["returned"], 1)

    def test_root_cause_counts_unique_inspections(self):
        path = "客诉成立 / 值守客服原因 / 值守未发现 / 客服原因 / 明显漏付"
        self.ingest(
            sample("1", "101", reason_path=path, responsibility="人工客服责任"),
            sample("2", "101", reason_path=path, responsibility="人工客服责任", updated="2026-10-20T11:05:00"),
        )
        roots = audit.root_cause_analysis(audit.load_aggregates("2026-10"))
        self.assertEqual(roots["upheld_total"], 1)
        self.assertEqual(roots["mechanisms"][0]["name"], "值守未发现")
        self.assertEqual(roots["mechanisms"][0]["count"], 1)

    def test_not_upheld_causes_count_unique_inspections_and_exclude_other_results(self):
        repeat_path = "客诉不成立 / 重复反馈 / 重复反馈 / 多渠道重复提交"
        no_abnormal_path = "客诉不成立 / 核实结果无异常 / 特殊诉求 / 商家要求核实事件"
        self.ingest(
            sample("1", "101", result=False, reason_path=repeat_path),
            sample("2", "101", result=False, reason_path=repeat_path, updated="2026-10-20T11:05:00"),
            sample("3", "102", result=False, reason_path=no_abnormal_path),
            sample("4", "103", result=True, reason_path="客诉成立 / 值守客服原因 / 值守未发现"),
            sample("5", "104", result=True, reason_path="客诉成立 / AI工作台值守 / ai模型未发现异常"),
            sample("6", "104", result=False, reason_path=repeat_path, updated="2026-10-20T11:05:00"),
        )
        analysis = self.summary()["not_upheld_causes"]
        self.assertEqual(analysis["not_upheld_total"], 2)
        self.assertEqual(analysis["repeat_summary"]["repeat_count"], 1)
        self.assertEqual(analysis["repeat_summary"]["multi_channel_count"], 1)
        self.assertEqual({row["name"]: row["count"] for row in analysis["branches"]}, {
            "重复反馈": 1, "核实结果无异常": 1,
        })
        self.assertIn("合计2张", analysis["analyst_facts"][0]["text"])

    def test_not_upheld_cause_drilldown_matches_analysis_count(self):
        repeat_path = "客诉不成立 / 重复反馈 / 重复反馈 / 稽查已核实商家重复反馈"
        self.ingest(
            sample("1", "101", result=False, reason_path=repeat_path),
            sample("2", "102", result=False, reason_path=repeat_path),
            sample("3", "103", result=False, reason_path="客诉不成立 / 核实结果无异常 / 特殊诉求 / 商家要求核实订单"),
        )
        analysis = self.summary()["not_upheld_causes"]
        repeat_count = next(row["count"] for row in analysis["branches"] if row["name"] == "重复反馈")
        response = self.client.get(
            "/api/complaint-audit/records?month=2026-10&bucket=not_upheld&root_cause=重复反馈"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["total"], repeat_count)

    def test_not_upheld_description_analysis_reads_source_text_without_responsibility_guessing(self):
        self.ingest(
            sample("1", "101", result=False, reason_description="顾客正常扫码支付离店"),
            sample("2", "102", result=False, reason_description="反馈时间店内没有顾客"),
            sample("3", "103", result=False, reason_description="商家要求核实事件"),
            sample("4", "104", result=False, reason_description="未填写"),
            sample("5", "105", result=True, reason_description="顾客正常扫码支付离店"),
        )
        analysis = self.summary()["not_upheld_descriptions"]
        self.assertEqual(analysis["not_upheld_total"], 4)
        themes = {row["name"]: row["count"] for row in analysis["themes"]}
        self.assertEqual(themes["订单支付或商品数量核实正常"], 1)
        self.assertEqual(themes["反馈时间、提单或核实对象不匹配"], 1)
        self.assertEqual(themes["仅描述核实诉求，未写明结论"], 1)
        self.assertEqual(themes["描述未填写或过于笼统"], 1)

    def test_description_theme_and_exact_original_drilldown_match_counts(self):
        self.ingest(
            sample("1", "101", result=False, reason_description="无异常。"),
            sample("2", "102", result=False, reason_description="无异常"),
            sample("3", "103", result=False, reason_description="商家要求核实事件"),
        )
        analysis = self.summary()["not_upheld_descriptions"]
        original = next(row for row in analysis["originals"] if row["name"] == "无异常")
        exact = self.client.get(
            "/api/complaint-audit/records?month=2026-10&bucket=not_upheld&reason_description=无异常"
        ).get_json()["data"]
        theme = self.client.get(
            "/api/complaint-audit/records?month=2026-10&bucket=not_upheld&description_theme=核实未发现异常"
        ).get_json()["data"]
        self.assertEqual(original["count"], 2)
        self.assertEqual(exact["total"], 2)
        self.assertEqual(theme["total"], 2)

    def test_description_with_unresolved_abnormality_is_not_hidden_by_notification_action(self):
        self.ingest(sample(
            "1", "101", result=False,
            reason_description="漏付未及时拦截，已发送商家",
        ))
        themes = self.summary()["not_upheld_descriptions"]["themes"]
        self.assertEqual(themes[0]["name"], "描述仍含异常或待处理事项")
        self.assertEqual(themes[0]["count"], 1)

    def test_changes_are_idempotent_and_history_keeps_before_after(self):
        first = self.ingest(sample("1", "101", result=True))
        duplicate = self.ingest(sample("1", "101", result=True))
        changed = self.ingest(sample("1", "101", result=False, updated="2026-10-20T12:00:00"))
        self.assertEqual(first["changed"], 1)
        self.assertEqual(duplicate["changed"], 0)
        self.assertEqual(changed["changed"], 1)
        history = self.client.get("/api/complaint-audit/records/101/history").get_json()["data"]["versions"]
        self.assertEqual(len(history), 2)
        result_change = next(item for item in history[0]["changes"] if item["field"] == "complaint_result")
        self.assertEqual(result_change["before"], "是")
        self.assertEqual(result_change["after"], "否")

    def test_same_ticket_multiple_inspection_ids_count_separately(self):
        self.ingest(
            sample("1", "101", ticket_id="1368113"),
            sample("2", "102", ticket_id="1368113"),
        )
        self.assertEqual(self.summary()["summary"]["inspected"], 2)

    def test_daily_and_weekly_trend_use_completed_time(self):
        self.ingest(
            sample("1", "101", completed="2026-10-02T08:00:00"),
            sample("2", "102", result=False, completed="2026-10-02T18:00:00"),
        )
        records = audit.load_aggregates("2026-10")
        daily = {row["key"]: row for row in audit.trend_rows(records, "2026-10", "day")}
        self.assertEqual(daily["2026-10-02"]["inspected"], 2)
        self.assertEqual(daily["2026-10-02"]["upheld"], 1)
        self.assertEqual(daily["2026-10-02"]["not_upheld"], 1)
        weekly = audit.trend_rows(records, "2026-10", "week")
        self.assertEqual(weekly[0]["inspected"], 2)

    def test_non_inspected_and_deleted_records_do_not_count(self):
        self.ingest(
            sample("1", "101", inspect_status="稽查中"),
            sample("2", "102", deleted=True),
            sample("3", "103"),
        )
        self.assertEqual(self.summary()["summary"]["inspected"], 1)

    def test_unauthenticated_and_remote_access_are_rejected(self):
        fresh_client = dashboard.app.test_client()
        self.assertEqual(fresh_client.get("/api/complaint-audit/records?month=2026-10").status_code, 401)
        remote = self.client.get("/", environ_base={"REMOTE_ADDR": "10.0.0.8"})
        self.assertEqual(remote.status_code, 403)

    def test_month_filter_is_limited_to_recent_six_months(self):
        response = self.client.get("/api/complaint-audit/summary?month=2026-01")
        self.assertEqual(response.status_code, 400)
        self.assertIn("近6个月", response.get_json()["error"])

    def test_login_is_rate_limited_without_logging_password(self):
        client = dashboard.app.test_client()
        wrong_password = "not-the-generated-test-value"
        for _ in range(audit.LOGIN_FAILURE_LIMIT):
            response = client.post("/complaint-audit/login", data={"password": wrong_password}, environ_base={"REMOTE_ADDR": "10.0.0.9"})
            self.assertEqual(response.status_code, 200)
        limited = client.post("/complaint-audit/login", data={"password": wrong_password}, environ_base={"REMOTE_ADDR": "10.0.0.9"})
        self.assertEqual(limited.status_code, 429)
        conn = audit.get_db()
        body = " ".join(str(tuple(row)) for row in conn.execute("SELECT * FROM complaint_audit_access_log"))
        conn.close()
        self.assertNotIn(wrong_password, body)

    def test_public_snapshot_contains_no_complaint_audit_assets_or_data(self):
        markers = ("/complaint-audit", "complained_agent", "responsibility_subject", "complaint_audit_records")
        docs = Path(__file__).resolve().parents[1] / "docs"
        for path in docs.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".html", ".js", ".json", ".css"}:
                content = path.read_text(encoding="utf-8")
                for marker in markers:
                    self.assertNotIn(marker, content, f"{marker} leaked into {path}")

    @patch("complaint_audit.fetch_pages")
    def test_sync_persists_cursor_and_uses_five_minute_overlap(self, fetch_pages):
        fetch_pages.side_effect = [
            ([sample("1", "101", updated="2026-10-20T11:00:00")], [], "cursor-1"),
            ([sample("1", "101", updated="2026-10-20T11:00:00")], [], "cursor-1"),
            ([sample("2", "102", updated="2026-10-20T11:10:00")], [], "cursor-2"),
        ]
        with patch("complaint_audit.date") as mocked_date:
            mocked_date.today.return_value = date(2026, 10, 20)
            mocked_date.fromisoformat.side_effect = date.fromisoformat
            audit.sync_once(force_backfill=True)
            audit.sync_once()
        incremental_call = fetch_pages.call_args_list[2]
        self.assertEqual(incremental_call.args[0], audit.CHANGES_PATH)
        self.assertEqual(incremental_call.args[1]["cursor"], "cursor-1")
        self.assertEqual(incremental_call.args[1]["modified_since"], "2026-10-20T10:55:00")
        conn = audit.get_db()
        cursor = conn.execute("SELECT cursor,last_source_updated_at FROM complaint_audit_sync_cursor").fetchone()
        conn.close()
        self.assertEqual(cursor["cursor"], "cursor-2")
        self.assertEqual(cursor["last_source_updated_at"], "2026-10-20T11:10:00")


if __name__ == "__main__":
    unittest.main()
