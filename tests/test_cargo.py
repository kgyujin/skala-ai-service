"""거래 보존, API 중복/복구, 상인별 관측 권한과 실제 MCP 연결을 검증합니다."""
import asyncio
import copy
import json
import random
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError
from cargo_game.domain import (GameError, accept_offer, add_offer, end_round, initial_game,
                               public_view, score, validate_offer)
from cargo_game.negotiation import NegotiationDeps, check_decision, run_negotiation
from cargo_game.schemas import Action, Decision, Proposal
from cargo_game.server import create_app
from cargo_game.store import GameStore
from cargo_game.tools import CargoTools


def valid_decision(merchant='a'):
    return {'decision': 'offer', 'speech': '이 조건으로 제안합니다. 카드를 확인해주세요.',
            'offer': {'seller': merchant, 'buyer': 'player', 'good': 'medicine' if merchant == 'a' else 'parts',
                      'quantity': 1, 'unit_price': 15}, 'memory_trade_id': None}


class ScriptedModel:
    name = 'test-double'
    def __init__(self, replies=None):
        self.replies = list(replies) if replies is not None else None
        self.calls = []
    async def available(self): return {'ready': True, 'model': self.name}
    async def close(self): pass
    async def decide(self, merchant, observations, request, error=None, timeout=20):
        self.calls.append((merchant, copy.deepcopy(observations), error))
        reply = self.replies.pop(0) if self.replies else valid_decision(merchant)
        if isinstance(reply, Exception): raise reply
        return json.dumps(reply) if isinstance(reply, dict) else reply, {'tokens': 12}


class LocalTools:
    def __init__(self, store): self.store = store
    async def start(self): pass
    async def close(self): pass
    async def observe(self, tokens):
        from cargo_game.mcp_server import consume
        from unittest.mock import patch
        with patch.dict('os.environ', {'CARGO_DB_PATH': str(self.store.path)}):
            from cargo_game.domain import RULES
            return {'rules': RULES, **{k: consume(t, k) for k, t in tokens.items()}}


class DomainTests(unittest.TestCase):
    def test_initial_score_and_opening_provenance(self):
        g = initial_game()
        self.assertEqual(score(g['actors']['player']), 240)
        self.assertEqual(len(g['offers']), 2)
        self.assertTrue(all(o['source'] == 'opening_rule' for o in g['offers']))

    def test_capacity_and_money_inventory_conserved(self):
        g = initial_game()
        before = copy.deepcopy(g['actors'])
        accept_offer(g, g['offers'][0]['id'])
        self.assertEqual(sum(a['credits'] for a in before.values()), sum(a['credits'] for a in g['actors'].values()))
        for good in before['player']['inventory']:
            self.assertEqual(sum(a['inventory'][good] for a in before.values()),
                             sum(a['inventory'][good] for a in g['actors'].values()))
        self.assertEqual(g['trades_this_round'], 1)

    def test_expired_and_double_accept_rejected(self):
        for expire in (True, False):
            g = initial_game();offer_id = g['offers'][0]['id']
            if expire: end_round(g)
            else: accept_offer(g, offer_id)
            with self.assertRaises(GameError): accept_offer(g, offer_id)

    def test_resource_limits_and_merchant_loss(self):
        base = valid_decision()['offer']
        for change in [{'quantity': 0}, {'quantity': 1.1}, {'quantity': True}, {'unit_price': -1},
                       {'quantity': 13}, {'unit_price': 101}, {'buyer': 'a'}, {'seller': 'b'},
                       {'quantity': 5}, {'quantity': 2, 'unit_price': 100},
                       {'seller': 'player', 'buyer': 'a', 'good': 'fuel', 'unit_price': 90}]:
            with self.subTest(change=change), self.assertRaises(GameError):
                validate_offer(initial_game(), {**base, **change}, 'a')
        g = initial_game();g['actors']['player']['inventory']['fuel'] += 2
        with self.assertRaises(GameError): validate_offer(g, base, 'a')
        # 잔액과 같은 금액은 허용하며 초과 금액만 거절합니다.
        validate_offer(initial_game(), {**base, 'unit_price': 100}, 'a')

    def test_no_negative_states_in_seeded_sequences(self):
        rng = random.Random(105)
        g = initial_game()
        quantities = {good: sum(a['inventory'][good] for a in g['actors'].values()) for good in g['actors']['player']['inventory']}
        for _ in range(100):
            proposal = {'buyer': rng.choice(['player', 'a']), 'seller': 'a', 'good': rng.choice(list(quantities)),
                        'quantity': rng.randint(1, 12), 'unit_price': rng.randint(1, 100)}
            proposal['seller'] = 'a' if proposal['buyer'] == 'player' else 'player'
            try: g['actors'] = validate_offer(g, proposal, 'a')
            except GameError: pass
            self.assertEqual(sum(a['credits'] for a in g['actors'].values()), 300)
            for good, count in quantities.items():
                self.assertEqual(sum(a['inventory'][good] for a in g['actors'].values()), count)
            for a in g['actors'].values():
                self.assertGreaterEqual(min(a['inventory'].values()), 0)
                self.assertGreaterEqual(a['credits'], 0)
                self.assertLessEqual(sum(a['inventory'].values()), 12)

    def test_five_rounds_and_score_ties(self):
        g = initial_game()
        for _ in range(5): end_round(g)
        view = public_view(g)
        self.assertEqual(g['status'], 'finished')
        self.assertEqual([a['rank'] for a in view['ranking']], [1, 1, 1])
        self.assertTrue(all('target' in a for a in view['ranking']))

    def test_public_view_hides_other_actor_state(self):
        view = public_view(initial_game())
        self.assertNotIn('actors', view)
        self.assertNotIn('ranking', view)
        for m in view['merchants']:
            self.assertEqual(set(m), {'id', 'name', 'persona'})

    def test_counteroffer_expires_only_same_merchant(self):
        g = initial_game();old = g['offers'][0]
        add_offer(g, valid_decision()['offer'], 'a')
        self.assertEqual(old['status'], 'expired')
        self.assertEqual(len([o for o in g['offers'] if o['status'] == 'active']), 2)

    def test_strict_schema(self):
        for quantity in ['1', 1.2, True, 0]:
            with self.assertRaises(ValidationError): Proposal(**{**valid_decision()['offer'], 'quantity': quantity})
        with self.assertRaises(ValidationError):
            Decision.model_validate({**valid_decision(), 'decision': 'reject'})

    def test_complete_card_cannot_be_clarified_or_reversed(self):
        g = initial_game();obs = {'memory': {'trades': []}}
        proposal = valid_decision()['offer']
        clarification = {**valid_decision(), 'decision': 'clarify', 'offer': None}
        with self.assertRaises(GameError):
            check_decision(json.dumps(clarification), g, 'a', obs, proposal)
        different = valid_decision();different['offer']['good'] = 'food'
        with self.assertRaises(GameError):
            check_decision(json.dumps(different), g, 'a', obs, proposal)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'cargo.db'
        self.store = GameStore(self.path);self.addCleanup(self.store.close)
        self.created = self.store.create();self.gid = self.created['game_id']

    def action(self, kind='accept', **fields):
        return Action(action_id='action-0001', expected_version=0, kind=kind, **fields)

    def test_idempotency_and_stale_input(self):
        offer = self.created['state']['offers'][0]
        action = self.action(offer_id=offer['id'])
        first = self.store.begin(self.gid, action)
        self.assertEqual(self.store.begin(self.gid, action), first)
        self.assertEqual(len(self.store.state(self.gid)['trades']), 1)
        with self.assertRaises(GameError): self.store.begin(self.gid, action.model_copy(update={'offer_id': 'different'}))
        with self.assertRaises(GameError): self.store.begin(self.gid, self.action('end_round').model_copy(update={'action_id': 'action-0002'}))

    def test_failed_accept_rolls_back(self):
        with self.assertRaises(GameError): self.store.begin(self.gid, self.action(offer_id='missing'))
        self.assertEqual(self.store.state(self.gid)['version'], 0)
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM actions').fetchone()[0], 0)

    def test_concurrent_accepts_only_one_commits(self):
        from concurrent.futures import ThreadPoolExecutor
        offer_id = self.created['state']['offers'][0]['id']
        def submit(number):
            try:
                self.store.begin(self.gid, Action(action_id=f'parallel-{number}', expected_version=0,
                                                  kind='accept', offer_id=offer_id))
                return 'accepted'
            except GameError:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(submit, [1, 2]))
        self.assertCountEqual(outcomes, ['accepted', 'conflict'])
        state = self.store.state(self.gid)
        self.assertEqual(len(state['trades']), 1)
        self.assertEqual(sum(a['credits'] for a in state['actors'].values()), 300)

    def test_access_requires_matching_game_token(self):
        self.store.authorize(self.gid, self.created['access_token'])
        for gid, token in [(self.gid, 'wrong'), ('other', self.created['access_token'])]:
            with self.assertRaises(GameError): self.store.authorize(gid, token)

    def test_pending_action_blocks_others_and_recovers(self):
        action = self.action('negotiate', merchant='a', message='조건 확인')
        self.store.begin(self.gid, action)
        with self.assertRaises(GameError): self.store.begin(self.gid, self.action('end_round').model_copy(update={'action_id': 'action-0002', 'expected_version': 1}))
        self.store.recover()
        g = self.store.state(self.gid)
        self.assertFalse(g['busy']);self.assertEqual(g['talks'], 1)
        self.assertEqual(g['version'], 2)
        self.assertEqual(len(g['trades']), 0)
        self.assertEqual(self.store.events(self.gid, 0)[-1]['type'], 'ActionInterrupted')

    def test_observation_one_time_kind_and_isolation(self):
        from cargo_game.mcp_server import consume
        from unittest.mock import patch
        tokens = self.store.issue_observations(self.gid, 'a')
        with patch.dict('os.environ', {'CARGO_DB_PATH': str(self.path)}):
            with self.assertRaises(ValueError): consume(tokens['inventory'], 'memory')
            own = consume(tokens['inventory'], 'inventory')
            self.assertEqual(own['merchant'], 'a')
            self.assertNotIn('actors', own)
            with self.assertRaises(ValueError): consume(tokens['inventory'], 'inventory')
            self.store.conn.execute('UPDATE observations SET expires=0')
            with self.assertRaises(ValueError): consume(tokens['memory'], 'memory')

    def test_trade_memory_is_not_shared_between_merchants(self):
        from cargo_game.mcp_server import consume
        from unittest.mock import patch
        state = self.store.state(self.gid)
        accept_offer(state, state['offers'][0]['id'])
        self.store._save(self.gid, state)
        with patch.dict('os.environ', {'CARGO_DB_PATH': str(self.path)}):
            for merchant, count in [('a', 1), ('b', 0)]:
                tokens = self.store.issue_observations(self.gid, merchant)
                memory = consume(tokens['memory'], 'memory')
                self.assertEqual(len(memory['trades']), count)

    def test_talk_and_trade_round_limits(self):
        state = self.store.state(self.gid)
        state['talks'] = 2
        self.store._save(self.gid, state)
        with self.assertRaises(GameError):
            self.store.begin(self.gid, self.action('negotiate', merchant='a', message='조건'))
        state['trades_this_round'] = 2
        self.store._save(self.gid, state)
        with self.assertRaises(GameError):
            self.store.begin(self.gid, self.action(offer_id=state['offers'][0]['id']))


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store = GameStore(Path(self.tmp.name)/'test.db');self.addCleanup(self.store.close)
        self.created = self.store.create();self.gid = self.created['game_id']

    async def run_agent(self, replies, merchant='a'):
        action = Action(action_id='agent-action', expected_version=0, kind='negotiate', merchant=merchant, message='제안해주세요')
        self.store.begin(self.gid, action)
        model = ScriptedModel(replies)
        await run_negotiation(NegotiationDeps(self.store, LocalTools(self.store), model), self.gid, action)
        return self.store.state(self.gid), model

    async def test_actual_graph_uses_scoped_memory_and_model(self):
        state, model = await self.run_agent([valid_decision()])
        self.assertFalse(state['busy'])
        self.assertFalse(state['last_result']['fallback'])
        self.assertEqual(state['model_calls'], 1)
        self.assertEqual(model.calls[0][1]['inventory']['merchant'], 'a')
        self.assertNotIn('b', model.calls[0][1]['inventory'])

    async def test_one_repair_then_fallback_preserves_offer(self):
        original = self.created['state']['offers'][0]['id']
        state, model = await self.run_agent(['broken', 'still broken'])
        self.assertEqual(state['model_calls'], 2)
        self.assertTrue(state['last_result']['fallback'])
        self.assertEqual(state['talks'], 1)
        self.assertTrue(any(o['id'] == original and o['status'] == 'active' for o in state['offers']))
        self.assertEqual(len(model.calls), 2)

    async def test_invalid_proposal_repaired(self):
        invalid = valid_decision();invalid['offer']['quantity'] = 12
        state, model = await self.run_agent([invalid, valid_decision()])
        self.assertFalse(state['last_result']['fallback'])
        self.assertEqual(state['last_result']['metrics']['validation_failures'], 1)
        self.assertTrue(model.calls[1][2])

    async def test_timeout_and_total_budget(self):
        state = self.store.state(self.gid);state['model_calls'] = 19;self.store._save(self.gid, state)
        state, model = await self.run_agent([asyncio.TimeoutError()])
        self.assertEqual(state['model_calls'], 20)
        self.assertEqual(len(model.calls), 1)
        self.assertTrue(state['last_result']['fallback'])

    async def test_mcp_failure_is_visible_and_no_model_called(self):
        action = Action(action_id='agent-action', expected_version=0, kind='negotiate', merchant='a', message='조건')
        self.store.begin(self.gid, action)
        tools = LocalTools(self.store)
        async def fail(tokens): raise ValueError('private details')
        tools.observe = fail
        model = ScriptedModel()
        await run_negotiation(NegotiationDeps(self.store, tools, model), self.gid, action)
        state = self.store.state(self.gid)
        self.assertTrue(state['last_result']['fallback']);self.assertEqual(model.calls, [])
        self.assertNotIn('private details', json.dumps(public_view(state)))

    async def test_reject_and_clarify_without_mutation(self):
        for kind in ['reject', 'clarify']:
            decision = {**valid_decision(), 'decision': kind, 'offer': None}
            original = copy.deepcopy(self.store.state(self.gid)['actors'])
            state, _ = await self.run_agent([decision])
            self.assertEqual(state['actors'], original)
            self.assertFalse(state['last_result']['fallback'])
            # 다음 독립 시험을 위해 초기화합니다.
            self.store._save(self.gid, initial_game())
            self.store.conn.execute('DELETE FROM actions')

    async def test_real_mcp_transport_and_replay_denial(self):
        tools = CargoTools(self.store.path);await tools.start()
        try:
            tokens = self.store.issue_observations(self.gid, 'a')
            observations = await tools.observe(tokens)
            self.assertEqual(observations['inventory']['merchant'], 'a')
            with self.assertRaises(ValueError): await tools.observe(tokens)
        finally: await tools.close()


class HttpTests(unittest.TestCase):
    def test_full_game_http_sse_idempotency_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'test.db';model = ScriptedModel()
            app = create_app(path, model=model)
            with TestClient(app) as client:
                created = client.post('/api/games').json();gid = created['game_id']
                base = '/api/games/'+gid;headers = {'Authorization': 'Bearer '+created['access_token']}
                self.assertEqual(client.get(base).status_code, 403)
                body = {'kind': 'negotiate', 'action_id': 'http-action1', 'expected_version': 0,
                        'merchant': 'a', 'message': '의약품을 사고 싶어요'}
                self.assertEqual(client.post(base+'/actions', headers=headers, json=body).status_code, 202)
                for _ in range(100):
                    view = client.get(base, headers=headers).json()
                    if not view['state']['busy']: break
                    import time;time.sleep(.01)
                self.assertFalse(view['state']['busy'])
                self.assertEqual(len(model.calls), 1)
                self.assertEqual(client.post(base+'/actions', headers=headers, json=body).status_code, 202)
                self.assertEqual(len(model.calls), 1)
                offer = next(o for o in view['state']['offers'] if o['merchant'] == 'a')
                acceptance = {'kind': 'accept', 'action_id': 'http-accept1',
                              'expected_version': view['state']['version'], 'offer_id': offer['id']}
                self.assertEqual(client.post(base+'/actions', headers=headers, json=acceptance).status_code, 200)
                self.assertEqual(client.post(base+'/actions', headers=headers, json=acceptance).status_code, 200)
                stale = client.post(base+'/actions', headers=headers, json={**acceptance, 'action_id': 'http-accept2'})
                self.assertEqual(stale.status_code, 409)
                self.assertIn('current', stale.json())
                self.assertNotIn('actors', stale.json()['current']['state'])
                bad = {**acceptance, 'expected_version': '2'}
                self.assertEqual(client.post(base+'/actions', headers=headers, json=bad).status_code, 422)
                stream = client.get(base+'/events?once=true', headers=headers)
                self.assertIn('ActionCompleted', stream.text)
                cursor = client.get(base, headers=headers).json()['event_cursor']
                self.assertEqual(client.get(base+f'/events?once=true&after={cursor}', headers=headers).text, '')
                for i in range(5):
                    state = client.get(base, headers=headers).json()['state']
                    self.assertEqual(client.post(base+'/actions', headers=headers, json={
                        'kind': 'end_round', 'action_id': f'http-round-{i}', 'expected_version': state['version']}).status_code, 200)
                state = client.get(base, headers=headers).json()['state']
                self.assertEqual(state['status'], 'finished');self.assertEqual(len(state['trades']), 1)
            with TestClient(create_app(path, model=ScriptedModel())) as client:
                state = client.get(base, headers=headers).json()['state']
                self.assertEqual(state['status'], 'finished');self.assertEqual(len(state['trades']), 1)


if __name__ == '__main__': unittest.main()
