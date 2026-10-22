"""카드 버전의 행동 트랜잭션. 기존 거래 버전 저장소와 테스트를 보존합니다."""
import copy
import json
import secrets
from types import SimpleNamespace

from . import cards
from .domain import GameError, accept_offer
from .schemas import Proposal
from .store import GameStore, token_hash


class CardGameStore(GameStore):
    max_model_calls = 30

    def create(self, seed=None):
        game_id, token = secrets.token_hex(16), secrets.token_urlsafe(32)
        state = cards.initial_card_game(seed)
        with self.transaction():
            self.conn.execute('INSERT INTO games VALUES (?,?,?)',
                              (game_id, token_hash(token), json.dumps(state, ensure_ascii=False)))
            self._event(game_id, 'GameCreated', version=0)
        return {'game_id': game_id, 'access_token': token, 'state': cards.card_public_view(state)}

    def _state(self, game_id):
        state = super()._state(game_id)
        if state.get('ruleset') != 'cards-v2':
            raise GameError('이전 거래 버전의 게임입니다. 새 카드 게임을 시작해주세요.', 409)
        return state

    def view(self, game_id):
        with self.lock:
            state = self._state(game_id)
            cursor = self.conn.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE game_id=?', (game_id,)).fetchone()[0]
            return {'game_id': game_id, 'state': cards.card_public_view(state), 'event_cursor': cursor}

    def begin(self, game_id, action):
        fingerprint = json.dumps(action.model_dump(mode='json'), sort_keys=True, ensure_ascii=False)
        with self.transaction():
            row = self.conn.execute('SELECT * FROM actions WHERE game_id=? AND id=?', (game_id, action.action_id)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint:
                    raise GameError('같은 행동 ID의 내용이 다릅니다.')
                return json.loads(row['response']), False
            state = self._state(game_id)
            if state['busy']:
                raise GameError('상인의 응답을 기다려주세요.')
            if state['version'] != action.expected_version:
                raise GameError('게임 상태가 바뀌었습니다. 최신 상태를 확인해주세요.')
            if state['status'] != 'active':
                raise GameError('이미 출항한 게임입니다.')
            start = False
            if action.kind == 'select_deal':
                cards.select_deal(state, action.deal_id)
            elif action.kind == 'prepare_trade':
                cards.prepare(state, action.merchant, action.direction, action.good, action.quantity, action.unit_price)
            elif action.kind == 'play_card':
                kind = next((card['kind'] for card in state['hand'] if card['id'] == action.card_id), None)
                cards.play_card(state, action.card_id, action.discard_ids)
                if kind == 'bulk':
                    expansion = cards.expanded_quantity(state)
                    if expansion:
                        state['preparation']['quantity'], state['preparation']['unit_price'] = expansion
            elif action.kind == 'cancel_preparation':
                cards.require_round(state)
                state['preparation'], state['preparation_effects'] = None, {}
            elif action.kind == 'negotiate':
                start, _ = cards.submit(state, action.message)
                if start:
                    state['busy'], state['busy_action'] = True, action.action_id
                    state['dialogues'].append({'merchant': state['negotiation_context']['merchant'],
                                               'role': 'user', 'text': action.message or '준비한 조건으로 거래를 제안합니다.'})
            elif action.kind == 'accept':
                cards.require_round(state)
                accept_offer(state, action.offer_id)
                # 재고가 바뀌어 준비 조건이 무효가 될 수 있습니다. 사용 효과는 보존하고 취소 선택을 제공합니다.
                state['last_result'] = {'speech': '거래가 체결되었습니다.', 'fallback': False,
                                        'merchant': state['trades'][-1]['merchant'], 'decision': 'offer'}
            elif action.kind == 'end_round':
                cards.finish_round(state)
                state['last_result'] = None
            elif action.kind == 'choose_reward':
                cards.choose_reward(state, action.reward)
            state['version'] += 1
            state['last_action'] = {'kind': action.kind, 'id': action.action_id,
                                    'card_id': action.card_id, 'offer_id': action.offer_id}
            self._save(game_id, state)
            self._event(game_id, 'ActionStarted' if start else 'StateChanged', action_id=action.action_id,
                        action_kind=action.kind, version=state['version'])
            response = {'action_id': action.action_id, 'pending': start, 'version': state['version']}
            self.conn.execute('INSERT INTO actions VALUES (?,?,?,?,?)', (game_id, action.action_id,
                              fingerprint, 'pending' if start else 'done', json.dumps(response)))
            return response, start

    def negotiation_action(self, game_id, action):
        context = self.state(game_id)['negotiation_context']
        return SimpleNamespace(action_id=action.action_id, merchant=context['merchant'],
                               message=context['message'], proposal=Proposal(**context['proposal']))

    def observation_extras(self, state, merchant):
        context = state.get('negotiation_context')
        return {'card_contract': copy.deepcopy(context) if context and context['merchant'] == merchant else None,
                'talks_remaining': state['talk_limit'] - state['talks']}
