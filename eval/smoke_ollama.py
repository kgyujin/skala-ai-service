"""실제 로컬 E5, MCP, Ollama의 HTTP/SSE 제출 전 확인입니다.

프로젝트 루트에서 python -m eval.smoke_ollama 로 실행합니다.
기존 세션은 사용하지 않으며 결과는 eval/results/final_ollama_smoke.json에 기록합니다.
기본 검증 모델은 설치된 qwen2.5:0.5b이며 OLLAMA_*_MODEL로 변경할 수 있습니다.
"""
import os
import json,tempfile
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from agent.runbook_search import RunbookSelector
from agent.mcp_client import MCPClient
from agent.session_store import SqliteSessionStore
import server
selector=RunbookSelector();assert selector.initialization_error is None,selector.initialization_error
checks=[]
with tempfile.TemporaryDirectory() as folder:
 with patch.object(server,'RunbookSelector',return_value=selector), \
      patch.object(server,'MCPClient',side_effect=lambda:MCPClient(flaky_first_tool_call=False)), \
      patch.object(server,'SqliteSessionStore',side_effect=lambda:SqliteSessionStore(Path(folder)/'sessions.db')), \
      patch.dict('os.environ',{'LLM_BACKEND':'ollama','OLLAMA_FAST_MODEL':os.getenv('OLLAMA_FAST_MODEL','qwen2.5:0.5b'),'OLLAMA_QUALITY_MODEL':os.getenv('OLLAMA_QUALITY_MODEL','qwen2.5:0.5b'),'SUPERVISOR_VERSION':'v2'}):
  with TestClient(server.app) as client:
   for i,query in enumerate(['gateway-service 네트워크 타임아웃 장애를 진단해주세요',
                            'CPU 급증과 cache-service 메모리 누수 장애를 함께 진단해주세요',
                            'payments-service CPU 급증과 cache-service 메모리 누수 장애를 함께 진단해주세요']):
    response=client.post('/chat',json={'message':query,'thread_id':f'candidate-{i}'})
    assert response.status_code==200,response.text
    result=response.json();chosen=[e['target'] for e in result['events'] if e['type']=='TaskCompleted' and e.get('source')=='runbook']
    if i==0:assert chosen==['network-timeout'],chosen
    else:assert set(chosen)=={'cpu-spike','memory-leak'},chosen
    print(json.dumps({'query':query,'reply':result['reply']},ensure_ascii=False),flush=True)
    checks.append({'query':query,'selected':chosen,'reply':result['reply'],'events':result['events']})
   with client.stream('GET','/chat/stream',params={'message':checks[0]['query'],'thread_id':'candidate-sse'}) as stream:
    frames=[line[6:] for line in stream.iter_lines() if line.startswith('data: ')]
   assert frames[-1]=='[DONE]'
   events=[json.loads(frame) for frame in frames[:-1]]
   chosen=[e['target'] for e in events if e.get('type')=='TaskCompleted' and e.get('source')=='runbook']
   assert chosen==['network-timeout'],chosen
   checks.append({'query':checks[0]['query'],'channel':'sse','selected':chosen,
                  'reply':next(e['reply'] for e in events if e.get('type')=='FinalAnswer'),'done':True})
out={'components':{'search':'actual multilingual-e5-small CPU','mcp':'actual local MCP subprocess',
                   'generation':'actual Ollama', 'fast_model':os.getenv('OLLAMA_FAST_MODEL','qwen2.5:0.5b'), 'quality_model':os.getenv('OLLAMA_QUALITY_MODEL','qwen2.5:0.5b'),'monitoring':'sample tool'},'checks':checks}
(Path(__file__).resolve().parent / 'results' / 'final_ollama_smoke.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
print(json.dumps([{'channel':c.get('channel','http'),'selected':c['selected']} for c in checks],ensure_ascii=False))
