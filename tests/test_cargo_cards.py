"""카드 효과, 상태 전이, 보존, 재시작과 실제 MCP 경계의 회귀 검증."""
import copy
import json
import random
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError

from cargo_game import cards
from cargo_game.card_schemas import CardAction
from cargo_game.card_store import CardGameStore
from cargo_game.domain import GameError, accept_offer, add_offer
from cargo_game.negotiation import check_decision
from cargo_game.server import create_app
from cargo_game.models import decision_schema


class BoundModel:
    name = 'card-test-double'

    def __init__(self, invalid=False):
        self.invalid, self.calls = invalid, []

    async def available(self):
        return {'ready': True, 'model': self.name}

    async def close(self):
        pass

    async def decide(self, merchant, observations, request, error=None, timeout=20):
        self.calls.append(copy.deepcopy(observations))
        proposal = dict(request['proposal'])
        contract = observations['inventory']['card_contract']
        proposal['unit_price'] = 100 if self.invalid else contract['bounds']['max_price']
        return json.dumps({'decision': 'offer', 'speech': '이 조건이라면 거래할 수 있어요.',
                           'offer': proposal, 'memory_trade_id': None}), {'tokens': 10}


class CardRulesTests(unittest.TestCase):
    def setUp(self):
        self.game = cards.initial_card_game(105)

    def give(self, kind):
        card = cards.make_card(self.game, kind)
        self.game['hand'].append(card)
        return card['id']

    def test_price_effect_changes_enforced_range_and_stops_at_cap(self):
        cards.prepare(self.game, 'a', 'buy', 'medicine', 1)
        before = cards.quote(self.game)
        cards.play_card(self.game, self.give('bargain'))
        self.assertEqual(cards.quote(self.game)['max_price'], before['max_price']-1)
        cards.play_card(self.game, self.give('bargain'))
        self.assertEqual(self.game['preparation_effects']['advantage'], 2)
        third = self.give('bargain')
        snapshot = copy.deepcopy(self.game)
        with self.assertRaises(GameError):
            cards.play_card(self.game, third)
        self.assertEqual(self.game, snapshot)

    def test_card_ownership_reuse_and_direction(self):
        cards.prepare(self.game, 'a', 'sell', 'fuel', 1)
        with self.assertRaises(GameError):
            cards.play_card(self.game, self.give('bargain'))
        focus = self.give('focus')
        cards.play_card(self.game, focus)
        with self.assertRaises(GameError):
            cards.play_card(self.game, focus)
        with self.assertRaises(GameError):
            cards.play_card(self.game, 'client-invented-card')

    def test_information_is_targeted_and_repeat_is_blocked(self):
        cards.prepare(self.game, 'a', 'buy', 'medicine', 1)
        cards.play_card(self.game, self.give('scan'))
        self.assertEqual(self.game['discoveries']['a']['medicine']['demand'], '여유')
        self.assertNotIn('b', self.game['discoveries'])
        with self.assertRaises(GameError):
            cards.play_card(self.game, self.give('scan'))
        cards.play_card(self.game, self.give('analysis'))
        self.assertEqual(self.game['discoveries']['a']['medicine']['shortage'], 0)

    def test_preparation_target_cannot_change_after_card(self):
        cards.prepare(self.game, 'a', 'buy', 'medicine', 1)
        cards.play_card(self.game, self.give('bargain'))
        with self.assertRaises(GameError):
            cards.prepare(self.game, 'b', 'buy', 'parts', 1)

    def test_redraw_cannot_recycle_used_card_and_duplicate_selection(self):
        redraw = self.give('redraw')
        discard = [c['id'] for c in self.game['hand'] if c['id'] != redraw][:2]
        with self.assertRaises(GameError):
            cards.play_card(self.game, redraw, [discard[0], discard[0]])
        count = len(self.game['hand'])
        cards.play_card(self.game, redraw, discard)
        self.assertEqual(len(self.game['hand']), count-1)
        self.assertIn(redraw, [c['id'] for c in self.game['spent']])
        self.assertNotIn(redraw, [c['id'] for c in self.game['hand']])

    def test_extra_negotiation_once_per_round_and_free_submission(self):
        cards.play_card(self.game, self.give('extra'))
        self.assertEqual(self.game['talk_limit'], 3)
        with self.assertRaises(GameError):
            cards.play_card(self.game, self.give('extra'))
        cards.prepare(self.game, 'a', 'buy', 'medicine', 1)
        cards.play_card(self.game, self.give('swift'))
        self.game['energy'] = 0
        start, offer = cards.submit(self.game, '')
        self.assertTrue(start)
        self.assertIsNone(offer)
        self.assertEqual(self.game['talks'], 1)
        self.assertEqual(self.game['energy'], 0)
        self.assertEqual(self.game['preparation_effects'], {})

    def test_standard_contract_and_bulk_do_not_bypass_inventory(self):
        cards.prepare(self.game, 'a', 'buy', 'medicine', 1)
        cards.play_card(self.game, self.give('bulk'))
        with self.assertRaises(GameError):
            cards.prepare(self.game, 'a', 'buy', 'medicine', 4)
        self.game['actors']['player']['inventory']['fuel'] -= 2
        cards.prepare(self.game, 'a', 'buy', 'medicine', 4)
        cards.play_card(self.game, self.give('standard'))
        self.game['energy'] = 0
        with self.assertRaises(GameError):
            cards.submit(self.game, '')  # 제출에 필요한 협상력이 없습니다.
        self.game['energy'] = 1
        start, offer = cards.submit(self.game, '')
        self.assertFalse(start)
        self.assertEqual(offer['source'], 'standard_rule')
        accept_offer(self.game, offer['id'])
        self.assertEqual(self.game['actors']['player']['inventory']['medicine'], 4)
        with self.assertRaises(GameError):
            accept_offer(self.game, offer['id'])

    def test_reward_phase_seed_resume_and_no_order_leak(self):
        other = cards.initial_card_game(105)
        self.assertEqual(self.game, other)
        before = cards.card_public_view(self.game)
        self.assertNotIn('seed', before)
        self.assertNotIn('draw_pile', before)
        self.assertNotIn('actors', before)
        cards.finish_round(self.game)
        self.assertEqual(self.game['phase'], 'reward')
        with self.assertRaises(GameError):
            cards.finish_round(self.game)
        loaded = json.loads(json.dumps(self.game))
        choice = self.game['reward_choices'][0]
        cards.choose_reward(self.game, choice)
        cards.choose_reward(loaded, choice)
        self.assertEqual(self.game, loaded)
        self.assertEqual(cards.card_public_view(self.game)['piles']['total'], 11)
        self.assertEqual(len(self.game['hand']), 5)

    def test_history_requires_actual_trade_and_quantity_price_bounds(self):
        cards.prepare(self.game, 'a', 'buy', 'medicine', 1)
        with self.assertRaises(GameError):
            cards.play_card(self.game, self.give('history'))
        cards.submit(self.game, '')
        p = self.game['negotiation_context']['proposal']
        decision = {'decision': 'offer', 'speech': '제안합니다.', 'offer': {**p, 'unit_price': 100}, 'memory_trade_id': None}
        with self.assertRaises(GameError):
            check_decision(json.dumps(decision), self.game, 'a', {'memory': {'trades': []}}, p)

    def test_fixed_route_finishes_targets_and_preserves_all_goods_and_credits(self):
        totals = {good: sum(a['inventory'][good] for a in self.game['actors'].values()) for good in cards.GOODS}
        route = [('a','sell','fuel',2),('a','buy','medicine',2),('a','sell','fuel',1),
                 ('a','buy','medicine',1),('b','buy','parts',1)]
        for index, (merchant,direction,good,quantity) in enumerate(route):
            if self.game['trades_this_round'] == 2:
                cards.finish_round(self.game);cards.choose_reward(self.game, None)
            cards.prepare(self.game,merchant,direction,good,quantity)
            q=cards.quote(self.game)
            p=cards.proposal_for(self.game['preparation'],q['base_price'])
            offer=add_offer(self.game,p,merchant,'test_rule')
            accept_offer(self.game,offer['id'])
            self.assertEqual(sum(a['credits'] for a in self.game['actors'].values()),300)
            for g,total in totals.items():
                self.assertEqual(sum(a['inventory'][g] for a in self.game['actors'].values()),total)
        while self.game['status']=='active':
            cards.finish_round(self.game)
            if self.game['phase']=='reward':cards.choose_reward(self.game,None)
        self.assertTrue(all(self.game['actors']['player']['inventory'][g]>=n for g,n in self.game['actors']['player']['target'].items()))
        self.assertEqual(cards.card_public_view(self.game)['phase'],'finished')

    def test_input_schema_blocks_effect_injection_and_bool(self):
        for body in [{'kind':'play_card','card_id':'card-1','energy':99},
                     {'kind':'prepare_trade','merchant':'a','direction':'buy','good':'medicine','quantity':True},
                     {'kind':'negotiate','reward':'standard'}]:
            with self.assertRaises(ValidationError):
                CardAction(action_id='schema-test-001',expected_version=0,**body)

    def test_public_view_handles_preparation_invalidated_by_another_trade(self):
        cards.prepare(self.game,'a','buy','medicine',2)
        offer=add_offer(self.game,{'buyer':'player','seller':'b','good':'parts','quantity':1,'unit_price':10},'b')
        accept_offer(self.game,offer['id'])
        public=cards.card_public_view(self.game)
        self.assertIsNone(public['quote'])
        self.assertIn('가능한 거래가 없습니다',public['quote_error'])

    def test_pending_card_negotiation_recovery_preserves_consumed_cards(self):
        with tempfile.TemporaryDirectory() as folder:
            store=CardGameStore(Path(folder)/'cards.db')
            try:
                gid=store.create(seed=105)['game_id'];state=store.state(gid)
                cards.prepare(state,'a','buy','medicine',1)
                card=cards.make_card(state,'bargain');state['hand'].append(card);cards.play_card(state,card['id'])
                store._save(gid,state)
                store.begin(gid,CardAction(action_id='pending-recovery',expected_version=0,kind='negotiate'))
                before=store.state(gid);store.recover();after=store.state(gid)
                self.assertFalse(after['busy'])
                self.assertEqual(before['hand'],after['hand'])
                self.assertEqual(before['spent'],after['spent'])
                self.assertEqual(after['preparation_effects'],{})
                self.assertEqual(after['talks'],1)
            finally:store.close()

    def test_model_schema_is_scoped_to_the_actual_prepared_card_contract(self):
        cards.prepare(self.game,'a','buy','medicine',1)
        cards.play_card(self.game,self.give('bargain'))
        cards.submit(self.game,'')
        schema=decision_schema({'inventory':{'card_contract':self.game['negotiation_context']}})
        fields=schema['$defs']['Proposal']['properties']
        self.assertEqual(fields['unit_price']['enum'],[7,8])
        self.assertEqual(fields['seller']['enum'],['a'])
        self.assertEqual(fields['buyer']['enum'],['player'])
        self.assertEqual(fields['quantity']['enum'],[1])
        self.assertEqual(fields['good']['enum'],['medicine'])

    def test_product_cards_choose_needed_quantity_and_only_valid_inventory(self):
        deals=cards.available_deals(self.game)
        parts=next(d for d in deals if d['merchant']=='b' and d['direction']=='buy' and d['good']=='parts')
        self.assertEqual(parts['quantity'],1)
        self.assertNotIn('target',parts)
        self.assertNotIn('inventory',parts)
        for deal in deals:
            cards.select_deal(self.game,deal['id'])
            self.assertEqual(self.game['preparation']['quantity'],deal['quantity'])
        self.game['actors']['player']['inventory']['fuel']+=2
        self.assertFalse(any(d['direction']=='buy' for d in cards.available_deals(self.game)))
        self.assertTrue(any(d['direction']=='sell' for d in cards.available_deals(self.game)))

    def test_product_switch_expires_effects_without_refunding_used_card(self):
        with tempfile.TemporaryDirectory() as folder:
            store=CardGameStore(Path(folder)/'shop.db')
            try:
                gid=store.create(seed=105)['game_id'];s=store.state(gid)
                card=cards.make_card(s,'premium');s['hand'].append(card);store._save(gid,s)
                first=next(d for d in cards.available_deals(s) if d['merchant']=='a' and d['good']=='fuel')
                store.begin(gid,CardAction(action_id='shop-select-001',expected_version=0,kind='select_deal',deal_id=first['id']))
                store.begin(gid,CardAction(action_id='shop-card-001',expected_version=1,kind='play_card',card_id=card['id']))
                s=store.state(gid);second=next(d for d in cards.available_deals(s) if d['merchant']=='b' and d['good']=='parts')
                store.begin(gid,CardAction(action_id='shop-select-002',expected_version=2,kind='select_deal',deal_id=second['id']))
                final=store.state(gid)
                self.assertEqual(final['preparation']['merchant'],'b')
                self.assertEqual(final['preparation_effects'],{})
                self.assertEqual(final['energy'],2)
                self.assertIn(card,final['spent'])
            finally:store.close()

    def test_bulk_card_automatically_expands_selected_product(self):
        with tempfile.TemporaryDirectory() as folder:
            store=CardGameStore(Path(folder)/'shop.db')
            try:
                gid=store.create(seed=105)['game_id'];s=store.state(gid)
                s['actors']['player']['inventory']['fuel']=4
                card=cards.make_card(s,'bulk');s['hand'].append(card);store._save(gid,s)
                deal=next(d for d in cards.available_deals(s) if d['merchant']=='a' and d['good']=='medicine')
                store.begin(gid,CardAction(action_id='bulk-select-001',expected_version=0,kind='select_deal',deal_id=deal['id']))
                store.begin(gid,CardAction(action_id='bulk-card-001',expected_version=1,kind='play_card',card_id=card['id']))
                self.assertEqual(store.state(gid)['preparation']['quantity'],3)
                self.assertEqual(store.view(gid)['state']['quote']['quantity'],3)
            finally:store.close()

    def test_forged_product_selection_does_not_discard_existing_effects(self):
        cards.select_deal(self.game,'a:buy:medicine:2')
        cards.play_card(self.game,self.give('bargain'))
        previous=copy.deepcopy(self.game)
        with self.assertRaises(GameError):cards.select_deal(self.game,'a:buy:medicine:4')
        self.assertEqual(self.game,previous)


class CardHttpTests(unittest.TestCase):
    def test_real_mcp_cards_transaction_retry_sse_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cards.db';model=BoundModel()
            with TestClient(create_app(path,model=model,ruleset='cards')) as client:
                created=client.post('/api/games').json();gid=created['game_id'];base=f'/api/games/{gid}'
                headers={'Authorization':'Bearer '+created['access_token']}
                def act(kind,**fields):
                    state=client.get(base,headers=headers).json()['state']
                    body={'kind':kind,'action_id':f'action-{state["version"]:06d}','expected_version':state['version'],**fields}
                    response=client.post(base+'/actions',headers=headers,json=body)
                    self.assertIn(response.status_code,(200,202),response.text)
                    return body
                act('prepare_trade',merchant='a',direction='buy',good='medicine',quantity=1)
                body=act('negotiate')
                for _ in range(150):
                    state=client.get(base,headers=headers).json()['state']
                    if not state['busy']:break
                    time.sleep(.01)
                self.assertFalse(state['busy'])
                self.assertFalse(state['last_result']['fallback'])
                self.assertEqual(len(model.calls),1)
                self.assertEqual(model.calls[0]['rules']['ruleset'],'cards-v2')
                self.assertNotIn('b',model.calls[0]['inventory']['self'])
                self.assertEqual(client.post(base+'/actions',headers=headers,json=body).status_code,202)
                self.assertEqual(len(model.calls),1)
                acceptance=act('accept',offer_id=state['offers'][0]['id'])
                self.assertEqual(client.post(base+'/actions',headers=headers,json=acceptance).status_code,200)
                self.assertEqual(client.get(base,headers=headers).json()['state']['player']['inventory']['medicine'],1)
                act('end_round');act('choose_reward',reward=None)
                saved=client.get(base,headers=headers).json()['state']
                self.assertIn('StateChanged',client.get(base+'/events?once=true',headers=headers).text)
            with TestClient(create_app(path,model=BoundModel(),ruleset='cards')) as client:
                restored=client.get(base,headers=headers).json()['state']
                self.assertEqual(restored,saved)


if __name__=='__main__':
    unittest.main()
