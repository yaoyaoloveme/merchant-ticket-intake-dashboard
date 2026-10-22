import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as dashboard


class TianrunAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old_db = dashboard.DB_PATH
        self.old_cookie = dashboard.TIANRUN_COOKIE_PATH
        self.old_assign = dashboard.TIANRUN_ASSIGN_ENABLED
        dashboard.DB_PATH = root / "tickets.db"
        dashboard.TIANRUN_COOKIE_PATH = root / "tianrun_cookie.txt"
        dashboard.TIANRUN_ASSIGN_ENABLED = False
        dashboard.TIANRUN_PREVIEWS.clear()
        conn = dashboard.get_db()
        conn.execute(
            "INSERT INTO tickets (ticket_id,shop_id,brand,category_raw,category,description,status,inspect_status,tags,handler,created_at,created_date,duration_text,duration_minutes,inspect_completed_at,response_text,response_minutes,pipeline_text,is_ka,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("100001", "200", "", "多付/漏付", "多付/漏付", "测试工单", "处理中", "未稽查", "", "楚家浩", "2026-10-20T09:00:00", "2026-10-20", "", None, "", "", None, "", 0, "2026-10-20T09:00:00"),
        )
        conn.commit()
        conn.close()
        self.client = dashboard.app.test_client()

    def tearDown(self):
        dashboard.DB_PATH = self.old_db
        dashboard.TIANRUN_COOKIE_PATH = self.old_cookie
        dashboard.TIANRUN_ASSIGN_ENABLED = self.old_assign
        self.temp.cleanup()

    def test_status_never_returns_cookie_value(self):
        dashboard.save_tianrun_cookie("TSESSIONID=top-secret; acw_tc=also-secret")
        result = self.client.get("/api/tianrun/status").get_json()
        self.assertTrue(result["ok"])
        body = str(result)
        self.assertNotIn("top-secret", body)
        self.assertNotIn("also-secret", body)
        self.assertEqual(oct(os.stat(dashboard.TIANRUN_COOKIE_PATH).st_mode & 0o777), "0o600")

    @patch("app.tianrun_users", return_value=[{"handler_id": "uuid", "name": "楚家浩", "cno": "05435"}])
    def test_cookie_can_be_saved_and_verified(self, _users):
        response = self.client.put(
            "/api/tianrun/cookie",
            json={"cookie": "TSESSIONID=fresh-value; acw_tc=token"},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("fresh-value", response.get_data(as_text=True))

    @patch("app.exact_tianrun_user", return_value={"handler_id": "uuid", "name": "楚家浩", "cno": "05435"})
    def test_preview_works_but_execute_is_locked(self, _user):
        preview = self.client.post(
            "/api/tianrun/assignment/preview",
            json={"ticket_id": "100001", "recipient": "05435", "reason": "安全预览"},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(preview.status_code, 200)
        token = preview.get_json()["data"]["confirm_token"]
        execute = self.client.post(
            "/api/tianrun/assignment/execute",
            json={"confirm_token": token},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(execute.status_code, 403)
        self.assertIn("关闭", execute.get_json()["error"])

    def test_remote_clients_are_rejected(self):
        response = self.client.get("/api/tianrun/status", environ_base={"REMOTE_ADDR": "10.0.0.8"})
        self.assertEqual(response.status_code, 403)
        page = self.client.get("/dispatch", environ_base={"REMOTE_ADDR": "10.0.0.8"})
        self.assertEqual(page.status_code, 403)

    def test_dispatch_is_a_separate_local_page(self):
        dashboard_html = self.client.get("/").get_data(as_text=True)
        dispatch_html = self.client.get("/dispatch").get_data(as_text=True)
        self.assertNotIn('id="dispatchPanel"', dashboard_html)
        self.assertNotIn("dispatch.js", dashboard_html)
        self.assertNotIn("dispatch.css", dashboard_html)
        self.assertIn('id="dispatchPageLink"', dashboard_html)
        self.assertIn('id="dispatchPanel"', dispatch_html)
        self.assertIn('dispatch.js', dispatch_html)
        self.assertNotIn('app.js', dispatch_html)

    def test_team_contains_the_16_configured_members(self):
        response = self.client.get("/api/tianrun/team")
        names = [row["name"] for row in response.get_json()["data"]]
        self.assertEqual(len(names), 16)
        self.assertIn("楚家浩", names)
        self.assertIn("周雪鑫", names)

    def test_team_counts_latest_non_failed_assignment_by_category(self):
        today = dashboard.date.today().isoformat()
        conn = dashboard.get_db()
        conn.executemany(
            "INSERT INTO tickets (ticket_id,category,fetched_at) VALUES (?,?,?)",
            [
                ("100002", "批量核实", f"{today}T08:00:00"),
                ("100003", "回访工单", f"{today}T08:00:00"),
                ("100004", "介入问题", f"{today}T08:00:00"),
            ],
        )
        audit_rows = [
            ("100001", "楚家浩", f"{today}T08:00:00", "已提交待核验"),
            ("100001", "曹彩月", f"{today}T09:00:00", "已提交待核验"),
            ("100001", "陈小如", f"{today}T10:00:00", "失败"),
            ("100002", "楚家浩", f"{today}T09:10:00", "已提交待核验"),
            ("100003", "楚家浩", f"{today}T09:20:00", "幂等完成"),
            ("100004", "楚家浩", f"{today}T09:30:00", "失败"),
            ("100002", "陈小如", "2026-10-19T17:30:00", "已提交待核验"),
        ]
        conn.executemany(
            "INSERT INTO assignment_audit (ticket_id,target_name,target_handler_id,requested_at,state) VALUES (?,?,?,?,?)",
            [(ticket_id, name, f"uuid-{name}", requested_at, state) for ticket_id, name, requested_at, state in audit_rows],
        )
        conn.commit()
        conn.close()

        response = self.client.get("/api/tianrun/team")
        members = {row["name"]: row for row in response.get_json()["data"]}
        self.assertEqual(members["楚家浩"]["assigned_today"], 2)
        self.assertEqual(members["楚家浩"]["category_counts"]["批量核实"], 1)
        self.assertEqual(members["楚家浩"]["category_counts"]["回访工单"], 1)
        self.assertEqual(members["曹彩月"]["assigned_today"], 1)
        self.assertEqual(members["曹彩月"]["category_counts"]["多付/漏付"], 1)
        self.assertEqual(members["陈小如"]["assigned_today"], 0)

    @patch("app.pending_guard_tickets")
    def test_pending_ticket_pool_returns_guard_queue(self, pending):
        pending.return_value = ([{
            "ticket_id": "100001", "shop_id": "200", "brand": "", "category_raw": "多付/漏付问题核实",
            "category": "多付/漏付", "description": "测试工单", "status": "待领取",
            "handler": "值守客服问题处理组", "created_at": "2026-10-20T09:00:00",
            "duration_text": "2小时", "duration_minutes": 120, "fetched_at": "2026-10-20T11:00:00",
            "source_url": "http://example.invalid/100001",
        }], None)
        response = self.client.get("/api/tianrun/pending-tickets?refresh=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["count"], 1)

    @patch("app.exact_tianrun_user")
    @patch("app.pending_guard_tickets")
    def test_batch_preview_round_robins_tickets(self, pending, exact_user):
        pending.return_value = ([
            {"ticket_id": ticket_id, "shop_id": "200", "brand": "", "category_raw": "介入问题",
             "category": "介入问题", "description": f"测试{ticket_id}", "status": "待领取",
             "handler": "值守客服问题处理组", "created_at": "2026-10-20T09:00:00",
             "duration_text": "2小时", "duration_minutes": 120, "fetched_at": "2026-10-20T11:00:00"}
            for ticket_id in ("100001", "100002", "100003")
        ], None)
        exact_user.side_effect = lambda name: {"handler_id": f"uuid-{name}", "name": name, "cno": f"cno-{name}"}
        response = self.client.post(
            "/api/tianrun/assignment/batch-preview",
            json={"ticket_ids": ["100001", "100002", "100003"], "members": ["楚家浩", "曹彩月"], "reason": "早班分派"},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(response.status_code, 200)
        assignments = response.get_json()["data"]["assignments"]
        self.assertEqual([row["user"]["name"] for row in assignments], ["楚家浩", "曹彩月", "楚家浩"])

    @patch("app.tianrun_json", return_value=(200, {"status": 200, "message": "批量分配任务创建成功"}))
    @patch("app.exact_tianrun_user", return_value={"handler_id": "uuid", "name": "楚家浩", "cno": "05435"})
    @patch("app.fetch_open_handler_rows")
    def test_batch_execute_uses_preview_button_as_the_only_confirmation(self, live_rows, _user, tianrun):
        dashboard.TIANRUN_ASSIGN_ENABLED = True
        ticket = {
            "ticket_id": "100001", "shop_id": "200", "brand": "", "category_raw": "多付/漏付",
            "category": "多付/漏付", "description": "测试工单", "status": "待领取",
            "handler": "值守客服问题处理组", "created_at": "2026-10-20T09:00:00",
        }
        live_rows.return_value = [ticket]
        dashboard.TIANRUN_PREVIEWS["single-confirm-token"] = {
            "mode": "batch",
            "assignments": [{"ticket": ticket, "user": {"handler_id": "uuid", "name": "楚家浩", "cno": "05435"}}],
            "reason": "早班分派",
            "expires_at": dashboard.time.time() + 300,
        }

        response = self.client.post(
            "/api/tianrun/assignment/batch-execute",
            json={"confirm_token": "single-confirm-token"},
            headers={"Origin": "http://localhost"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["success"], 1)
        tianrun.assert_called_once()

    def queue_ticket(self, ticket_id="100001", description="测试问题", category="多付/漏付"):
        return {"ticket_id": ticket_id, "shop_id": "200", "brand": "", "category_raw": category,
                "category": category, "description": description, "status": "待领取",
                "handler": dashboard.GUARD_QUEUE_HANDLER, "created_at": "2026-10-20T09:00:00",
                "duration_minutes": 120, "duration_text": "2小时", "fetched_at": dashboard.datetime.now().isoformat(timespec="seconds")}

    def install_preview(self, token, tickets):
        dashboard.TIANRUN_ASSIGN_ENABLED = True
        user = {"handler_id": "uuid", "name": "楚家浩", "cno": "05435"}
        dashboard.TIANRUN_PREVIEWS[token] = {"mode": "batch", "assignments": [{"ticket": row, "user": user} for row in tickets],
                                           "reason": "早班分派", "expires_at": dashboard.time.time() + 300}
        return user

    @patch("app.team_assignment_statistics")
    def test_recommendation_balances_existing_total_then_category(self, statistics):
        statistics.return_value = [
            {"name": "楚家浩", "assigned_today": 4, "category_counts": {"介入问题": 0}},
            {"name": "曹彩月", "assigned_today": 1, "category_counts": {"介入问题": 1}},
        ]
        users = [{"name": name} for name in ["楚家浩", "曹彩月"]]
        tickets = [dashboard.enrich_dispatch_ticket(self.queue_ticket(str(i), category="介入问题")) for i in range(4)]
        assignments = dashboard.recommend_assignments(tickets, users, {}, "")
        self.assertEqual([item["user"]["name"] for item in assignments], ["曹彩月", "曹彩月", "曹彩月", "楚家浩"])

    @patch("app.exact_tianrun_user")
    @patch("app.pending_guard_tickets")
    def test_risk_designation_and_manual_override_are_enforced(self, pending, exact):
        pending.return_value = ([self.queue_ticket("100001", "顾客情绪激动，威胁砸店"), self.queue_ticket("100002")], None)
        exact.side_effect = lambda name: {"name": name, "handler_id": "uuid-" + name, "cno": name}
        response = self.client.post("/api/tianrun/assignment/batch-preview", json={
            "ticket_ids": ["100001", "100002"], "members": ["楚家浩", "曹彩月"], "reason": "早班",
            "risk_recipient": "曹彩月", "overrides": {"100002": "楚家浩"}})
        self.assertEqual(response.status_code, 200)
        rows = response.get_json()["data"]["assignments"]
        self.assertEqual([row["user"]["name"] for row in rows], ["曹彩月", "楚家浩"])
        self.assertEqual(rows[0]["ticket"]["risk_level"], "紧急")
        self.assertIn("砸店", rows[0]["ticket"]["risk_hits"][0]["matched_text"])
        denied = self.client.post("/api/tianrun/assignment/batch-preview", json={
            "ticket_ids": ["100001"], "members": ["楚家浩"], "reason": "早班", "overrides": {"100001": "曹彩月"}})
        self.assertEqual(denied.status_code, 400)

    @patch("app.pending_guard_tickets", return_value=([], "实时读取失败，本地缓存"))
    @patch("app.exact_tianrun_user")
    def test_preview_rejects_stale_fallback(self, exact, _pending):
        response = self.client.post("/api/tianrun/assignment/batch-preview", json={
            "ticket_ids": ["100001"], "members": ["楚家浩"], "reason": "早班"})
        self.assertEqual(response.status_code, 503)
        exact.assert_not_called()

    @patch("app.tianrun_json")
    @patch("app.exact_tianrun_user")
    @patch("app.fetch_open_handler_rows")
    def test_uncertain_write_is_durable_and_stops_remaining_tickets(self, live, exact, send):
        rows = [self.queue_ticket("100001"), self.queue_ticket("100002")]
        exact.return_value = self.install_preview("uncertain", rows)
        live.return_value = rows
        def disconnect(*args):
            conn = dashboard.get_db()
            state = conn.execute("SELECT state FROM assignment_audit ORDER BY id DESC LIMIT 1").fetchone()[0]
            conn.close()
            self.assertEqual(state, "正在提交")
            raise dashboard.TianrunUncertainError("提交连接中断")
        send.side_effect = disconnect
        response = self.client.post("/api/tianrun/assignment/batch-execute", json={"confirm_token": "uncertain"})
        self.assertEqual(response.status_code, 200)
        results = response.get_json()["data"]["results"]
        self.assertEqual([row["state"] for row in results], ["提交结果待确认", "未提交"])
        self.assertFalse(results[0]["retryable"])
        self.assertTrue(results[1]["retryable"])
        send.assert_called_once()
        self.assertIn("100001", dashboard.unresolved_assignments())
        self.assertEqual(next(row for row in dashboard.team_assignment_statistics() if row["name"] == "楚家浩")["assigned_today"], 0)

    @patch("app.tianrun_json", return_value=(200, {"status": 200, "message": "批量分配任务创建成功"}))
    @patch("app.exact_tianrun_user")
    @patch("app.fetch_open_handler_rows")
    def test_two_previews_cannot_submit_same_ticket_twice(self, live, exact, send):
        rows = [self.queue_ticket()]
        exact.return_value = self.install_preview("first", rows)
        self.install_preview("second", rows)
        live.return_value = rows  # The secondary source still shows the old queue.
        first = self.client.post("/api/tianrun/assignment/batch-execute", json={"confirm_token": "first"})
        second = self.client.post("/api/tianrun/assignment/batch-execute", json={"confirm_token": "second"})
        reused = self.client.post("/api/tianrun/assignment/batch-execute", json={"confirm_token": "first"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(reused.status_code, 400)
        send.assert_called_once()

    @patch("app.tianrun_json", side_effect=PermissionError("天润登录已失效"))
    @patch("app.exact_tianrun_user")
    @patch("app.fetch_open_handler_rows")
    def test_auth_failure_reports_unsubmitted_remainder(self, live, exact, send):
        rows = [self.queue_ticket("100001"), self.queue_ticket("100002")]
        exact.return_value = self.install_preview("expired", rows)
        live.return_value = rows
        response = self.client.post("/api/tianrun/assignment/batch-execute", json={"confirm_token": "expired"})
        data = response.get_json()["data"]
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["failed"], 1)
        self.assertEqual(data["unsubmitted"], 1)
        self.assertEqual(data["success"], 0)
        send.assert_called_once()

    @patch("app.fetch_open_handler_rows")
    def test_verification_requires_exact_ticket_and_target_after_submission(self, live):
        user = {"name": "楚家浩", "cno": "05435", "handler_id": "uuid"}
        for ticket_id in ["100001", "100002", "100003", "100004"]:
            dashboard.save_assignment_audit(self.queue_ticket(ticket_id), user, "测试", "已提交待核验", 200, "已受理")
        rows = [self.queue_ticket("100001"), self.queue_ticket("100002"), self.queue_ticket("100003")]
        rows[0].update(handler="楚家浩", fetched_at="2099-01-01T00:00:00")
        rows[1].update(handler="曹彩月", fetched_at="2099-01-01T00:00:00")
        rows[2].update(handler="楚家浩", fetched_at="2000-01-01T00:00:00")
        live.return_value = rows
        response = self.client.post("/api/tianrun/assignment/verify", json={})
        self.assertEqual(response.get_json()["data"]["verified"], ["100001"])
        self.assertEqual(response.get_json()["data"]["remaining"], 3)
        audit = self.client.get("/api/tianrun/assignment/audit").get_json()["data"]
        self.assertEqual(sum(row["state"] == "已核验" for row in audit), 1)

    @patch("app.tianrun_session")
    def test_timeout_write_is_uncertain_but_lookup_is_read_failure(self, session):
        session.return_value.request.side_effect = dashboard.requests.Timeout("timeout")
        with self.assertRaises(dashboard.TianrunUncertainError):
            dashboard.tianrun_json("PUT", "/api/ticket/batchAssign", {})
        with self.assertRaises(RuntimeError) as error:
            dashboard.tianrun_json("POST", "/api/user/page", {})
        self.assertNotIsInstance(error.exception, dashboard.TianrunUncertainError)

    def test_export_excludes_assignment_ui_scripts_and_endpoints(self):
        import publish_snapshot as publisher
        today = dashboard.date.today().isoformat()
        conn = dashboard.get_db()
        conn.execute("UPDATE tickets SET created_date=?,fetched_at=?", (today, f"{today}T10:00:00"))
        conn.execute("INSERT INTO sync_runs (started_at,finished_at,status) VALUES (?,?,?)", (f"{today}T10:00:00", f"{today}T10:00:00", "ok"))
        conn.commit()
        conn.close()
        destination = Path(self.temp.name) / "public"
        with patch.object(publisher, "DOCS", destination), \
             patch.object(dashboard, "build_dashboard", return_value={}), \
             patch.object(dashboard, "build_root_cause_ledger", return_value={"records": []}):
            publisher.export_snapshot()
        html = (destination / "index.html").read_text()
        script = (destination / "assets/app.js").read_text()
        for marker in ["dispatchPanel", "dispatch.js", "dispatch.css", "楚家浩", "{{", "tianrunCookie"]:
            self.assertNotIn(marker, html)
        self.assertNotIn("DashboardDispatch", script)
        self.assertNotIn("/api/tianrun", script)
        self.assertFalse((destination / "assets/dispatch.js").exists())

    def test_failed_attempt_cannot_hide_a_previous_unverified_submission(self):
        user = {"name": "楚家浩", "handler_id": "uuid", "cno": "05435"}
        ticket = self.queue_ticket()
        dashboard.save_assignment_audit(ticket, user, "早班", "已提交待核验", 200, "已受理")
        dashboard.save_assignment_audit(ticket, user, "重试", "失败", 403, "登录失效")
        self.assertIn(ticket["ticket_id"], dashboard.unresolved_assignments())


if __name__ == "__main__":
    unittest.main()
