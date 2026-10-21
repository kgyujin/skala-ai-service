"""v2 그래프와 HTTP/SSE 계약 검증. LLM/검색 벡터만 대체한다."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph, research_node
from agent.runbook_search import RunbookSelector
from agent.session_store import SqliteSessionStore


class FixedMCP:
    async def start(self): pass
    async def stop(self): pass
    async def find_prompt(self, name): return "ops"
    async def get_prompt(self, *args, **kwargs): return "표준 절차"
    async def call_tool(self, name, arguments):
        return {"system_name": arguments["system_name"], "status": "degraded",
                "cpu_percent": 91, "memory_percent": 89, "alerts": ["CPU high", "heap near limit"]}
    async def find_resource_template(self, name):
        return "ops", "runbook://incident-response/{incident_type}"
    async def read_resource(self, uri, server):
        incident_type = uri.rsplit("/", 1)[-1]
        return (Path("data/runbooks") / f"{incident_type}.md").read_text()


class TwoRunbooks:
    def select(self, query, collected):
        return {"requested_mode": "rag", "mode": "rag", "fallback_reason": None,
                "search_seconds": 0.001,
                "matches": [{"incident_type": "cpu-spike"}, {"incident_type": "memory-leak"}]}


class GraphTests(unittest.IsolatedAsyncioTestCase):
    def make_deps(self, selector):
        store = SqliteSessionStore(Path(":memory:"))
        self.addCleanup(store.close)
        return SupervisorDeps(FastLLM("fake-fast", 0), QualityLLM("fake-quality", 0),
                              FixedMCP(), store, "v2", selector)

    async def test_two_documents_research_once_and_fake_uses_both(self):
        deps = self.make_deps(TwoRunbooks())
        result = await build_graph(deps).ainvoke({"query": "payments-service CPU와 메모리 급증 진단", "events": [], "history": []})
        runbooks = [c for c in result["collected"] if c["source"] == "runbook"]
        self.assertEqual([c["incident_type"] for c in runbooks], ["cpu-spike", "memory-leak"])
        self.assertEqual(result["replan_rounds"], 1)
        self.assertEqual(result["validation_attempts"], 2)
        self.assertIn("CPU 급증 대응 절차", result["draft"])
        self.assertIn("메모리 누수", result["draft"])
        self.assertIn("참고 런북:", result["final"])
        self.assertEqual(sum(e["type"] == "ReplanCheck" for e in result["events"]), 2)

    async def test_monitoring_tasks_overlap_and_preserve_results(self):
        # 첫 조회가 두 번째 조회를 기다리므로 순차 실행으로 바뀌면 완료되지 않습니다.
        both_started = asyncio.Event()
        started = []
        deps = self.make_deps(None)
        async def monitoring(name, arguments):
            system = arguments["system_name"]
            started.append(system)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            return {"system_name": system, "status": "healthy", "alerts": []}
        deps.mcp.call_tool = monitoring
        tasks = [{"id": f"task-{i}", "agent": "research", "kind": "monitoring",
                  "target": system, "status": "pending"}
                 for i, system in enumerate(["auth-service", "cache-service"])]
        result = await asyncio.wait_for(research_node(
            {"tasks": tasks, "events": [], "collected": []}, deps), timeout=5)
        self.assertCountEqual(started, ["auth-service", "cache-service"])
        self.assertEqual([entry["data"]["system_name"] for entry in result["collected"]],
                         ["auth-service", "cache-service"])
        self.assertTrue(all(task["status"] == "done" for task in result["tasks"]))

    async def test_fallback_notice_in_final_answer(self):
        with tempfile.TemporaryDirectory() as folder:
            selector = RunbookSelector(directory=Path(folder))
        result = await build_graph(self.make_deps(selector)).ainvoke({"query": "payments-service CPU 급증 진단", "events": [], "history": []})
        self.assertIn("기존 키워드 방식", result["final"])
        self.assertEqual(result["runbook_selection"]["mode"], "keyword")

    async def test_keyword_and_old_deps_remain_single_document(self):
        for selector in (None, RunbookSelector("keyword")):
            result = await build_graph(self.make_deps(selector)).ainvoke({"query": "payments-service CPU 급증 진단", "events": [], "history": []})
            self.assertEqual(len([c for c in result["collected"] if c["source"] == "runbook"]), 1)
            self.assertNotIn("기존 키워드 방식", result["final"])

    async def test_general_chat_bypasses_search_and_retains_history(self):
        selector = TwoRunbooks()
        with patch.object(selector, "select", side_effect=AssertionError("should not search")):
            result = await build_graph(self.make_deps(selector)).ainvoke({"query": "제 이름이 뭐였죠?", "events": [],
                "history": [{"role": "user", "content": "제 이름은 민수입니다"}]})
        self.assertIn("민수", result["final"])
        self.assertNotIn("collected", result)


class HttpTests(unittest.TestCase):
    def test_chat_sse_contract_and_sqlite_restart(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "sessions.db"
            with patch.object(server, "MCPClient", FixedMCP), \
                 patch.object(server, "RunbookSelector", return_value=TwoRunbooks()), \
                 patch.object(server, "SqliteSessionStore", side_effect=lambda: SqliteSessionStore(db_path)), \
                 patch.dict("os.environ", {"LLM_BACKEND": "fake", "SUPERVISOR_VERSION": "v2"}):
                with TestClient(server.app) as client:
                    response = client.post("/chat", json={"message": "payments-service CPU 급증 진단", "thread_id": "integration"})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(set(response.json()), {"reply", "events", "metadata"})
                    self.assertIn("참고 런북", response.json()["reply"])
                    with client.stream("GET", "/chat/stream", params={"message": "제 이름은 민수입니다", "thread_id": "memory"}) as response:
                        frames = [line[6:] for line in response.iter_lines() if line.startswith("data: ")]
                    with client.stream("GET", "/chat/stream", params={"message": "payments-service CPU 급증 장애 진단", "thread_id": "incident-sse"}) as response:
                        incident_frames = [line[6:] for line in response.iter_lines() if line.startswith("data: ")]
                    self.assertEqual(incident_frames[-1], "[DONE]")
                    incident_events = [json.loads(frame) for frame in incident_frames[:-1]]
                    self.assertEqual(sum(e.get("type") == "TaskCompleted" and e.get("source") == "runbook" for e in incident_events), 2)
                    self.assertIn("참고 런북:", next(e["reply"] for e in incident_events if e.get("type") == "FinalAnswer"))
                    self.assertEqual(frames[-1], "[DONE]")
                    self.assertTrue(any(json.loads(frame).get("type") == "FinalAnswer" for frame in frames[:-1]))
                with TestClient(server.app) as client:
                    response = client.post("/chat", json={"message": "제 이름이 뭐였죠?", "thread_id": "memory"})
                    self.assertIn("민수", response.json()["reply"])

    def test_real_mcp_resources_and_discovery(self):
        import asyncio
        from agent.mcp_client import MCPClient
        async def check():
            mcp = MCPClient(flaky_first_tool_call=False)
            try:
                await mcp.start()
                server_name, template = await mcp.find_resource_template("get_incident_runbook")
                self.assertEqual(server_name, "ops")
                for incident in ("cpu-spike", "memory-leak"):
                    body = await mcp.read_resource(template.format(incident_type=incident), server_name)
                    self.assertIn("# Runbook:", body)
            finally:
                await mcp.stop()
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
