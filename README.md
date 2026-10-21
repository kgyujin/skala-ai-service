# 운영 장애 대응 Agent 개선

과제 1번으로 기존 Agent의 조사 대상 확인, 런북 검색과 답변 검수를 보완했습니다.

## 주요 개선

- 조사 대상이 미지정이거나 지원 목록에 없으면 확인 질문을 합니다. 같은 대화에서 대상을 답하면 원래 장애 증상으로 분석을 이어갑니다.
- 사용자 증상과 모니터링 알림으로 런북을 검색합니다. 기본은 한 문서이며, 증상별 검색 결과가 뒷받침될 때 최대 두 문서를 사용합니다.
- 백분율, CPU·메모리 수치와 일부 조치 조건을 검사합니다. 최초 작성을 포함해 세 차례 모두 반려되면 관측 사실을 안내합니다.
- 검수 중인 초안은 화면에 표시하지 않고, 최종 답변에 참고 런북을 표시합니다.

## 실행

Python 3.11 환경에서 검증했습니다. Ollama를 별도로 실행하고 사용할 모델을 설치해야 합니다. 임베딩 모델은 최초 실행 시 내려받습니다.

```sh
bash sh/setup_venv.sh

LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
LLM_BACKEND=ollama SUPERVISOR_VERSION=v2 RUNBOOK_SEARCH_MODE=rag \
OLLAMA_FAST_MODEL=qwen2.5:0.5b OLLAMA_QUALITY_MODEL=qwen2.5:0.5b \
.venv/bin/python server.py
```

다른 터미널에서 화면을 실행합니다.

```sh
.venv/bin/python web_demo_server.py
```

브라우저에서 `http://localhost:8600`에 접속합니다. 서버 상태는 `http://localhost:8100/health`에서 확인합니다. 위 직접 실행 명령은 기존 세션 DB를 삭제하지 않습니다.

모델 설정, 검색 방식 전환, 평가 재현과 런북 추가 방법은 [실행·개발 안내](doc/RAG_GUIDE.md)를 참고합니다. `.env.example`은 선택 설정의 예시이며, 실제 키는 Git에서 제외된 `.env`에 보관합니다.

## 코드 구성

| 경로 | 역할 |
|---|---|
| `agent/system_targets.py` | 조사 대상 확인과 후속 질문 연결 |
| `agent/runbook_search.py` | E5 임베딩과 런북 선택 |
| `agent/answer_grounding.py` | 답변 검사와 관측 사실 요약 |
| `agent/graph.py` | 기존 LangGraph에 검색·검수 연결 |
| `agent/mcp_client.py`, `mcp_server.py` | 기존 MCP 연결과 런북 본문 조회 |
| `data/` | 런북 6개와 샘플 모니터링 자료 |
| `tests/` | 기능·회귀 테스트 |
| `eval/` | 고정 평가 질의와 비교 실행 스크립트 |

## 검증

```sh
.venv/bin/python -m unittest discover -s tests -q
node --test tests/test_web_stream.cjs
.venv/bin/python -m compileall -q agent eval tests server.py
.venv/bin/python -m pip check
```

Python 테스트 62개와 Node.js 화면 이벤트 테스트 2개를 확인했습니다. 화면 이벤트 테스트에는 Node.js 18 이상이 필요하며 별도 npm 패키지는 사용하지 않습니다.

평가 결과는 실행 환경에 따라 다시 생성합니다. 출력 JSON은 Git에서 제외합니다.

```sh
.venv/bin/python -m eval.run_eval --pipeline --output eval/results/evaluation.json
```

## 확인 범위

검색은 별도 벡터 DB 없이 문서 벡터를 메모리에 보관합니다. 기존 LangGraph와 MCP 연결을 활용했으며, 모니터링은 샘플 자료입니다. 답변 검수는 정해진 수치와 일부 조치 조건을 검사하므로 모든 자연어 오류의 검출을 보장하지 않습니다. 로컬 모델 응답이 반복 반려되면 대응안 대신 관측 사실을 제공합니다.
