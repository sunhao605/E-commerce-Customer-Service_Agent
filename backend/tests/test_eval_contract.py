import unittest

from evals.runner import EvalRunner


class AnswerContractMatcherTests(unittest.TestCase):
    def test_normalizes_spaces_and_full_width_punctuation(self) -> None:
        missing, forbidden, diagnostics = EvalRunner._evaluate_answer_contract(
            "露营季活动支持满300减40；请以商品详情页和结算页为准。",
            {
                "must_convey": [
                    {"id": "promotion", "any_of": ["满 300 减 40"]},
                    {"id": "price_boundary", "all_of": ["商品详情页", "结算页"]},
                ]
            },
        )
        self.assertEqual(missing, [])
        self.assertEqual(forbidden, [])
        self.assertTrue(all(item["matched"] for item in diagnostics))

    def test_accepts_confirmed_shipped_synonym(self) -> None:
        missing, _, _ = EvalRunner._evaluate_answer_contract(
            "经核实，订单已经发货，物流正在运输中。",
            {"must_convey": [{"id": "order_shipped", "any_of": ["已发货"]}]},
        )
        self.assertEqual(missing, [])

    def test_does_not_use_internal_state_to_satisfy_answer_contract(self) -> None:
        missing, _, _ = EvalRunner._evaluate_answer_contract(
            "我会为你转接人工客服，暂不直接处理。",
            {"must_convey": [{"id": "order_shipped", "any_of": ["已发货"]}]},
        )
        self.assertEqual(missing, ["order_shipped"])

    def test_negated_phrase_does_not_count_as_positive_claim(self) -> None:
        missing, forbidden, _ = EvalRunner._evaluate_answer_contract(
            "本演示环境不执行真实资金退款，也不代表已到账。",
            {
                "must_convey": [{"id": "real_refund", "any_of": ["真实资金退款已完成"]}],
                "must_not_convey": [{"id": "refund_arrived", "any_of": ["已到账"]}],
            },
        )
        self.assertEqual(missing, ["real_refund"])
        self.assertEqual(forbidden, [])

        _, forbidden, _ = EvalRunner._evaluate_answer_contract(
            "目前系统无法直接承诺退货成功或自动退款。",
            {"must_not_convey": [{"id": "return_completed", "any_of": ["退货成功", "自动退款"]}]},
        )
        self.assertEqual(forbidden, [])

    def test_positive_forbidden_claim_is_detected(self) -> None:
        _, forbidden, _ = EvalRunner._evaluate_answer_contract(
            "退款已到账。",
            {"must_not_convey": [{"id": "refund_arrived", "any_of": ["已到账"]}]},
        )
        self.assertEqual(forbidden, ["refund_arrived"])


class EvalRunnerContractIntegrationTests(unittest.TestCase):
    def test_answer_contract_replaces_legacy_exact_signal_gate(self) -> None:
        runner = EvalRunner.__new__(EvalRunner)
        runner.backfilled_cases = []
        runner.load_cases = lambda: [
            {
                "case_id": "contract-v1",
                "user_message": "查询订单",
                "expected_signals": ["已经发货"],
                "answer_contract": {
                    "must_convey": [{"id": "order_shipped", "any_of": ["已发货", "已经发货"]}]
                },
            }
        ]
        runner._run_case = lambda case, session_id: {
            "answer": "订单已发货。",
            "tools": [],
            "citations": [],
            "trace_events": [],
            "session_state": {},
            "text": "订单已发货。",
        }
        report = runner.run()
        self.assertEqual(report.total, 1)
        self.assertEqual(report.passed, 1)
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.results[0].missing_signals, [])
        self.assertEqual(report.results[0].missing_answer_claims, [])
        self.assertEqual(report.summary["answer_contract_version"], "zh_semantic_v1_1")

    def test_runner_does_not_use_flattened_session_state_for_answer_claims(self) -> None:
        runner = EvalRunner.__new__(EvalRunner)
        runner.backfilled_cases = []
        runner.load_cases = lambda: [
            {
                "case_id": "answer-only",
                "user_message": "查询订单",
                "answer_contract": {
                    "must_convey": [{"id": "order_shipped", "any_of": ["已发货"]}]
                },
            }
        ]
        runner._run_case = lambda case, session_id: {
            "answer": "我会转接人工客服。",
            "tools": [],
            "citations": [],
            "trace_events": [],
            "session_state": {"order_status": "已发货"},
            "text": "我会转接人工客服。已发货",
        }
        report = runner.run()
        self.assertEqual(report.passed, 0)
        self.assertEqual(report.results[0].missing_answer_claims, ["order_shipped"])
        self.assertIn("answer_semantic_missing", report.results[0].failure_categories)


if __name__ == "__main__":
    unittest.main()
