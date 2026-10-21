"""보고된 복합 증상 답변과 세 번 반려 후 종료 경로의 회귀 검증."""
import unittest
from pathlib import Path

from agent.answer_grounding import CPU_UNCERTAINTY, WRITING_RULES, grounding_issues
from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM
from agent.graph import build_graph
from agent.session_store import SqliteSessionStore
from tests.test_graph_rag import FixedMCP, TwoRunbooks


MONITORING = {"system_name": "cache-service", "status": "degraded", "cpu_percent": 47.3,
              "memory_percent": 91.7, "alerts": ["memory usage climbing steadily over 6h", "heap near limit"]}
COLLECTED = [{"source": "monitoring", "data": MONITORING}] + [
    {"source": "runbook", "incident_type": incident,
     "data": (Path("data/runbooks") / f"{incident}.md").read_text()}
    for incident in ("memory-leak", "cpu-spike")]
VALID = ("1) cache-service CPU는 47.3%, 메모리는 91.7%입니다.\n"
         + CPU_UNCERTAINTY + ". 과거 지표를 확인합니다.\n"
         "2) 메모리 런북의 90% 이상 조건에 해당하며 재시작은 임시 조치로 검토합니다.\n"
         "3) 배포가 원인으로 확인되면 롤백을 검토합니다.")
REPORTED = ("1) 메모리 사용률은 91.7%로 재시작은 임시 조치로 기록합니다.\n"
            "2) CPU 47.3%는 임계치 90%를 넘지 않습니다.\n"
            "3) 메모리 누수 원인을 파악한 후 해당 배포를 롤백하고 메모리 상한선을 재설정합니다.")


class GroundingTests(unittest.TestCase):
    def test_reported_answer_rejects_unconditional_rollback_and_missing_cpu_context(self):
        issues = grounding_issues(REPORTED, COLLECTED)
        self.assertTrue(any("롤백" in issue for issue in issues))
        self.assertTrue(any("CPU 관측" in issue for issue in issues))
        self.assertFalse(any("백분율" in issue for issue in issues))

    def test_observed_values_and_original_runbook_threshold_pass(self):
        self.assertEqual(grounding_issues(VALID, COLLECTED), [])

    def test_invented_threshold_and_analysis_do_not_become_evidence(self):
        draft = VALID + "\n알림 기준은 80%입니다."
        collected = COLLECTED + [{"source": "analysis", "data": "기준은 80%입니다."}]
        self.assertTrue(any("80.0" in issue for issue in grounding_issues(draft, collected)))

    def test_condition_in_another_sentence_does_not_authorize_rollback(self):
        draft = VALID.replace("배포가 원인으로 확인되면 롤백을 검토합니다.",
                              "배포가 원인으로 확인되면 로그를 확인합니다. 해당 배포를 롤백합니다.")
        self.assertTrue(any("롤백" in issue for issue in grounding_issues(draft, COLLECTED)))

    def test_negated_rollback_does_not_require_recommendation_condition(self):
        draft = VALID.replace("배포가 원인으로 확인되면 롤백을 검토합니다.", "롤백을 하지 않습니다.")
        self.assertEqual(grounding_issues(draft, COLLECTED), [])

    def test_cpu_caveat_not_required_for_unrelated_runbook(self):
        collected = [entry for entry in COLLECTED if entry.get("incident_type") != "cpu-spike"]
        self.assertEqual(grounding_issues(VALID.replace(CPU_UNCERTAINTY, ""), collected), [])


class CacheMCP(FixedMCP):
    async def call_tool(self, name, arguments):
        return dict(MONITORING)


class ScriptedWriter:
    def __init__(self, answers):
        self.answers = answers
        self.feedback = []

    async def write(self, query, collected, attempt, feedback, on_token=None):
        self.feedback.append(feedback)
        answer = self.answers[min(attempt - 1, len(self.answers) - 1)]
        if on_token:
            on_token(answer)
        return answer, {"usd": 0.0}


class GroundingGraphTests(unittest.IsolatedAsyncioTestCase):
    def make_deps(self, answers):
        store = SqliteSessionStore(Path(":memory:"))
        self.addCleanup(store.close)
        return SupervisorDeps(FastLLM("fake-fast", 0), ScriptedWriter(answers), CacheMCP(),
                              store, "v2", TwoRunbooks())

    async def test_bad_answer_rewritten_with_specific_feedback(self):
        deps = self.make_deps([REPORTED, VALID])
        result = await build_graph(deps).ainvoke({"query": "CPU 급증과 cache-service 메모리 누수 장애를 함께 진단해주세요",
                                                 "events": [], "history": []})
        self.assertEqual(result["validation_attempts"], 2)
        self.assertIn("배포가 원인으로 확인되면", deps.quality_llm.feedback[1])
        self.assertEqual(result["draft"], VALID)
        self.assertIn("참고 런북:", result["final"])

    async def test_three_failures_do_not_force_pass_or_expose_invalid_final(self):
        deps = self.make_deps([REPORTED + " 임계치는 80%입니다."])
        result = await build_graph(deps).ainvoke({"query": "cache-service CPU 급증 메모리 누수 진단",
                                                 "events": [], "history": []})
        self.assertEqual(result["validation_attempts"], 3)
        verdicts = [e["verdict"] for e in result["events"] if e["type"] == "Validation"]
        self.assertEqual(verdicts, ["불합격", "불합격", "검수 실패(사실 요약 제공)"])
        self.assertIn("검수를 통과하지 못해", result["final"])
        self.assertIn("47.3%", result["final"])
        self.assertIn("91.7%", result["final"])
        self.assertNotIn("80%", result["final"])
        self.assertNotIn("롤백하고", result["final"])
        self.assertIn("실행하지 않았습니다", result["final"])
        self.assertIn("참고 런북:", result["final"])

    async def test_real_backends_share_generation_contract_without_network(self):
        from unittest.mock import patch, AsyncMock
        from agent.ollama_llm import OllamaQualityLLM
        from agent.openai_llm import OpenAIQualityLLM
        # 생성 지침은 실제 네트워크 호출 직전 인자를 검사합니다.
        with patch.object(OllamaQualityLLM, "_chat_stream", new_callable=AsyncMock,
                          return_value=(VALID, {"usd": 0})) as request:
            model = OllamaQualityLLM("test", 0)
            await model.write("진단", COLLECTED, 1, None)
            self.assertEqual(request.call_args.args[0], WRITING_RULES)
        with patch.object(OpenAIQualityLLM, "_chat", return_value=(VALID, {"usd": 0})) as request:
            model = OpenAIQualityLLM("test", 0)
            await model.write("진단", COLLECTED, 1, None)
            self.assertEqual(request.call_args.args[0], WRITING_RULES)


class GroundingHttpTests(unittest.TestCase):
    def test_chat_and_sse_return_fact_summary_after_repeated_rejection(self):
        import json
        import tempfile
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        import server
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(server, "MCPClient", CacheMCP), \
                 patch.object(server, "RunbookSelector", return_value=TwoRunbooks()), \
                 patch.object(server, "SqliteSessionStore", side_effect=lambda: SqliteSessionStore(Path(folder) / "sessions.db")), \
                 patch.object(server, "_build_llms", return_value=("fake", FastLLM("test", 0), ScriptedWriter([REPORTED]))), \
                 patch.dict("os.environ", {"SUPERVISOR_VERSION": "v2"}):
                with TestClient(server.app) as client:
                    response = client.post("/chat", json={"message": "cache-service CPU 급증 진단", "thread_id": "guard-http"})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(set(response.json()), {"reply", "events", "metadata"})
                    self.assertIn("검수를 통과하지 못해", response.json()["reply"])
                    with client.stream("GET", "/chat/stream", params={"message": "cache-service CPU 급증 진단", "thread_id": "guard-sse"}) as stream:
                        frames = [line[6:] for line in stream.iter_lines() if line.startswith("data: ")]
                    self.assertEqual(frames[-1], "[DONE]")
                    events = [json.loads(frame) for frame in frames[:-1]]
                    self.assertEqual([e["verdict"] for e in events if e.get("type") == "Validation"],
                                     ["불합격", "불합격", "검수 실패(사실 요약 제공)"])
                    final = next(e["reply"] for e in events if e.get("type") == "FinalAnswer")
                    self.assertIn("검수를 통과하지 못해", final)
                    self.assertNotIn("해당 배포를 롤백하고", final)
                    self.assertIn("참고 런북:", final)


class NetworkAnswerTests(unittest.TestCase):
    def test_reported_network_steps_keep_original_adjustment_condition(self):
        collected = [{"source": "monitoring", "data": {
            "system_name": "gateway-service", "cpu_percent": 30, "memory_percent": 45,
            "alerts": ["upstream timeout to payments-service (504)", "network packet loss 3%"]}},
            {"source": "runbook", "incident_type": "network-timeout",
             "data": Path('data/runbooks/network-timeout.md').read_text()}]
        draft = ('1) gateway-service에서 upstream timeout to payments-service (504)를 확인했습니다.\n'
                 '2) DNS 해석 실패, 방화벽 변경과 로드밸런서 상태를 추가 점검합니다.\n'
                 '3) 타임아웃 값이 너무 짧아 정상 응답이 실패한 것이 확인되면 임계값 임시 상향을 검토합니다.')
        self.assertEqual(grounding_issues(draft, collected), [])

class MetricBindingTests(unittest.TestCase):
    def test_swapped_metric_values_are_rejected(self):
        draft = VALID.replace('CPU는 47.3%, 메모리는 91.7%', 'CPU는 91.7%, 메모리는 47.3%')
        self.assertTrue(any('지표' in issue for issue in grounding_issues(draft,COLLECTED)))

    def test_threshold_is_not_treated_as_current_measurement(self):
        self.assertEqual(grounding_issues(VALID + '\nCPU 사용률 90% 이상일 때 점검합니다.',COLLECTED),[])

    def test_unconditional_timeout_increase_is_rejected(self):
        draft = '1) gateway-service 타임아웃을 확인했습니다.\n2) DNS를 점검합니다.\n3) 타임아웃 임계값을 상향합니다.'
        self.assertTrue(any('타임아웃' in issue for issue in grounding_issues(draft,[])))
