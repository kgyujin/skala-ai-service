"""작성자: Git 이력 참조
작성목적: 물자 보존, 거래 자격과 점수를 결정하는 순수 게임 규칙.
작성일: 2026-10-21
주의: 모델 출력은 이 모듈의 검증을 통과해야만 상태에 반영합니다.
"""
import copy
import uuid

GOODS = {'fuel': '연료', 'food': '식량', 'medicine': '의약품', 'parts': '부품'}
VALUES = dict(zip(GOODS, [10, 8, 14, 12]))
NAMES = {'player': '당신', 'a': '이안', 'b': '베라'}
PERSONAS = {'a': '목표 물자 확보를 우선하는 신중한 보급 상인',
            'b': '수익과 남은 기회를 중시하는 단호한 부품 상인'}
RULES = {'rounds': 5, 'talks_per_round': 2, 'trades_per_round': 2,
         'max_model_calls': 20, 'capacity': 12, 'values': VALUES,
         'goods': GOODS, 'quantity': [1, 12], 'unit_price': [1, 100],
         'score': '크레딧 + 목표 이내 수량×가치×2 + 초과 수량×가치/2 + 전체 목표 달성 100',
         'settlement': '단일 물자와 크레딧 교환. 사용자 카드 수락 시에만 체결.',
         'merchant_constraint': '거래 후 상인 자신의 자산 점수가 감소하면 제안 불가'}


class GameError(Exception):
    def __init__(self, message, status=409):
        self.message, self.status = message, status
        super().__init__(message)


def score(actor):
    total = actor['credits']
    for good, value in VALUES.items():
        quantity, target = actor['inventory'][good], actor['target'][good]
        total += min(quantity, target) * value * 2 + max(0, quantity-target) * value // 2
    if all(actor['inventory'][g] >= actor['target'][g] for g in GOODS):
        total += 100
    return total


def initial_game():
    actors = {}
    for who, stock, target in [('player', [6, 2, 0, 2], [2, 2, 3, 3]),
                                ('a', [0, 6, 4, 0], [3, 2, 1, 2]),
                                ('b', [2, 0, 2, 6], [2, 3, 2, 2])]:
        actors[who] = {'credits': 100, 'capacity': 12,
                       'inventory': dict(zip(GOODS, stock)), 'target': dict(zip(GOODS, target))}
        actors[who]['initial_score'] = score(actors[who])
    game = {'round': 1, 'version': 0, 'status': 'active', 'talks': 0, 'trades_this_round': 0,
            'model_calls': 0, 'actors': actors, 'offers': [], 'trades': [], 'dialogues': [],
            'last_result': None, 'busy': False}
    opening_offers(game)
    return game


def validate_offer(game, offer, merchant):
    if merchant not in PERSONAS or {offer.get('buyer'), offer.get('seller')} != {'player', merchant}:
        raise GameError('사용자와 선택한 상인 사이의 거래만 가능합니다.')
    if offer.get('good') not in GOODS:
        raise GameError('지원하지 않는 물자입니다.')
    for key, maximum in [('quantity', 12), ('unit_price', 100)]:
        if type(offer.get(key)) is not int or not 1 <= offer[key] <= maximum:
            raise GameError('수량과 단가는 허용 범위의 정수여야 합니다.')
    buyer, seller = game['actors'][offer['buyer']], game['actors'][offer['seller']]
    quantity, total = offer['quantity'], offer['quantity'] * offer['unit_price']
    if seller['inventory'][offer['good']] < quantity:
        raise GameError('판매자의 재고가 부족합니다.')
    if buyer['credits'] < total:
        raise GameError('구매자의 크레딧이 부족합니다.')
    if sum(buyer['inventory'].values()) + quantity > buyer['capacity']:
        raise GameError('구매자의 적재 공간이 부족합니다.')
    changed = copy.deepcopy(game['actors'])
    changed[offer['buyer']]['credits'] -= total
    changed[offer['seller']]['credits'] += total
    changed[offer['buyer']]['inventory'][offer['good']] += quantity
    changed[offer['seller']]['inventory'][offer['good']] -= quantity
    if score(changed[merchant]) < score(game['actors'][merchant]):
        raise GameError('상인의 납품 가치보다 불리한 조건입니다.')
    return changed


def add_offer(game, proposal, merchant, source='agent'):
    validate_offer(game, proposal, merchant)
    for old in game['offers']:
        if old['merchant'] == merchant and old['status'] == 'active':
            old['status'] = 'expired'
    offer = {**proposal, 'id': uuid.uuid4().hex, 'merchant': merchant, 'round': game['round'],
             'total': proposal['quantity'] * proposal['unit_price'], 'source': source,
             'status': 'active', 'version': game['version'] + 1}
    game['offers'].append(offer)
    return offer


def opening_offers(game):
    for merchant in PERSONAS:
        actor = game['actors'][merchant]
        # 초기 제안은 AI 생성으로 가장하지 않고 잉여 재고 중 하나를 판매합니다.
        surplus = [(g, actor['inventory'][g]-actor['target'][g]) for g in GOODS]
        for good, count in sorted(surplus, key=lambda x: -x[1]):
            if count <= 0:
                continue
            try:
                add_offer(game, {'buyer': 'player', 'seller': merchant, 'good': good,
                                 'quantity': 1, 'unit_price': VALUES[good] + (1 if merchant == 'a' else 3)},
                          merchant, 'opening_rule')
                break
            except GameError:
                continue


def accept_offer(game, offer_id):
    if game['trades_this_round'] >= 2:
        raise GameError('이번 라운드의 거래 기회를 모두 사용했습니다.')
    offer = next((o for o in game['offers'] if o['id'] == offer_id), None)
    if not offer or offer['status'] != 'active' or offer['round'] != game['round']:
        raise GameError('이미 체결되었거나 만료된 제안입니다.')
    game['actors'] = validate_offer(game, offer, offer['merchant'])
    offer['status'] = 'accepted'
    game['trades'].append({**offer, 'trade_id': uuid.uuid4().hex})
    game['trades_this_round'] += 1
    # 다른 제안은 보존하되 새 재고/잔액으로 자격을 다시 검사합니다.
    for active in game['offers']:
        if active['status'] == 'active':
            try:
                validate_offer(game, active, active['merchant'])
            except GameError:
                active['status'] = 'expired'


def end_round(game):
    for offer in game['offers']:
        if offer['status'] == 'active':
            offer['status'] = 'expired'
    if game['round'] == 5:
        game['status'] = 'finished'
    else:
        game['round'] += 1
        game['talks'] = game['trades_this_round'] = 0
        opening_offers(game)


def public_view(game):
    player = copy.deepcopy(game['actors']['player'])
    player['score'] = score(player)
    player['gain'] = player['score'] - player['initial_score']
    view = {k: copy.deepcopy(game[k]) for k in ['round', 'version', 'status', 'talks',
            'trades_this_round', 'model_calls', 'busy', 'last_result', 'dialogues']}
    view.update(player=player, goods=GOODS, merchants=[{'id': m, 'name': NAMES[m], 'persona': PERSONAS[m]}
                for m in PERSONAS], offers=[], trades=copy.deepcopy(game['trades']))
    for offer in game['offers']:
        if offer['status'] == 'active':
            entry = copy.deepcopy(offer)
            try:
                validate_offer(game, offer, offer['merchant'])
                entry['acceptable'] = game['status'] == 'active' and not game['busy'] and game['trades_this_round'] < 2
            except GameError:
                entry['acceptable'] = False
            view['offers'].append(entry)
    if game['status'] == 'finished':
        ranked = sorted([{'id': who, 'name': NAMES[who], 'score': score(actor),
                          'gain': score(actor)-actor['initial_score'], 'target': actor['target']}
                         for who, actor in game['actors'].items()], key=lambda a: -a['gain'])
        for actor in ranked:
            actor['rank'] = 1 + sum(other['gain'] > actor['gain'] for other in ranked)
        view['ranking'] = ranked
    return view
