"""미등록 대상의 대체 조회, 복수 대상 누락 및 확인 대화 회귀 검증."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph
from agent.session_store import SqliteSessionStore
from test_graph_rag import FixedMCP, TwoRunbooks

class RecordingMCP(FixedMCP):
    def __init__(self): self.requested=[]; self.prompt_queries=[]
    async def call_tool(self,name,arguments):
        self.requested.append(arguments['system_name'])
        return await super().call_tool(name,arguments)
    async def get_prompt(self,name,arguments,**kwargs):
        self.prompt_queries.append(arguments['symptom'])
        return '표준 절차'

class WrongPlanner(FastLLM):
    def plan(self,query,procedure):
        return {'needs_analysis':False,'tasks':[
            {'id':'bad','agent':'research','kind':'monitoring','target':'auth-service','status':'pending'}]}, {'usd':0}

class TargetHelpers:
    def setup_graph(self,fast=None):
        store=SqliteSessionStore(Path(':memory:'));self.addCleanup(store.close)
        mcp=RecordingMCP()
        graph=build_graph(SupervisorDeps(fast or FastLLM('fake',0),QualityLLM('fake',0),mcp,store,'v2',TwoRunbooks()))
        return graph,mcp
    async def ask(self,graph,query,thread='target'):
        return await graph.ainvoke({'query':query,'thread_id':thread,'history':[],'events':[],'cost_usd':0})

class TargetGuardTests(TargetHelpers, unittest.IsolatedAsyncioTestCase):
    async def test_unknown_does_not_call_monitoring_or_prompt(self):
        graph,mcp=self.setup_graph();r=await self.ask(graph,'billing-service CPU 급증 장애를 진단해주세요')
        self.assertEqual(mcp.requested,[]);self.assertEqual(mcp.prompt_queries,[])
        self.assertIn('billing-service',r['final']);self.assertIn('조회할 시스템',r['final'])
    async def test_unknown_is_blocked_even_when_llm_is_unavailable(self):
        graph,mcp=self.setup_graph()
        with patch.object(FastLLM,'route',side_effect=RuntimeError('LLM unavailable')):
            r=await self.ask(graph,'billing-service 장애 진단')
        self.assertEqual(mcp.requested,[]);self.assertIn('조회할 시스템',r['final'])
    async def test_missing_system_asks_without_monitoring(self):
        graph,mcp=self.setup_graph();r=await self.ask(graph,'CPU 급증 장애를 진단해주세요')
        self.assertEqual(mcp.requested,[]);self.assertIn('조회할 시스템',r['final'])
    async def test_mixed_known_unknown_blocks_entire_plan(self):
        graph,mcp=self.setup_graph();r=await self.ask(graph,'payments-service CPU와 billing-service 메모리 누수 장애 진단')
        self.assertEqual(mcp.requested,[]);self.assertIn('billing-service',r['final'])
    async def test_supported_name_prefix_is_not_a_match(self):
        graph,mcp=self.setup_graph();await self.ask(graph,'auth-service-v2 장애 진단')
        self.assertEqual(mcp.requested,[])
    async def test_clarification_resumes_original_symptom(self):
        graph,mcp=self.setup_graph()
        await self.ask(graph,'billing-service 메모리 누수 장애 진단')
        await self.ask(graph,'그럼 cache-service로 확인해주세요')
        self.assertEqual(mcp.requested,['cache-service'])
        self.assertIn('메모리 누수',mcp.prompt_queries[-1]);self.assertNotIn('billing-service',mcp.prompt_queries[-1])
    async def test_missing_target_reply_and_thread_isolation(self):
        graph,mcp=self.setup_graph()
        await self.ask(graph,'CPU 급증 장애 진단','a')
        await self.ask(graph,'안녕하세요','b')
        self.assertEqual(mcp.requested,[])
        await self.ask(graph,'payments-service','a')
        self.assertEqual(mcp.requested,['payments-service'])
        self.assertIn('CPU 급증',mcp.prompt_queries[-1])
    async def test_planner_cannot_add_or_drop_targets(self):
        graph,mcp=self.setup_graph(WrongPlanner('wrong',0))
        await self.ask(graph,'payments-service CPU와 cache-service 메모리 누수 장애 진단')
        self.assertCountEqual(mcp.requested,['payments-service','cache-service'])
    async def test_cancel_clears_pending_request(self):
        graph,mcp=self.setup_graph()
        await self.ask(graph,'billing-service 장애 진단');r=await self.ask(graph,'취소')
        self.assertIn('취소',r['final']);await self.ask(graph,'안녕하세요')
        self.assertEqual(mcp.requested,[])

class BackendTargetTests(unittest.TestCase):
    def test_ollama_and_openai_do_not_replace_unknown(self):
        from agent.ollama_llm import OllamaFastLLM
        from agent.openai_llm import OpenAIFastLLM
        for cls in (OllamaFastLLM,OpenAIFastLLM):
            model=cls('unused',0)
            with patch.object(model,'_chat',return_value=(json.dumps({'tasks':[{'agent':'research','target':'billing-service'}]}),{'usd':0})):
                plan,_=model.plan('billing-service 장애 진단','표준 절차')
            self.assertEqual([t for t in plan['tasks'] if t['agent']=='research'],[],cls.__name__)

class TargetHttpTests(unittest.TestCase):
    def test_restart_and_http_sse_followup(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'session.db';connections=[]
            def make_mcp():
                mcp=RecordingMCP();connections.append(mcp);return mcp
            with patch.object(server,'MCPClient',side_effect=make_mcp),patch.object(server,'RunbookSelector',return_value=TwoRunbooks()),patch.object(server,'SqliteSessionStore',side_effect=lambda:SqliteSessionStore(path)),patch.dict('os.environ',{'LLM_BACKEND':'fake','SUPERVISOR_VERSION':'v2'}):
                with TestClient(server.app) as client:
                    r=client.post('/chat',json={'message':'billing-service 메모리 누수 장애 진단','thread_id':'resume'})
                    self.assertEqual(set(r.json()),{'reply','events','metadata'})
                    self.assertEqual(connections[-1].requested,[])
                with TestClient(server.app) as client:
                    with client.stream('GET','/chat/stream',params={'message':'cache-service','thread_id':'resume'}) as stream:
                        frames=[line[6:] for line in stream.iter_lines() if line.startswith('data: ')]
                    self.assertEqual(frames[-1],'[DONE]')
                    self.assertEqual(connections[-1].requested,['cache-service'])
                    self.assertIn('메모리 누수',connections[-1].prompt_queries[-1])
                    events=[json.loads(f) for f in frames[:-1]]
                    self.assertTrue(any(e.get('type')=='FinalAnswer' for e in events))

class AdditionalTargetTests(TargetHelpers, unittest.IsolatedAsyncioTestCase):
    async def test_invalid_followup_still_blocks_then_corrects(self):
        graph,mcp=self.setup_graph()
        await self.ask(graph,'billing-service 메모리 누수 장애 진단')
        await self.ask(graph,'cache-servce')
        self.assertEqual(mcp.requested,[])
        await self.ask(graph,'cache-service')
        self.assertEqual(mcp.requested,['cache-service'])
        self.assertIn('메모리 누수',mcp.prompt_queries[-1])
    async def test_mixed_target_replacement_preserves_other_system(self):
        graph,mcp=self.setup_graph()
        await self.ask(graph,'payments-service CPU와 billing-service 메모리 누수 장애 진단')
        await self.ask(graph,'cache-service')
        self.assertCountEqual(mcp.requested,['payments-service','cache-service'])
    async def test_new_incident_replaces_pending_context(self):
        graph,mcp=self.setup_graph()
        await self.ask(graph,'billing-service 메모리 누수 장애 진단')
        await self.ask(graph,'gateway-service 네트워크 타임아웃 장애 진단')
        self.assertEqual(mcp.requested,['gateway-service'])
        self.assertNotIn('메모리 누수',mcp.prompt_queries[-1])
    async def test_catalog_failure_does_not_use_fallback_system(self):
        graph,mcp=self.setup_graph()
        with patch('agent.system_targets.CATALOG_PATH',Path('/nonexistent/monitoring.json')):
            r=await self.ask(graph,'auth-service 장애 진단')
        self.assertEqual(mcp.requested,[]);self.assertIn('목록을 읽을 수 없어',r['final'])
    async def test_uppercase_supported_target_keeps_canonical_name(self):
        graph,mcp=self.setup_graph();await self.ask(graph,'CACHE-SERVICE 메모리 누수 장애 진단')
        self.assertEqual(mcp.requested,['cache-service'])
    async def test_multiple_unknowns_are_replaced_in_request_order(self):
        graph,mcp=self.setup_graph()
        await self.ask(graph,'billing-service CPU와 catalog-service 메모리 누수 장애 진단')
        self.assertEqual(mcp.requested,[])
        await self.ask(graph,'payments-service 및 cache-service')
        self.assertEqual(mcp.requested,['payments-service','cache-service'])
        self.assertIn('payments-service CPU',mcp.prompt_queries[-1])
        self.assertIn('cache-service 메모리',mcp.prompt_queries[-1])
    async def test_status_request_without_incident_keyword_is_guarded(self):
        graph,mcp=self.setup_graph()
        r=await self.ask(graph,'billing-service 상태 확인')
        self.assertEqual(mcp.requested,[]);self.assertIn('조회할 시스템',r['final'])
    async def test_legacy_unknown_and_followup_are_guarded(self):
        from agent.supervisor_v1 import run_v1
        store=SqliteSessionStore(Path(':memory:'));self.addCleanup(store.close)
        mcp=RecordingMCP();deps=SupervisorDeps(FastLLM('fake',0),QualityLLM('fake',0),mcp,store,'v1')
        r=await run_v1(deps,'billing-service 장애 진단','legacy',[])
        self.assertEqual(mcp.requested,[]);self.assertIn('조회할 시스템',r['final'])
        await run_v1(deps,'cache-service','legacy',[])
        self.assertEqual(mcp.requested,['cache-service'])

class SseClarificationTests(unittest.TestCase):
    def test_unknown_sse_does_not_call_tools_in_either_version(self):
        import server
        for version in ('v1','v2'):
            with tempfile.TemporaryDirectory() as folder:
                mcp=RecordingMCP();path=Path(folder)/'session.db'
                with patch.object(server,'MCPClient',return_value=mcp),patch.object(server,'RunbookSelector',return_value=TwoRunbooks()),patch.object(server,'SqliteSessionStore',side_effect=lambda:SqliteSessionStore(path)),patch.dict('os.environ',{'LLM_BACKEND':'fake','SUPERVISOR_VERSION':version}):
                    with TestClient(server.app) as client:
                        with client.stream('GET','/chat/stream',params={'message':'billing-service 장애 진단','thread_id':'sse'}) as stream:
                            frames=[line[6:] for line in stream.iter_lines() if line.startswith('data: ')]
                        self.assertEqual(frames[-1],'[DONE]');self.assertEqual(mcp.requested,[])
                        events=[json.loads(f) for f in frames[:-1]]
                        self.assertFalse(any(e.get('type')=='TaskCompleted' for e in events))
                        self.assertIn('조회할 시스템',next(e['reply'] for e in events if e.get('type')=='FinalAnswer'))
                        r=client.post('/chat',json={'message':'cache-service','thread_id':'sse'})
                        self.assertEqual(r.status_code,200);self.assertEqual(mcp.requested,['cache-service'])

class NaturalTargetTests(TargetHelpers, unittest.IsolatedAsyncioTestCase):
    async def test_incident_identifiers_and_domains_are_not_targets(self):
        for query in ('gateway-service network-timeout 장애를 진단해주세요',
                      'gateway-service에서 https://api.example.com/v1 호출 타임아웃 진단'):
            with self.subTest(query=query):
                graph,mcp=self.setup_graph()
                await self.ask(graph,query)
                self.assertEqual(mcp.requested,['gateway-service'])

    async def test_natural_followup_restores_original_symptom(self):
        for reply in ('cache-service를 조사해주세요', 'cache-service로 진단해 주세요',
                      'cache-service를 확인해 주세요'):
            with self.subTest(reply=reply):
                graph,mcp=self.setup_graph()
                await self.ask(graph,'billing-service 메모리 누수 장애 진단')
                await self.ask(graph,reply)
                self.assertEqual(mcp.requested,['cache-service'])
                self.assertIn('메모리 누수',mcp.prompt_queries[-1])
