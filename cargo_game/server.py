"""과제 2번 전용 FastAPI 서버. 실행: python -m cargo_game.server

작성자: Git 이력 참조
작성일: 2026-10-21
주의: 로컬 단일 프로세스 서비스이며 기존 운영 장애 서버와 DB를 공유하지 않습니다.
"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from .domain import GameError
from .models import OllamaModel
from .negotiation import NegotiationDeps, run_negotiation
from .schemas import Action
from .store import GameStore
from .card_store import CardGameStore
from .card_schemas import CardAction
from .tools import CargoTools

ROOT = Path(__file__).resolve().parent


def create_app(db_path=None, model=None, tools=None, ruleset='legacy'):
    @asynccontextmanager
    async def lifespan(app):
        load_dotenv()
        default_db = 'cargo-cards.db' if ruleset == 'cards' else 'cargo.db'
        path = Path(db_path or os.getenv('CARGO_CARD_DB_PATH' if ruleset == 'cards' else 'CARGO_DB_PATH', str(ROOT/'.runtime'/default_db))).resolve()
        store = (CardGameStore if ruleset == 'cards' else GameStore)(path)
        store.recover()
        actual_model = model or OllamaModel(os.getenv('CARGO_MODEL', 'qwen3.5:2b'),
                                           os.getenv('CARGO_OLLAMA_URL', 'http://127.0.0.1:11434'))
        actual_tools = tools or CargoTools(path)
        try:
            await actual_tools.start()
            app.state.tools_ready = True
        except Exception:
            app.state.tools_ready = False
        app.state.deps = NegotiationDeps(store, actual_tools, actual_model)
        app.state.tasks = set()
        try:
            yield
        finally:
            for task in list(app.state.tasks):
                task.cancel()
            await asyncio.gather(*app.state.tasks, return_exceptions=True)
            await actual_tools.close()
            await actual_model.close()
            store.close()

    app = FastAPI(title='폐허 상점', lifespan=lifespan)

    @app.exception_handler(GameError)
    async def game_error(request, exc):
        payload = {'detail': exc.message}
        if exc.status == 409 and request.path_params.get('game_id'):
            game_id = request.path_params['game_id']
            authorization = request.headers.get('authorization', '')
            if authorization.startswith('Bearer '):
                try:
                    app.state.deps.store.authorize(game_id, authorization[7:])
                    payload['current'] = app.state.deps.store.view(game_id)
                except GameError:
                    pass
        return JSONResponse(payload, status_code=exc.status)

    async def access(game_id: str, request: Request, authorization: str = Header(default='')):
        if not authorization.startswith('Bearer '):
            raise GameError('게임 접근 권한이 없습니다.', 403)
        request.app.state.deps.store.authorize(game_id, authorization[7:])

    @app.get('/api/health')
    async def health():
        status = await app.state.deps.model.available()
        return {'status': 'ok', 'model': status, 'mcp_ready': app.state.tools_ready,
                'ruleset': ruleset,
                'backend': 'ollama' if isinstance(app.state.deps.model, OllamaModel) else 'test_double'}

    @app.post('/api/games', status_code=201)
    async def create_game():
        return app.state.deps.store.create()

    @app.get('/api/games/{game_id}', dependencies=[Depends(access)])
    async def get_game(game_id: str):
        return app.state.deps.store.view(game_id)

    async def execute_action(game_id, body):
        response, start = app.state.deps.store.begin(game_id, body)
        if start:
            resolved = app.state.deps.store.negotiation_action(game_id, body) if ruleset == 'cards' else body
            task = asyncio.create_task(run_negotiation(app.state.deps, game_id, resolved))
            app.state.tasks.add(task)
            task.add_done_callback(app.state.tasks.discard)
        return JSONResponse(response, status_code=202 if response['pending'] else 200)

    if ruleset == 'cards':
        @app.post('/api/games/{game_id}/actions', dependencies=[Depends(access)])
        async def card_action(game_id: str, body: CardAction):
            return await execute_action(game_id, body)
    else:
        @app.post('/api/games/{game_id}/actions', dependencies=[Depends(access)])
        async def action(game_id: str, body: Action):
            return await execute_action(game_id, body)

    @app.get('/api/games/{game_id}/events', dependencies=[Depends(access)])
    async def events(game_id: str, request: Request, after: int = Query(0, ge=0),
                     last_event_id: str = Header(default=''), once: bool = False):
        if last_event_id.isdigit():
            after = max(after, int(last_event_id))
        async def stream():
            cursor = after
            while not await request.is_disconnected():
                rows = app.state.deps.store.events(game_id, cursor)
                for row in rows:
                    cursor = row['id']
                    yield f"id: {cursor}\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                if once:
                    break
                yield ': heartbeat\n\n'
                await asyncio.sleep(.4)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})

    app.mount('/', StaticFiles(directory=ROOT/'static', html=True), name='web')
    return app


app = create_app(ruleset='cards')

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=int(os.getenv('CARGO_PORT', '8200')))
