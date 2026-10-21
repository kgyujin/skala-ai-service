"""평가 분리와 지표 계산이 결과를 부풀리지 않는지 검증한다."""
import json
import unittest
from unittest.mock import patch
from eval.run_eval import CASE_PATH, evaluate, score_case
from agent.runbook_search import RunbookSelector


class EvaluationTests(unittest.TestCase):
    def test_dataset_split_and_reference_labels(self):
        from agent.runbook_search import load_documents, RUNBOOK_DIR
        cases = json.loads(CASE_PATH.read_text())
        self.assertEqual(sum(c['split'] == 'development' for c in cases), 12)
        self.assertEqual(sum(c['split'] == 'evaluation' for c in cases), 24)
        self.assertEqual(len({c['query'] for c in cases}), 36)
        known = {d.incident_type for d in load_documents(RUNBOOK_DIR)}
        for case in cases:
            self.assertTrue(set(case['expected']) <= known)
            self.assertTrue(case['reason'])

    def test_extra_candidate_does_not_improve_top1(self):
        score = score_case(['cpu-spike'], ['memory-leak', 'cpu-spike'])
        self.assertFalse(score['top1_correct'])
        self.assertEqual(score['recall'], 1)
        self.assertEqual(score['precision'], 0.5)
        self.assertEqual(score['unnecessary'], 1)
        self.assertIsNone(score_case([], ['db-down'])['top1_correct'])

    def test_pipeline_fixture_supplies_explicit_target_without_changing_catalog(self):
        import asyncio
        from eval.run_eval import pipeline_case
        from agent.system_targets import load_known_systems
        case = json.loads(CASE_PATH.read_text())[0]
        before = load_known_systems()
        run = asyncio.run(pipeline_case(case, RunbookSelector('keyword')))
        self.assertFalse(run['needs_clarification'])
        self.assertTrue(run['selected'])
        self.assertIn('조회 대상: test-service', run['query'])
        self.assertEqual(load_known_systems(), before)

    def test_evaluation_rejects_silent_fallback(self):
        selector = RunbookSelector('keyword')
        with patch.object(selector, 'select', return_value={'fallback_reason': 'search_failed'}):
            with self.assertRaises(RuntimeError):
                evaluate([{'query':'증상', 'monitoring':[]}], {'rag':selector}, 1)


if __name__ == '__main__':
    unittest.main()
