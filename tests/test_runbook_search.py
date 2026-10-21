"""검색 동작 검증. 합성 벡터 테스트는 실제 의미 검색 품질을 증명하지 않는다."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.runbook_search import RunbookSelector, keyword_incident_type, load_documents


def monitoring(alerts, status="degraded"):
    return [{"source": "monitoring", "data": {"system_name": "test-service", "status": status, "alerts": alerts}}]


class StubEncoder:
    def __init__(self):
        self.calls = []
        self.fail = False

    def encode(self, sentences, **kwargs):
        self.calls.append(sentences)
        if self.fail:
            raise RuntimeError("private/path and secret must not leak")
        return [[1.0, 0.0] if "CPU" in text or "프로세서" in text else [0.0, 1.0]
                for text in sentences]


class RunbookSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        (self.directory / "cpu-spike.md").write_text("# CPU 매뉴얼\n프로세서 포화", encoding="utf-8")
        (self.directory / "memory-leak.md").write_text("# 메모리 매뉴얼\n메모리 누수", encoding="utf-8")

    def test_prefix_ranking_and_one_document_encoding(self):
        encoder = StubEncoder()
        selector = RunbookSelector(directory=self.directory, encoder=encoder)
        for _ in range(2):
            selection = selector.select("프로세서 사용 급증", monitoring(["CPU 91%"]))
            self.assertEqual([m["incident_type"] for m in selection["matches"]], ["cpu-spike"])
            self.assertAlmostEqual(selection["matches"][0]["score"], 1.0)
        self.assertEqual(len(encoder.calls), 3)
        self.assertTrue(all(s.startswith("passage: ") for s in encoder.calls[0]))
        self.assertTrue(encoder.calls[1][0].startswith("query: "))
        self.assertIn("CPU 91%", encoder.calls[1][0])

    def test_runtime_failure_is_visible_and_sanitized(self):
        encoder = StubEncoder()
        selector = RunbookSelector(directory=self.directory, encoder=encoder)
        encoder.fail = True
        selection = selector.select("증상", monitoring(["heap near limit"]))
        self.assertEqual(selection["mode"], "keyword")
        self.assertEqual(selection["matches"][0]["incident_type"], "memory-leak")
        self.assertEqual(selection["fallback_reason"], "search_failed:RuntimeError")
        self.assertNotIn("secret", str(selection))

    def test_load_failure_falls_back(self):
        with patch("agent.runbook_search.load_documents", side_effect=OSError("private")):
            selector = RunbookSelector(directory=self.directory, encoder=StubEncoder())
        selection = selector.select("지연", monitoring(["p95 latency 2.4s"]))
        self.assertEqual(selection["matches"][0]["incident_type"], "high-latency")
        self.assertEqual(selection["fallback_reason"], "initialization_failed:OSError")

    def test_keyword_preserves_rule_order_and_default(self):
        selector = RunbookSelector("keyword", self.directory)
        selection = selector.select("CPU", monitoring(["CPU 91%, latency high"]))
        self.assertEqual(selection["matches"][0]["incident_type"], "high-latency")
        self.assertEqual(len(selection["matches"]), 1)
        self.assertIsNone(selection["fallback_reason"])
        self.assertEqual(keyword_incident_type(monitoring([], "healthy")), "db-down")

    def test_new_document_discovered_on_reinitialization(self):
        first = load_documents(self.directory)
        (self.directory / "queue-backlog.md").write_text("# 큐 적체\n대기 메시지", encoding="utf-8")
        encoder = StubEncoder()
        def encode(sentences, **kwargs):
            return [[0, 0, 1] if "큐" in sentence else [1, 0, 0] if "CPU" in sentence else [0, 1, 0]
                    for sentence in sentences]
        encoder.encode = encode
        selector = RunbookSelector(directory=self.directory, encoder=encoder)
        self.assertEqual(len(selector.documents), len(first) + 1)
        self.assertIn("queue-backlog", [m["incident_type"] for m in selector.select("큐", [])['matches']])

    def test_invalid_embeddings_do_not_produce_fake_success(self):
        for vectors in ([[0, 0], [0, 0]], [[float("nan"), 1], [1, 0]], [[1], [1, 2]], []):
            with self.subTest(vectors=vectors):
                encoder = StubEncoder()
                with patch.object(encoder, "encode", return_value=vectors):
                    selector = RunbookSelector(directory=self.directory, encoder=encoder)
                self.assertIsNotNone(selector.select("질의", [])["fallback_reason"])

    def test_query_dimension_failure(self):
        encoder = StubEncoder()
        selector = RunbookSelector(directory=self.directory, encoder=encoder)
        with patch.object(encoder, "encode", return_value=[[1, 0, 0]]):
            self.assertEqual(selector.select("질의", [])["fallback_reason"], "search_failed:ValueError")

    def test_model_initialization_failure_is_visible(self):
        import types
        fake_module = types.SimpleNamespace(SentenceTransformer=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("private")))
        with patch.dict("sys.modules", {"sentence_transformers": fake_module}):
            selector = RunbookSelector(directory=self.directory)
        self.assertEqual(selector.select("CPU", monitoring(["CPU high"]))["fallback_reason"],
                         "initialization_failed:RuntimeError")

    def test_empty_and_unsafe_documents_are_rejected(self):
        for name, body in [("empty.md", ""), ("bad name.md", "# invalid")]:
            path = self.directory / name
            path.write_text(body)
            with self.assertRaises(ValueError):
                load_documents(self.directory)
            path.unlink()

    def test_bad_mode_rejected(self):
        with self.assertRaises(ValueError):
            RunbookSelector("typo", self.directory)



class IndependentSymptomEncoder:
    """전체 순위는 네트워크/DB로 재현하고 분리 질의의 독립 지지만 바꿉니다."""
    def encode(self, sentences, **kwargs):
        vectors = []
        for sentence in sentences:
            if sentence.startswith('passage: '):
                vectors.append([1.0, 0.0] if '네트워크' in sentence else [0.8, 0.6])
            elif '모니터링 알림:\n' in sentence and '타임아웃과' in sentence:
                vectors.append([1.0, 0.0])
            elif 'DB 연결 불가' in sentence:
                vectors.append([0.8, 0.6])
            else:
                vectors.append([1.0, 0.0])
        return vectors


class CandidateSelectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        directory = Path(self.folder.name)
        (directory/'network-timeout.md').write_text('# 네트워크 타임아웃\n네트워크 점검')
        (directory/'db-down.md').write_text('# DB Down\nDB 연결 불가')
        self.selector = RunbookSelector(directory=directory, encoder=IndependentSymptomEncoder())

    def test_single_network_request_does_not_fill_second_slot_with_db(self):
        selection = self.selector.select('gateway-service 네트워크 타임아웃 장애를 진단해주세요',
                                         monitoring(['upstream timeout to payments-service (504)', 'network packet loss 3%']))
        self.assertEqual([m['incident_type'] for m in selection['matches']], ['network-timeout'])

    def test_second_document_requires_independent_symptom_support(self):
        selection = self.selector.select('네트워크 타임아웃과 DB 연결 불가를 함께 진단해주세요', [])
        self.assertEqual([m['incident_type'] for m in selection['matches']], ['network-timeout', 'db-down'])

    def test_multiple_clauses_with_same_meaning_do_not_add_db(self):
        selection = self.selector.select('네트워크 타임아웃과 네트워크 패킷 손실을 확인해주세요', [])
        self.assertEqual([m['incident_type'] for m in selection['matches']], ['network-timeout'])


    def test_connected_predicates_support_two_distinct_symptoms(self):
        selection = self.selector.select('네트워크 타임아웃이 발생했고 DB 연결 불가도 나타납니다', [])
        self.assertEqual({m['incident_type'] for m in selection['matches']}, {'network-timeout', 'db-down'})


    def test_long_enumeration_has_bounded_query_embedding_batch(self):
        calls = []
        original = self.selector.encoder.encode
        def record(sentences, **kwargs):
            calls.append(sentences)
            return original(sentences, **kwargs)
        self.selector.encoder.encode = record
        self.selector.select(', '.join(f'네트워크 문제 {i}' for i in range(50)), [])
        self.assertLessEqual(len(calls[-1]), 5)


if __name__ == "__main__":
    unittest.main()
