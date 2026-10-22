#!/usr/bin/env bash
# 과제 2번만 실행합니다. 모델 다운로드와 기존 서버 설정 변경은 하지 않습니다.
set -euo pipefail
cd "$(dirname "$0")/.."
export LANGSMITH_PROJECT="${LANGSMITH_PROJECT:-last-cargo}"
exec .venv/bin/python -m cargo_game.server
