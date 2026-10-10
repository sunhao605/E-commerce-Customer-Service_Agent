"""BC-001 / BC-004：最新订单复核、审核身份和终态恢复。不调用模型或真实商城。"""
import os
import threading
import unittest
from unittest.mock import patch

from api.schemas import ChatRequest, ChatResumeRequest
from agent.state import SUBMITTED_ACTIONS, WORKFLOW_CHECKPOINTS
from agent.trace import trace_store
from agent.workflows.resume import (
    SIMULATED_REVIEWER,
    TrustedReviewer,
    build_refund_workflow_checkpoint,
    business_recheck,
    resolve_trusted_reviewer,
    resume_from_checkpoint,
)


class ResumeGuardTest(unittest.TestCase):
    def setUp(self):
        self._checkpoints = dict(WORKFLOW_CHECKPOINTS)
        self._actions = dict(SUBMITTED_ACTIONS)
        WORKFLOW_CHECKPOINTS.clear()
        SUBMITTED_ACTIONS.clear()
        self.order = {
            "orderNo": "ORD1234",
            "userId": "U1001",
            "paymentStatus": "PAID",
            "fulfillmentStatus": "UNSHIPPED",
        }
        self.session = "bc-resume"
        request = ChatRequest(session_id=self.session, runtime_user_id="U1001", user_message="ORD1234 我要退款")
        self.workflow = build_refund_workflow_checkpoint(
            request=request, order=self.order, order_id="ORD1234", citations=[]
        )
        self.key = (self.session, self.workflow["workflow_id"])
        self.checkpoint = WORKFLOW_CHECKPOINTS[self.key]

    def tearDown(self):
        WORKFLOW_CHECKPOINTS.clear()
        WORKFLOW_CHECKPOINTS.update(self._checkpoints)
        SUBMITTED_ACTIONS.clear()
        SUBMITTED_ACTIONS.update(self._actions)

    def resume(self, decision, reviewer=SIMULATED_REVIEWER, **overrides):
        payload = dict(
            session_id=self.session,
            workflow_id=self.workflow["workflow_id"],
            resume_token=self.workflow["resume_token"],
            reviewer_id="ordinary-user",
            reviewer_role="customer",
            decision=decision,
        )
        payload.update(overrides)
        return resume_from_checkpoint(ChatResumeRequest(**payload), trace_store, reviewer)

    def test_missing_latest_order_does_not_use_snapshot(self):
        self.checkpoint["order_snapshot"] = {**self.order, "fulfillmentStatus": "SHIPPED"}
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=None):
            recheck = business_recheck(self.checkpoint)
            resumed = self.resume("approved")
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=self.order):
            matched = business_recheck(self.checkpoint)
        self.assertTrue(matched["passed"])
        self.assertFalse(recheck["passed"])
        self.assertEqual(recheck["reason"], "latest_order_unavailable")
        self.assertEqual(resumed.status, "blocked")
        self.assertIn("无法核实最新订单", resumed.answer)
        self.assertEqual(self.checkpoint["workflow"]["status"], "paused")
        self.assertNotIn("decision_record", self.checkpoint)
        self.assertEqual(SUBMITTED_ACTIONS, {})

    def test_query_error_and_missing_status_block_without_calling_it_unchanged(self):
        with patch("agent.workflows.resume.order_fact_from_ecommerce", side_effect=TimeoutError("down")):
            failed = self.resume("approved")
        self.assertEqual(failed.business_recheck["detail"], "query_failed")
        self.assertEqual(failed.resume_result["reason"], "latest_order_unavailable")
        self.assertNotEqual(failed.resume_result["reason"], "business_fact_drift")
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value={"orderNo": "ORD1234"}):
            incomplete = self.resume("approved")
        self.assertEqual(incomplete.business_recheck["detail"], "missing_status")
        self.assertIn("无法核实最新订单", incomplete.answer)
        self.assertEqual(self.checkpoint["workflow"]["status"], "paused")
        self.assertEqual(SUBMITTED_ACTIONS, {})

    def test_recheck_can_continue_after_query_recovers(self):
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=None):
            blocked = self.resume("approved")
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=self.order):
            resumed = self.resume("approved")
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(self.checkpoint["workflow"]["status"], "completed")
        self.assertEqual(len(SUBMITTED_ACTIONS), 1)
        self.assertEqual(self.checkpoint["decision_record"]["request_id"], resumed.resume_result["request_id"])

    def test_shipped_order_stops_the_original_refund(self):
        shipped = {**self.order, "fulfillmentStatus": "SHIPPED", "logisticsStatus": "IN_TRANSIT"}
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=shipped):
            resumed = self.resume("approved")
        self.assertEqual(resumed.resume_result["reason"], "business_fact_drift")
        self.assertIn("已发货", resumed.answer)
        self.assertNotIn("无法核实", resumed.answer)
        self.assertEqual(self.checkpoint["workflow"]["status"], "paused")
        self.assertEqual(SUBMITTED_ACTIONS, {})

    def test_offline_mirror_is_labeled_and_not_a_real_recheck(self):
        mirror = {**self.order, "_fact_source": "course_seed_mirror"}
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=mirror):
            resumed = self.resume("approved")
        self.assertTrue(resumed.business_recheck["passed"])
        self.assertTrue(resumed.business_recheck["simulated"])
        self.assertFalse(resumed.business_recheck["real_business_recheck"])
        self.assertIn("离线模拟复核", resumed.answer)
        self.assertNotIn("真实业务复核通过", resumed.answer)

    def test_self_claimed_reviewer_cannot_approve(self):
        before = dict(self.checkpoint["workflow"])
        for role in ("customer", "after_sale_manager"):
            resumed = self.resume("approved", reviewer=None, reviewer_role=role)
            self.assertEqual(resumed.status, "blocked")
            self.assertEqual(resumed.resume_result["reason"], "reviewer_unverified")
        self.assertEqual(self.checkpoint["workflow"]["status"], before["status"])
        self.assertEqual(SUBMITTED_ACTIONS, {})

    def test_trusted_reviewer_records_one_submission_on_the_checkpoint(self):
        reviewer = TrustedReviewer("ops", "ADMIN", "admin_session")
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=self.order):
            resumed = self.resume("approved", reviewer=reviewer)
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(resumed.resume_result["review_mode"], "admin_session")
        self.assertEqual(resumed.resume_result["reviewer_id"], "ops")
        self.assertEqual(self.checkpoint["workflow"]["status"], "completed")
        self.assertEqual(self.checkpoint["workflow"]["reviewer_id"], "ops")
        self.assertEqual(len(SUBMITTED_ACTIONS), 1)
        self.assertNotIn("离线模拟审核", resumed.answer)

    def test_repeat_approval_survives_a_later_query_failure(self):
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=self.order):
            first = self.resume("approved")
        with patch("agent.workflows.resume.order_fact_from_ecommerce", side_effect=TimeoutError("down")):
            second = self.resume("approved")
        self.assertEqual(second.status, "completed")
        self.assertTrue(second.resume_result["idempotent_replay"])
        self.assertEqual(second.resume_result["request_id"], first.resume_result["request_id"])
        self.assertEqual(len(SUBMITTED_ACTIONS), 1)

    def test_opposite_decision_keeps_the_saved_result(self):
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=self.order):
            self.resume("approved")
            conflict = self.resume("rejected")
            info = self.resume("needs_more_info")
        self.assertEqual(conflict.resume_result["reason"], "decision_conflict")
        self.assertEqual(info.resume_result["reason"], "decision_conflict")
        self.assertEqual(self.checkpoint["workflow"]["status"], "completed")
        self.assertEqual(self.checkpoint["decision_record"]["decision"], "approved")
        self.assertEqual(len(SUBMITTED_ACTIONS), 1)

    def test_repeat_rejection_does_not_submit(self):
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=self.order):
            first = self.resume("rejected")
            second = self.resume("rejected")
            conflict = self.resume("approved")
        self.assertEqual(first.status, "rejected")
        self.assertTrue(second.resume_result["idempotent_replay"])
        self.assertEqual(second.status, "rejected")
        self.assertEqual(conflict.resume_result["reason"], "decision_conflict")
        self.assertEqual(SUBMITTED_ACTIONS, {})
        self.assertEqual(self.checkpoint["workflow"]["status"], "rejected")

    def test_more_info_stays_pending_and_later_approval_rechecks(self):
        shipped = {**self.order, "fulfillmentStatus": "SHIPPED"}
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=self.order):
            pending = self.resume("needs_more_info")
        self.assertEqual(pending.status, "paused")
        self.assertEqual(self.checkpoint["workflow"]["pending_action"], "need_more_info")
        self.assertNotIn("decision_record", self.checkpoint)
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=shipped):
            blocked = self.resume("approved")
        self.assertEqual(blocked.resume_result["reason"], "business_fact_drift")
        self.assertEqual(self.checkpoint["workflow"]["status"], "paused")
        self.assertEqual(SUBMITTED_ACTIONS, {})
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=self.order):
            approved = self.resume("approved")
        self.assertEqual(approved.status, "completed")
        self.assertEqual(len(SUBMITTED_ACTIONS), 1)

    def test_concurrent_approvals_submit_once_and_conflicts_lose(self):
        barrier = threading.Barrier(2)
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=self.order):
            same = self._race(barrier, ("approved", "approved"))
        self.assertEqual([item.status for item in same], ["completed", "completed"])
        self.assertEqual(len({item.resume_result["request_id"] for item in same}), 1)
        self.assertEqual(len(SUBMITTED_ACTIONS), 1)
        self._rebuild()
        barrier = threading.Barrier(2)
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=self.order):
            mixed = self._race(barrier, ("approved", "rejected"))
        accepted = [item for item in mixed if item.resume_result["accepted"]]
        conflict = [item for item in mixed if item.resume_result["reason"] == "decision_conflict"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(conflict), 1)
        self.assertIn("已处理", conflict[0].answer)
        self.assertEqual(self.checkpoint["workflow"]["status"], accepted[0].status)
        self.assertEqual(len(SUBMITTED_ACTIONS), 1 if accepted[0].status == "completed" else 0)

    def test_lost_checkpoint_blocks_without_success(self):
        WORKFLOW_CHECKPOINTS.pop(self.key)
        resumed = self.resume("approved")
        self.assertEqual(resumed.status, "blocked")
        self.assertEqual(resumed.resume_result["reason"], "checkpoint_not_found")
        self.assertIn("凭证已失效", resumed.answer)
        self.assertNotIn("审批通过", resumed.answer)
        self.assertEqual(SUBMITTED_ACTIONS, {})

    def test_customer_session_is_rejected_and_simulated_flag_is_explicit(self):
        admin = TrustedReviewer("ops", "ADMIN", "admin_session")

        def identify(cookie):
            if cookie == "JSESSIONID=admin":
                return "admin", admin
            if cookie == "JSESSIONID=user":
                return "rejected", None
            return "unknown", None

        self.assertEqual(resolve_trusted_reviewer("JSESSIONID=admin", identify=identify).reviewer_id, "ops")
        with patch.dict(os.environ, {"AGENT_COURSE_SIMULATED_REVIEWER": "1"}):
            self.assertIsNone(resolve_trusted_reviewer("JSESSIONID=user", identify=identify))
            self.assertEqual(resolve_trusted_reviewer(None, identify=identify).source, "offline_simulated")
        with patch.dict(os.environ, {"AGENT_COURSE_SIMULATED_REVIEWER": "0"}):
            self.assertIsNone(resolve_trusted_reviewer(None, identify=identify))

    def test_http_ignores_self_claimed_role_until_server_trusts_the_caller(self):
        from fastapi.testclient import TestClient
        from api.routes import app

        client = TestClient(app)
        body = {
            "session_id": self.session,
            "workflow_id": self.workflow["workflow_id"],
            "resume_token": self.workflow["resume_token"],
            "reviewer_id": "course-reviewer-001",
            "reviewer_role": "after_sale_manager",
            "decision": "approved",
        }
        with patch.dict(os.environ, {"AGENT_COURSE_SIMULATED_REVIEWER": "0"}):
            rejected = client.post("/chat/resume", json=body)
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json()["resume_result"]["reason"], "reviewer_unverified")
        self.assertEqual(SUBMITTED_ACTIONS, {})
        admin = TrustedReviewer("ops", "ADMIN", "admin_session")
        with patch("agent.workflows.resume.identify_session", return_value=("admin", admin)), \
             patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=self.order), \
             patch.dict(os.environ, {"AGENT_COURSE_SIMULATED_REVIEWER": "0"}):
            approved = client.post("/chat/resume", json={**body, "reviewer_role": "customer"}, headers={"Cookie": "JSESSIONID=admin"})
        payload = approved.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["resume_result"]["review_mode"], "admin_session")
        self.assertEqual(payload["resume_result"]["reviewer_id"], "ops")
        self.assertEqual(self.checkpoint["workflow"]["status"], "completed")
        self.assertEqual(len(SUBMITTED_ACTIONS), 1)

    def _rebuild(self):
        WORKFLOW_CHECKPOINTS.clear()
        SUBMITTED_ACTIONS.clear()
        request = ChatRequest(session_id=self.session, runtime_user_id="U1001", user_message="ORD1234 我要退款")
        self.workflow = build_refund_workflow_checkpoint(
            request=request, order=self.order, order_id="ORD1234", citations=[]
        )
        self.key = (self.session, self.workflow["workflow_id"])
        self.checkpoint = WORKFLOW_CHECKPOINTS[self.key]

    def _race(self, barrier, decisions):
        results = []

        def call(decision):
            barrier.wait(timeout=5)
            results.append(self.resume(decision))

        threads = [threading.Thread(target=call, args=(decision,)) for decision in decisions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results


if __name__ == "__main__":
    unittest.main()
