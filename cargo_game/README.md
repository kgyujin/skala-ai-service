# 라스트 카고

AI 상인과 물자를 거래하는 카드 전략 게임입니다. 카드를 조합해 조건을 준비하고 상인의 제안을 확인한 뒤 거래합니다.

## 실행

기존 `.venv`를 사용하며 Ollama에 `qwen3.5:2b`가 설치되어 있어야 합니다. 모델을 자동으로 다운로드하거나 유료 API로 전환하지 않습니다.

```sh
bash sh/run_cargo.sh
```

주소는 `http://127.0.0.1:8200`입니다. 카드 버전 DB는 `cargo_game/.runtime/cargo-cards.db`이며 기존 거래 버전 DB와 분리합니다. 로컬 단일 프로세스로 실행합니다.

환경변수는 `CARGO_MODEL`, `CARGO_CARD_DB_PATH`, `CARGO_OLLAMA_URL`을 사용합니다. LangSmith 추적은 기존 환경변수와 `LANGSMITH_PROJECT`로 설정합니다.

## 플레이

1. 거래 상대, 구매 또는 판매, 물자와 수량을 정하고 거래를 준비합니다.
2. 손패를 선택해 정보를 얻거나 가격 범위를 조정합니다.
3. 협상을 제출하면 AI 상인이 조건을 판단합니다.
4. 제안 카드에서 물품과 금액을 확인하고 수락합니다.
5. 라운드 종료 후 카드 보상을 선택하며 총 5라운드를 진행합니다.

매 라운드 손패 5장, 협상력 3, 협상 기본 2회와 체결 2회입니다. 기본 거래 수량은 최대 2개이며 대량 계약으로 4개까지 요청합니다. 가격 효과는 개당 최대 2이고 상인의 손실 방지 조건을 넘지 못합니다. 역제안은 준비한 물자와 수량을 유지합니다.

카드 소모와 거래는 서버에서 확정합니다. 대사만으로 거래가 체결되지 않습니다. 모델 실패 시 새 제안을 만들지 않으며 사용한 카드와 기회는 반환하지 않습니다. 표준 계약은 모델을 호출하지 않는 규칙 기반 제안입니다.

키보드로 카드 선택과 창 닫기가 가능합니다. 소리와 동작 줄이기는 설정에서 바꿀 수 있습니다. 접근 토큰은 현재 브라우저 세션에 저장되므로 브라우저 세션을 닫으면 이전 게임 접근 권한을 잃을 수 있습니다.

## 검증

```sh
.venv/bin/python -m compileall -q agent cargo_game eval tests server.py mcp_server.py mcp_server_docs.py web_demo_server.py
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false .venv/bin/python -m unittest discover -s tests -q
node --check cargo_game/static/app.js
node --check cargo_game/static/effects.js
node --check cargo_game/static/sound.js
```

실제 모델 검증은 실행 중인 Ollama를 사용합니다.

```sh
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false .venv/bin/python -m cargo_game.cards_playthrough
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false .venv/bin/python -m cargo_game.cards_evaluate
```

실측은 공개 결과만 저장하며 접근 토큰을 출력하지 않습니다. 평가의 단일 호출과 Agent는 같은 모델, 게임 상태와 카드 조건을 사용합니다. Agent에 MCP 기억 조회와 최대 1회 수정이 추가됩니다.

설계, 검증 결과와 화면 자료는 [과제 2번 자료](../result/task-2/README.md)에 있습니다. 이 폴더는 로컬 산출물이며 Git에서 제외됩니다. 이전 거래 버전 평가는 `cargo_game.evaluate`, 기존 테스트 앱은 `create_app(ruleset='legacy')`로 유지합니다.
