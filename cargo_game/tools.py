"""게임 전용 stdio MCP 연결. 관측 식별자를 모델 프롬프트에 전달하지 않습니다."""
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class CargoTools:
    def __init__(self, db_path):
        self.db_path = str(Path(db_path).resolve())

    async def start(self):
        self.stack = AsyncExitStack()
        try:
            params = StdioServerParameters(command=sys.executable,
                args=['-m', 'cargo_game.mcp_server'], cwd=str(Path(__file__).resolve().parent.parent),
                env={**os.environ, 'CARGO_DB_PATH': self.db_path, 'FASTMCP_LOG_LEVEL': 'ERROR'})
            read, write = await self.stack.enter_async_context(stdio_client(params))
            self.session = await self.stack.enter_async_context(ClientSession(read, write))
            await self.session.initialize()
        except BaseException:
            await self.stack.aclose()
            raise

    async def close(self):
        await self.stack.aclose()

    async def observe(self, tokens):
        rules = await self.session.read_resource('cargo://rules')
        observations = {'rules': json.loads(rules.contents[0].text)}
        for kind in ('inventory', 'memory'):
            reply = await self.session.call_tool('own_'+kind, {'observation_id': tokens[kind]})
            is_error = reply.is_error if hasattr(reply, 'is_error') else reply.isError
            if is_error:
                raise ValueError('Observation failed')
            observations[kind] = json.loads(reply.content[0].text)
        if observations['inventory'].get('card_contract'):
            rules = await self.session.read_resource('cargo://cards-rules')
            observations['rules'] = json.loads(rules.contents[0].text)
        return observations
