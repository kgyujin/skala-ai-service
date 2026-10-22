"""작성자: Git 이력 참조
작성목적: 상태 버전, 중복 행동, 원자적 거래와 재시작 복구를 SQLite로 관리합니다.
작성일: 2026-10-21
주의: 모델 호출 중에는 트랜잭션을 유지하지 않습니다. 단일 서버 프로세스용입니다.
"""
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .domain import GameError, accept_offer, add_offer, end_round, initial_game, public_view


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class GameStore:
    max_model_calls = 20

    def observation_extras(self, state, merchant):
        return {}

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS games(id TEXT PRIMARY KEY, token_hash TEXT, state TEXT);
            CREATE TABLE IF NOT EXISTS actions(game_id TEXT, id TEXT, fingerprint TEXT, status TEXT,
                response TEXT, PRIMARY KEY(game_id,id));
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS observations(token_hash TEXT PRIMARY KEY, kind TEXT,
                payload TEXT, expires REAL);
        ''')

    @contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.conn.execute('COMMIT')
            except BaseException:
                self.conn.execute('ROLLBACK')
                raise

    def close(self):
        self.conn.close()

    def _state(self, game_id):
        row = self.conn.execute('SELECT state FROM games WHERE id=?', (game_id,)).fetchone()
        if row is None:
            raise GameError('게임을 찾을 수 없습니다.', 404)
        return json.loads(row['state'])

    def _save(self, game_id, state):
        self.conn.execute('UPDATE games SET state=? WHERE id=?', (json.dumps(state, ensure_ascii=False), game_id))

    def _event(self, game_id, kind, **payload):
        self.conn.execute('INSERT INTO events(game_id,payload) VALUES (?,?)',
                          (game_id, json.dumps({'type': kind, **payload}, ensure_ascii=False)))

    def create(self):
        game_id, token = secrets.token_hex(16), secrets.token_urlsafe(32)
        state = initial_game()
        with self.transaction():
            self.conn.execute('INSERT INTO games VALUES (?,?,?)',
                              (game_id, token_hash(token), json.dumps(state, ensure_ascii=False)))
            self._event(game_id, 'GameCreated', version=0)
        return {'game_id': game_id, 'access_token': token, 'state': public_view(state)}

    def authorize(self, game_id, token):
        with self.lock:
            row = self.conn.execute('SELECT token_hash FROM games WHERE id=?', (game_id,)).fetchone()
        if not row or not secrets.compare_digest(row['token_hash'], token_hash(token)):
            raise GameError('게임 접근 권한이 없습니다.', 403)

    def view(self, game_id):
        with self.lock:
            state = self._state(game_id)
            cursor = self.conn.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE game_id=?',
                                       (game_id,)).fetchone()[0]
            return {'game_id': game_id, 'state': public_view(state), 'event_cursor': cursor}

    def state(self, game_id):
        with self.lock:
            return self._state(game_id)

    def begin(self, game_id, action):
        body = action.model_dump(mode='json')
        fingerprint = json.dumps(body, sort_keys=True, ensure_ascii=False)
        with self.transaction():
            row = self.conn.execute('SELECT * FROM actions WHERE game_id=? AND id=?',
                                     (game_id, action.action_id)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint:
                    raise GameError('같은 행동 ID의 내용이 다릅니다.')
                return json.loads(row['response']), False
            state = self._state(game_id)
            if state['busy']:
                raise GameError('진행 중인 협상을 먼저 마쳐주세요.')
            if state['version'] != action.expected_version:
                raise GameError('게임 상태가 바뀌었습니다. 최신 상태를 확인해주세요.')
            if state['status'] != 'active':
                raise GameError('이미 종료된 게임입니다.')
            if action.kind == 'negotiate':
                if state['talks'] >= 2:
                    raise GameError('이번 라운드의 협상 기회를 모두 사용했습니다.')
                if state['model_calls'] >= self.max_model_calls:
                    raise GameError('게임의 모델 호출 한도에 도달했습니다.')
                state['talks'] += 1
                state['busy'] = True
                state['busy_action'] = action.action_id
                state['dialogues'].append({'merchant': action.merchant, 'role': 'user',
                                           'text': action.message or '제안 카드 조건을 검토해주세요.'})
            elif action.kind == 'accept':
                accept_offer(state, action.offer_id)
                state['last_result'] = {'speech': '거래가 체결되었습니다.', 'fallback': False}
            else:
                end_round(state)
                state['last_result'] = None
            state['version'] += 1
            self._save(game_id, state)
            self._event(game_id, 'ActionStarted' if state['busy'] else 'StateChanged',
                        action_id=action.action_id, version=state['version'])
            response = {'action_id': action.action_id, 'pending': state['busy'], 'version': state['version']}
            self.conn.execute('INSERT INTO actions VALUES (?,?,?,?,?)', (game_id, action.action_id,
                              fingerprint, 'pending' if state['busy'] else 'done', json.dumps(response)))
            return response, action.kind == 'negotiate'

    def reserve_model_call(self, game_id, action_id):
        with self.transaction():
            state = self._state(game_id)
            if state.get('busy_action') != action_id or state['model_calls'] >= self.max_model_calls:
                return False
            state['model_calls'] += 1
            self._save(game_id, state)
            return True

    def progress(self, game_id, action_id, stage):
        with self.transaction():
            state = self._state(game_id)
            if state.get('busy_action') == action_id:
                self._event(game_id, 'Progress', action_id=action_id, stage=stage)

    def finish(self, game_id, action, decision, metrics):
        with self.transaction():
            state = self._state(game_id)
            if state.get('busy_action') != action.action_id:
                return
            fallback = decision is None
            if decision is not None and decision.offer:
                try:
                    add_offer(state, decision.offer.model_dump(), action.merchant)
                except GameError:
                    fallback = True
            speech = ('모델 응답 실패로 기본 응답 사용. 현재 제안은 유지합니다.' if fallback else decision.speech)
            state['last_result'] = {'speech': speech, 'fallback': fallback, 'metrics': metrics,
                                    'merchant': action.merchant,
                                    'decision': decision.decision if not fallback else 'fallback'}
            state['dialogues'].append({'merchant': action.merchant, 'role': 'assistant',
                                       'text': speech, 'fallback': fallback})
            state['busy'] = False
            state.pop('busy_action', None)
            state['version'] += 1
            self._save(game_id, state)
            self.conn.execute("UPDATE actions SET status='done' WHERE game_id=? AND id=?",
                              (game_id, action.action_id))
            self._event(game_id, 'ActionCompleted', action_id=action.action_id,
                        version=state['version'], fallback=fallback)

    def recover(self):
        with self.transaction():
            for row in self.conn.execute('SELECT id,state FROM games').fetchall():
                state = json.loads(row['state'])
                if state['busy']:
                    action_id = state.pop('busy_action')
                    state['busy'] = False
                    state['version'] += 1
                    state['last_result'] = {'fallback': True, 'speech': '재시작으로 협상이 중단되었습니다. 기존 거래는 보존했습니다.'}
                    self._save(row['id'], state)
                    self.conn.execute("UPDATE actions SET status='interrupted' WHERE game_id=? AND id=?", (row['id'], action_id))
                    self._event(row['id'], 'ActionInterrupted', version=state['version'])
            self.conn.execute('DELETE FROM observations')

    def issue_observations(self, game_id, merchant):
        with self.transaction():
            state = self._state(game_id)
            own = {'merchant': merchant, 'self': state['actors'][merchant], 'round': state['round'],
                   'talks_remaining': 2-state['talks'],
                   'offers': [o for o in state['offers'] if o['merchant'] == merchant and o['status'] == 'active']}
            own.update(self.observation_extras(state, merchant))
            memory = {'trades': [t for t in state['trades'] if t['merchant'] == merchant][-5:],
                      'dialogues': [d for d in state['dialogues'] if d['merchant'] == merchant][-6:]}
            tokens = {}
            self.conn.execute('DELETE FROM observations WHERE expires<?', (time.time(),))
            for kind, payload in [('inventory', own), ('memory', memory)]:
                token = secrets.token_urlsafe(32)
                self.conn.execute('INSERT INTO observations VALUES (?,?,?,?)',
                                  (token_hash(token), kind, json.dumps(payload, ensure_ascii=False), time.time()+30))
                tokens[kind] = token
            return tokens

    def events(self, game_id, after):
        with self.lock:
            return [{'id': r['seq'], **json.loads(r['payload'])} for r in self.conn.execute(
                'SELECT seq,payload FROM events WHERE game_id=? AND seq>? ORDER BY seq LIMIT 100',
                (game_id, after))]
