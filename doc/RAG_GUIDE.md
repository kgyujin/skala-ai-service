# 런북 RAG 실행·개발 안내

기존 장애 대응 Agent에 의미 기반 런북 검색과 조사 대상 확인을 적용했습니다. 사용자 증상과 수집한 모니터링 알림으로 런북을 찾고, 기존 MCP를 통해 본문을 읽어 대응안을 작성합니다. 미등록 또는 미지정 대상은 조회 전에 확인합니다.

## 1. 실행

프로젝트 루트에서 실행합니다. 기존 `.env`는 수정하지 않아도 됩니다. 기본 선택 방식은 `rag`이고, 실제 키나 토큰을 이 문서에 넣지 않습니다.

```bash
.venv/bin/python -m pip install -r requirements.txt

# 이번 검증에서 사용한 임시 캐시입니다. /tmp 정리 시 모델을 다시 받습니다.
export HF_HOME="/tmp/runbook-rag-model-cache"

# 최초 모델 다운로드·로딩을 확인합니다. 오류가 있으면 대체 사유가 출력됩니다.
.venv/bin/python -c 'from agent.runbook_search import RunbookSelector; s=RunbookSelector(); print(s.initialization_error, s.initialization_seconds)'

# 로컬 Ollama가 실행 중이고 qwen3:0.6b, qwen3:8b가 설치된 상태
LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false \
LLM_BACKEND=ollama SUPERVISOR_VERSION=v2 RUNBOOK_SEARCH_MODE=rag \
.venv/bin/python server.py
```

다른 터미널에서 프런트엔드를 띄웁니다.

```bash
.venv/bin/python web_demo_server.py
```

브라우저에서 `http://localhost:8600`에 접속합니다. 백엔드는 `8100`입니다. 기존 실행 스크립트에는 기존 서버 종료·세션 DB 삭제가 포함되어 있으므로, 위 직접 실행 명령은 기존 세션을 보존하고 싶을 때 사용합니다. 최초 모델 다운로드가 끝나기 전에는 서버가 준비되지 않을 수 있습니다.

이번 실제 LLM 연결 확인에는 이미 설치된 `qwen2.5:0.5b`를 두 역할에 사용했습니다. 같은 구성을 재현하려면 실행 명령에 `OLLAMA_FAST_MODEL=qwen2.5:0.5b OLLAMA_QUALITY_MODEL=qwen2.5:0.5b`를 추가합니다. 기본 qwen3 조합은 장시간 요청을 중단해 완료 여부가 미검증입니다. 소형 모델 답변에서도 근거 없는 수치가 발견됐으므로 답변의 사실 확인은 필요합니다.

테스트용 fake LLM은 `LLM_BACKEND=fake`로 실행합니다. 임베딩 검색은 실제 모델을 사용하지만 답변은 고정 규칙으로 생성됩니다. OpenAI API를 사용할 필요는 없습니다.

### 예시 입력

- `payments-service CPU 급증과 cache-service 메모리 누수 장애를 함께 진단해주세요`
- `gateway-service 네트워크 타임아웃 장애를 진단해주세요`
- `log-service 저장 공간이 부족한 장애를 확인해주세요`

답변 하단의 `참고 런북`은 MCP에서 실제로 읽어 생성기에 전달한 문서 목록입니다. 모든 문서의 모든 조치가 정답이라는 의미는 아닙니다. 검색 실패 시 기존 키워드 방식으로 대체했다는 안내가 붙습니다.

### 기존 방식으로 비교하거나 되돌리기

서버를 종료하고 같은 실행 명령에서 `RUNBOOK_SEARCH_MODE=keyword`로 바꿉니다. 서버 시작 시 선택기가 만들어지므로 설정 변경 후 재시작이 필요합니다. `SUPERVISOR_VERSION=v1`은 검색기를 사용하지 않는 기존 별도 파이프라인이므로, 전후 비교에는 v2를 사용합니다.

## 2. 코드 이해

```text
server.py: 시작 시 선택기 생성, 의존성 주입
    ↓
agent/system_targets.py: 대상 검증과 확인 응답 복원
    ↓
agent/graph.py: 대상 확인 → 모니터링 조사 → replan_check에서 런북 선택
    ↓
agent/runbook_search.py: query와 알림 → E5 → 전체 질의 1순위 선택 → 복합 증상의 독립 지지가 있으면 2순위 추가
    ↓
기존 research: MCP Resource 본문 조회
    ↓
write/validate: 대응안 생성·제한적 근거 검사
    ↓
agent/answer_grounding.py: 반려 사유 생성, 반복 실패 시 사실 요약
    ↓
finalize: 참고 런북·검색 대체 여부 표시
```

- `passage:`는 문서 임베딩, `query:`는 검색 입력의 접두사입니다. 한국어에도 적용합니다.
- 문서 벡터는 시작 시 한 번, 질의 벡터는 검색마다 계산합니다. CPU에서 실행합니다.
- 점수는 벡터 간 유사도이며 장애 확률이나 정답 확률이 아닙니다.
- 기존 키워드 규칙의 우선순위와 기본 `db-down` 선택을 비교 기준으로 보존했습니다.
- 검색 상세는 `runbook_search` LangSmith 추적과 그래프 내부 `runbook_selection`에 남깁니다. LangSmith 업로드 검증은 이번 로컬 테스트에서 수행하지 않았습니다.
- 채팅 요청·응답의 최상위 필드와 SSE 이벤트 종류는 유지합니다. 기존 재계획 이벤트의 `target`은 첫 후보이며, 각 후보 조회는 기존 `TaskCompleted` 이벤트로 확인합니다.

## 3. 테스트·평가 재현

```bash
LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false \
.venv/bin/python -m unittest discover -s tests -v

.venv/bin/python -m compileall -q agent eval tests server.py
.venv/bin/python -m pip check

# 기본은 evaluation 24개, 각 입력을 3회 측정. 네트워크 추적은 평가기에서 끕니다.
.venv/bin/python -m eval.run_eval --pipeline --output /tmp/runbook-evaluation.json

# 개발용 12개만 확인
.venv/bin/python -m eval.run_eval --split development --output /tmp/runbook-development.json

# 모델 설치 없이 기존 검색 기준만 실행
.venv/bin/python -m eval.run_eval --mode keyword --output /tmp/runbook-keyword.json
```

다운로드가 완료된 캐시를 사용한다면 `HF_HUB_OFFLINE=1`을 추가할 수 있습니다. 모델이 없는 오프라인 환경에서 RAG 평가는 실패로 종료하며, 키워드 대체를 의미 검색 성공으로 집계하지 않습니다.

`--pipeline`은 **실제 LangGraph + fake LLM + 고정 MCP 어댑터**입니다. 라우팅·계획도 고정해 검색 방식만 바꾸고, 난수·의도적 첫 호출 실패는 제거합니다. 실제 MCP 통신은 별도 통합 테스트가 검증합니다. 이 실행시간을 실제 Ollama/OpenAI 응답시간으로 해석하지 않습니다. 초기 로딩 시간은 반복 검색 시간에서 분리합니다.

평가 데이터는 직접 작성한 합성 사례입니다. 개발용 12개와 평가용 24개를 나누었지만 외부 독립 벤치마크는 아닙니다. 정답 런북과 이유, 정보 부족 사례가 포함됩니다. 지표는 Top-1 정확도, 문서 재현율·정밀도, 불필요 후보 수, 복합 증상 회수, 검색·파이프라인 시간입니다. 원본 결과에 데이터·문서 SHA-256과 패키지 버전이 저장됩니다.

## 4. 런북 추가

1. `data/runbooks/queue-backlog.md`처럼 영문 소문자·숫자·하이픈 파일명으로 새 문서를 작성합니다. 제목은 `# `로 시작합니다.
2. 증상과 확인 절차, 조치 조건을 본문에 적습니다. 검색만을 위한 정답 키워드 나열은 피합니다.
3. 서버를 재시작합니다. 검색기가 파일을 자동 발견하며 MCP도 같은 파일 식별자로 본문을 조회합니다.
4. 새 문서에 해당하는 평가 질의와 예상 정답을 추가해 품질 변화를 확인합니다.

분류 조건문 추가는 필요하지 않습니다. 문서 수정도 재시작 후 반영됩니다. 장문 문서의 분할, 다량 문서의 벡터 DB 저장은 향후 검색 모듈 내부 확장 대상으로 남겼습니다.

## 5. 현재 한계

기본은 전체 요청의 1순위 문서 하나를 선택합니다. 요청에 와, 과, 고, 및, 그리고, 쉼표 같은 나열 표현이 있으면 각 부분을 별도로 검색합니다. 전체 요청의 1순위와 2순위가 각각 분리된 증상의 1위로 선택될 때만 두 번째 문서를 추가합니다. 장애 유형별 조건문은 추가하지 않았습니다. 나열 표현이 없는 복합 증상은 놓칠 수 있고, 정보가 부족해도 첫 후보는 반환됩니다. 이를 자동 실행에 사용하면 안 됩니다. 검색 임계값이나 증상 정보 부족에 대한 추가 질문은 별도 데이터로 검증할 수 있습니다. 답변 검수는 형식, 수집 근거에 없는 백분율, 롤백 조건, CPU 추세의 불확실성 안내를 검사합니다. 반려 사유를 전달해 최대 3회 작성하며, 계속 실패하면 강제 합격시키지 않고 도구 관측값만 요약합니다. 채팅과 SSE에 검수 실패 사실을 표시합니다. 검사 범위는 제한적입니다. CPU와 메모리 이름 바로 뒤에 나오는 수치는 해당 지표와 대조합니다. 다만 생략된 시스템명이나 모든 자연어 주장의 사실성까지 보장하지 않습니다. 타임아웃 상향 권고에도 정상 응답 실패와 짧은 설정에 대한 확인 조건을 요구합니다. CPU와 롤백 안내에는 정해진 문구를 요구하므로 의미가 같은 다른 표현도 반려될 수 있습니다. 미등록 또는 미지정 시스템은 조회 전에 대상을 확인합니다. 확인 질문에 답한 시스템으로 원래 장애 요청을 이어갑니다.

모니터링 수치는 샘플 JSON에 난수를 더한 값입니다. 실제 운영 장애 진단 성능을 입증하지 않습니다. `cost_usd` 역시 실제 청구 금액이 아닙니다. 실제 답변 품질은 별도로 사람이 점검해야 합니다.

## 참고

- [프로젝트 설명](../README.md)
- [E5 공식 모델 카드](https://huggingface.co/intfloat/multilingual-e5-small)


## 제출 전 재현 확인

이전 제출 환경은 Python 3.14에서 확인했습니다. 이번 수정은 기존 Python 3.11 환경에서 Python 테스트 62개와 Node.js 화면 이벤트 테스트 2개를 통과했습니다. 미등록 대상 차단, 복수 대상 보존, 확인 대화 재개, 취소와 세션 분리도 포함합니다.

```sh
python -m unittest discover -s tests -q
python -m pip check
node --test tests/test_web_stream.cjs
```

실제 로컬 모델 연결은 별도 확인 스크립트로 재현합니다. Ollama에 기본 검증 모델 qwen2.5:0.5b가 설치되어 있어야 합니다. 모델을 바꾸려면 OLLAMA_FAST_MODEL과 OLLAMA_QUALITY_MODEL을 지정합니다. 기존 세션 DB 대신 임시 DB를 사용하며 결과는 eval/results/final_ollama_smoke.json에 기록합니다.

```sh
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false python -m eval.smoke_ollama
```

소스 압축은 다음 명령을 사용합니다. result/task-1/G105_runbook_source.zip에 실행 코드, 런북, 테스트, 평가 자료와 이 안내를 담습니다. 인증 파일, 세션 DB, 모델, 캐시와 기존 보고서 초안은 포함하지 않습니다. 최종 보고서 PDF는 result/task-1/에서 별도로 제출합니다.

```sh
bash sh/make_student_zip.sh
```

Ollama 요청은 호출당 최대 1,024개 토큰을 생성하도록 제한했습니다. 소형 모델의 반복 출력이 길어지는 경우를 줄이기 위한 설정이며, 잘린 답변이 사실성을 보장하지는 않습니다.

최종 실제 모델 확인에서는 네트워크 HTTP/SSE 요청 모두 네트워크 런북만 조회했습니다. CPU와 메모리 복합 요청 두 건에서는 두 런북을 조회했습니다. qwen2.5:0.5b의 복합 답변 한 건은 검수 세 차례 실패 후 사실 요약으로 종료됐고, 다른 한 건도 수치 나열에 그쳤습니다. 검색 및 연결 확인과 생성 답변 품질을 구분해야 합니다.


## 조사 대상 확인과 대화 재개

지원 시스템은 data/monitoring.json의 키를 사용합니다. 미등록 이름이나 시스템명이 없는 장애 요청은 MCP 프롬프트와 모니터링 호출 전에 조회 대상을 확인합니다. 일부만 지원되는 복수 시스템 요청도 확인 전에는 조회하지 않습니다. auth-service나 web-01을 기본 대상으로 추가하지 않습니다.

예를 들어 billing-service 메모리 누수 장애를 요청하면 지원 시스템을 안내합니다. 같은 thread_id에서 cache-service라고 답하면 원래 메모리 누수 증상을 유지하면서 cache-service만 조사합니다. 대기 요청은 SQLite의 pending_requests에 보관하므로 서버 재시작 후에도 이어집니다. 취소라고 답하면 대기 요청을 해제합니다. 새 장애 요청은 이전 요청과 섞지 않습니다.

계획 모델이 요청한 시스템을 누락하거나 다른 대상을 출력해도 확인된 대상마다 모니터링 작업 하나만 생성합니다. fake, Ollama와 OpenAI가 같은 정규화 함수를 사용하며 v1도 조회 전에 동일한 대상 확인을 수행합니다.

고정 평가의 --pipeline 옵션은 고정 MCP 자료의 test-service를 테스트 카탈로그와 요청에 명시합니다. 검색 단독 평가에는 원래 질의를 사용하며, 전체 그래프 평가에는 query 필드에 기록된 대상 명시 질의를 사용합니다. 실제 서버의 지원 목록을 변경하지 않습니다.

## 발표 전 수정 확인

런북 식별자와 URL을 조사 대상 이름으로 잘못 판단하던 문제를 수정했습니다. `cache-service를 조사해주세요` 같은 확인 답변도 원래 장애 증상을 이어받습니다. 장애 답변은 검수가 끝난 최종 응답만 화면에 표시하며, 일반 대화의 스트리밍은 유지합니다.
