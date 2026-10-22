"""게임 상태는 읽기만 하고 일회성 관측 권한만 소모하는 게임 전용 MCP 서버."""
import hashlib
import json
import os
import sqlite3
import time
from fastmcp import FastMCP
from cargo_game.domain import RULES

mcp = FastMCP('last-cargo-observations')


def consume(observation_id, kind):
    conn = sqlite3.connect(os.environ['CARGO_DB_PATH'], isolation_level=None)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT kind,payload,expires FROM observations WHERE token_hash=?',
                           (hashlib.sha256(observation_id.encode()).hexdigest(),)).fetchone()
        if not row or row[0] != kind or row[2] < time.time():
            raise ValueError('Observation unavailable')
        conn.execute('DELETE FROM observations WHERE token_hash=?',
                     (hashlib.sha256(observation_id.encode()).hexdigest(),))
        conn.commit()
        return json.loads(row[1])
    finally:
        conn.close()


@mcp.resource('cargo://rules')
def rules() -> str:
    return json.dumps(RULES, ensure_ascii=False)


@mcp.resource('cargo://cards-rules')
def card_rules() -> str:
    return json.dumps({**RULES, 'ruleset': 'cards-v2', 'max_model_calls': 30,
                       'quantity': [1, 4], 'card_contract': '카드 적용 가격 범위와 준비한 수량을 지킵니다.'}, ensure_ascii=False)


@mcp.tool()
def own_inventory(observation_id: str) -> dict:
    """서버가 발급한 권한에 해당하는 상인 자신의 상태만 한 번 반환합니다."""
    return consume(observation_id, 'inventory')


@mcp.tool()
def own_memory(observation_id: str) -> dict:
    """해당 상인이 참여한 거래와 최근 대화만 한 번 반환합니다."""
    return consume(observation_id, 'memory')


if __name__ == '__main__':
    mcp.run(show_banner=False)
