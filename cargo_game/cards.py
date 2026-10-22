"""라스트 카고 카드 규칙. 카드 효과와 난수는 서버가 소유하며 모델은 변경하지 못합니다.

작성자: Git 이력 참조
작성일: 2026-10-21
입력: 저장된 게임 상태와 검증된 행동. 출력: 변경된 상태와 공개 가능한 관측.
"""
import copy
import random
import secrets

from .domain import (GOODS, NAMES, PERSONAS, GameError, add_offer,
                     initial_game, public_view, validate_offer)


CATALOG = {
    'scan': ('눈치 보기', 1, '상인이 필요한 물자와 여유 물자를 알아봅니다.', '정보'),
    'bargain': ('값 깎기', 1, '입고 제안 상한을 개당 1 낮춥니다.', '입고'),
    'premium': ('제값 받기', 1, '판매 제안 하한을 개당 1 높입니다.', '판매'),
    'redraw': ('다시 고르기', 0, '다른 손패 최대 2장을 교환합니다.', '전략'),
    'focus': ('숨 고르기', 0, '협상력을 1 얻습니다. 최대 4.', '전략'),
    'bulk': ('한꺼번에', 1, '가능한 수량을 최대 4개까지 자동으로 늘립니다.', '거래'),
    'breakthrough': ('끈질긴 흥정', 1, '다음 거래의 가격 범위를 개당 2 조정합니다.', '흥정'),
    'swift': ('단도직입', 0, '다음 흥정의 협상력 비용이 0이 됩니다.', '전략'),
    'extra': ('잠깐만요', 0, '오늘 흥정 기회를 1회 얻습니다. 하루 한 번.', '전략'),
    'history': ('오랜 거래', 0, '거래한 적 있는 상인에게 개당 1 양보를 얻습니다.', '신뢰'),
    'analysis': ('장부 살피기', 1, '상인의 해당 물자 목표 부족분을 확인합니다.', '정보'),
    'standard': ('악수', 1, '다음 제안은 기준 가격으로 확정합니다. AI 호출 없음.', '거래'),
}
BASIC = list(CATALOG)[:5]
REWARDS = list(CATALOG)[5:]
PREPARATION_CARDS = {'bargain', 'premium', 'bulk', 'breakthrough', 'swift', 'history', 'standard'}


def shuffled(game, sequence):
    ordered = list(sequence)
    random.Random(f"{game['seed']}:{game['rng_step']}").shuffle(ordered)
    game['rng_step'] += 1
    return ordered


def make_card(game, kind):
    game['card_serial'] += 1
    return {'id': f"card-{game['card_serial']}", 'kind': kind}


def draw(game, count):
    for _ in range(count):
        if not game['draw_pile']:
            game['draw_pile'] = shuffled(game, game['discard_pile'])
            game['discard_pile'] = []
        if not game['draw_pile']:
            break
        game['hand'].append(game['draw_pile'].pop())


def initial_card_game(seed=None):
    game = initial_game()
    game.update(ruleset='cards-v2', phase='round_active', seed=seed if seed is not None else secrets.randbits(63),
                rng_step=0, card_serial=0, energy=3, talk_limit=2, extra_used=False,
                hand=[], draw_pile=[], discard_pile=[], spent=[], reward_choices=[],
                preparation=None, preparation_effects={}, discoveries={}, offers=[],
                negotiation_context=None, effects_log=[], last_action=None)
    game['draw_pile'] = shuffled(game, [make_card(game, kind) for kind in BASIC for _ in range(2)])
    draw(game, 5)
    return game


def require_round(game):
    if game['phase'] != 'round_active' or game['status'] != 'active':
        raise GameError('현재는 카드를 사용하거나 거래할 수 없습니다.')


def proposal_for(preparation, price):
    merchant = preparation['merchant']
    return {'buyer': 'player' if preparation['direction'] == 'buy' else merchant,
            'seller': merchant if preparation['direction'] == 'buy' else 'player',
            'good': preparation['good'], 'quantity': preparation['quantity'], 'unit_price': price}


def quote(game, preparation=None, effects=None):
    preparation = preparation or game.get('preparation')
    if not preparation:
        raise GameError('먼저 거래 상대와 물자를 선택해주세요.')
    effects = game['preparation_effects'] if effects is None else effects
    if preparation['quantity'] > (4 if effects.get('bulk') else 2):
        raise GameError('기본 수량은 최대 2개입니다. 대량 계약을 먼저 사용해주세요.')
    merchant = preparation['merchant']
    prices = []
    for price in range(1, 101):
        try:
            validate_offer(game, proposal_for(preparation, price), merchant)
            prices.append(price)
        except GameError:
            continue
    if not prices:
        raise GameError('재고, 적재 공간 또는 잔액 때문에 가능한 거래가 없습니다.')
    margin = (1 if game['round'] >= 4 else 2) if merchant == 'a' else (3 if game['round'] == 5 else 4)
    advantage = min(2, effects.get('advantage', 0))
    if preparation['direction'] == 'buy':
        base = min(prices[-1], prices[0] + margin)
        low, high = prices[0], max(prices[0], base - advantage)
    else:
        base = max(prices[0], prices[-1] - margin)
        low, high = min(prices[-1], base + advantage), prices[-1]
    return {'base_price': base, 'min_price': low, 'max_price': high,
            'quantity': preparation['quantity'], 'unit_price': preparation.get('unit_price', base),
            'max_quantity': 4 if effects.get('bulk') else 2}


def prepare(game, merchant, direction, good, quantity, unit_price=None):
    require_round(game)
    previous = game.get('preparation')
    candidate = {'merchant': merchant, 'direction': direction, 'good': good,
                 'quantity': quantity}
    if previous and game['preparation_effects'] and any(previous[key] != candidate[key]
                                                        for key in ('merchant', 'direction', 'good')):
        raise GameError('적용한 효과가 있습니다. 준비를 취소한 뒤 대상을 바꿔주세요.')
    if unit_price is not None:
        candidate['unit_price'] = unit_price
    preview = quote(game, candidate)
    candidate.setdefault('unit_price', preview['base_price'])
    game['preparation'] = candidate


def available_deals(game):
    """상품 카드는 실재하는 수요/잉여와 체결 가능한 수량으로만 만듭니다."""
    deals = []
    for merchant in PERSONAS:
        actor = game['actors'][merchant]
        for direction in ('buy', 'sell'):
            for good in GOODS:
                gap = actor['inventory'][good] - actor['target'][good]
                available = gap if direction == 'buy' else min(-gap, game['actors']['player']['inventory'][good])
                if available <= 0:
                    continue
                target = {'merchant': merchant, 'direction': direction, 'good': good}
                previous = game.get('preparation')
                same_target = previous and all(previous[key] == value for key, value in target.items())
                effects = game['preparation_effects'] if same_target else {}
                maximum = 4 if effects.get('bulk') else 2
                player = game['actors']['player']
                if not effects.get('bulk'):
                    if direction == 'buy':
                        maximum = min(maximum, max(1, player['target'][good] - player['inventory'][good]))
                    elif player['inventory'][good] > player['target'][good]:
                        maximum = min(maximum, player['inventory'][good] - player['target'][good])
                for quantity in range(min(maximum, available), 0, -1):
                    candidate = {**target, 'quantity': quantity}
                    try:
                        preview = quote(game, candidate, effects)
                    except GameError:
                        continue
                    deals.append({**candidate, 'id': f'{merchant}:{direction}:{good}:{quantity}',
                                  'base_price': preview['base_price'],
                                  'total': preview['base_price'] * quantity,
                                  'uses_reserved_stock': direction == 'sell' and player['inventory'][good] - quantity < player['target'][good]})
                    break
    return deals


def select_deal(game, deal_id):
    require_round(game)
    deal = next((deal for deal in available_deals(game) if deal['id'] == deal_id), None)
    if not deal:
        raise GameError('이 상품의 조건이 바뀌었습니다. 현재 상품 카드에서 다시 선택해주세요.')
    previous = game.get('preparation')
    if previous and any(previous[key] != deal[key] for key in ('merchant', 'direction', 'good')):
        game['preparation_effects'] = {}
        game['preparation'] = None
    prepare(game, deal['merchant'], deal['direction'], deal['good'], deal['quantity'])


def expanded_quantity(game):
    preparation = game.get('preparation')
    if not preparation:
        return None
    actor = game['actors'][preparation['merchant']]
    good = preparation['good']
    availability = (actor['inventory'][good] - actor['target'][good] if preparation['direction'] == 'buy'
                    else min(actor['target'][good] - actor['inventory'][good], game['actors']['player']['inventory'][good]))
    for quantity in range(min(4, availability), preparation['quantity'], -1):
        try:
            preview = quote(game, {**preparation, 'quantity': quantity}, {**game['preparation_effects'], 'bulk': True})
            return quantity, preview['base_price']
        except GameError:
            continue
    return None


def card_error(game, card, discard_ids=None):
    kind = card['kind']
    if game['energy'] < CATALOG[kind][1]:
        return '협상력이 부족합니다.'
    preparation, effects = game.get('preparation'), game['preparation_effects']
    if kind in PREPARATION_CARDS | {'scan', 'analysis'} and not preparation:
        return '먼저 물자 카드를 골라주세요.'
    if kind == 'focus' and game['energy'] >= 4:
        return '협상력이 이미 최대입니다.'
    if kind == 'redraw' and len(game['hand']) <= 1:
        return '교환할 다른 카드가 없습니다.'
    if kind == 'extra' and game['extra_used']:
        return '이번 라운드에 이미 추가 교섭을 사용했습니다.'
    if kind in {'bulk', 'swift', 'standard'} and effects.get(kind):
        return '이미 적용한 효과입니다.'
    if kind == 'bulk' and expanded_quantity(game) is None:
        return '이 상품은 재고나 적재 공간 때문에 수량을 더 늘릴 수 없습니다.'
    if kind == 'standard' and effects.get('advantage'):
        return '가격 효과와 표준 계약은 함께 사용할 수 없습니다.'
    if kind in {'bargain', 'premium', 'breakthrough', 'history'}:
        if effects.get('standard'):
            return '표준 계약은 기준 가격으로 체결합니다.'
        if kind == 'bargain' and preparation['direction'] != 'buy':
            return '구매 협상에 사용하는 카드입니다.'
        if kind == 'premium' and preparation['direction'] != 'sell':
            return '판매 협상에 사용하는 카드입니다.'
        if kind == 'history' and not any(t['merchant'] == preparation['merchant'] for t in game['trades']):
            return '이 상인과 완료한 거래가 없습니다.'
        if effects.get('advantage', 0) >= 2:
            return '가격 효과는 최대 2입니다.'
        try:
            before = quote(game)
            after = quote(game, effects={**effects, 'advantage': min(2, effects.get('advantage', 0)
                                                                 + (2 if kind == 'breakthrough' else 1))})
        except GameError as exc:
            return exc.message
        if (before['min_price'], before['max_price']) == (after['min_price'], after['max_price']):
            return '상인의 거래 한계에 도달해 효과가 없습니다.'
    if kind in {'scan', 'analysis'}:
        known = game['discoveries'].get(preparation['merchant'], {}).get(preparation['good'], {})
        if ('shortage' if kind == 'analysis' else 'demand') in known:
            return '이미 확인한 정보입니다.'
    return None


def play_card(game, card_id, discard_ids=None):
    require_round(game)
    card = next((c for c in game['hand'] if c['id'] == card_id), None)
    if not card:
        raise GameError('손패에 없는 카드입니다.')
    reason = card_error(game, card, discard_ids)
    if reason:
        raise GameError(reason)
    kind, effects, preparation = card['kind'], game['preparation_effects'], game.get('preparation')
    selected = list(discard_ids or [])
    if kind == 'redraw' and (not 1 <= len(selected) <= 2 or len(set(selected)) != len(selected)
                            or any(cid == card_id or not any(c['id'] == cid for c in game['hand']) for cid in selected)):
        raise GameError('교환할 다른 카드 1~2장을 선택해주세요.')
    game['energy'] -= CATALOG[kind][1]
    game['hand'].remove(card)
    game['spent'].append(card)
    if kind == 'focus':
        game['energy'] = min(4, game['energy'] + 1)
    elif kind == 'redraw':
        for cid in selected:
            picked = next(c for c in game['hand'] if c['id'] == cid)
            game['hand'].remove(picked)
            game['discard_pile'].append(picked)
        draw(game, len(selected))
    elif kind == 'extra':
        game['talk_limit'] += 1
        game['extra_used'] = True
    elif kind in {'bargain', 'premium', 'breakthrough', 'history'}:
        effects['advantage'] = min(2, effects.get('advantage', 0) + (2 if kind == 'breakthrough' else 1))
        if kind == 'history':
            effects['history'] = True
    elif kind in {'scan', 'analysis'}:
        actor = game['actors'][preparation['merchant']]
        good = preparation['good']
        gap = actor['target'][good] - actor['inventory'][good]
        known = game['discoveries'].setdefault(preparation['merchant'], {}).setdefault(good, {})
        known['demand'] = '필요' if gap > 0 else ('여유' if gap < 0 else '목표 확보')
        if kind == 'analysis':
            known['shortage'] = max(0, gap)
    else:
        effects[kind] = True
    game['effects_log'].append({'round': game['round'], 'card': kind})


def submit(game, message):
    require_round(game)
    if game['talks'] >= game['talk_limit']:
        raise GameError('이번 라운드 협상 기회를 모두 사용했습니다.')
    effects = game['preparation_effects']
    cost = 0 if effects.get('swift') else 1
    if game['energy'] < cost:
        raise GameError('협상 제출에 필요한 협상력이 부족합니다.')
    preview = quote(game)
    preparation = copy.deepcopy(game['preparation'])
    proposal = proposal_for(preparation, preparation['unit_price'])
    standard = bool(effects.get('standard'))
    if not standard and game['model_calls'] >= 30:
        raise GameError('게임의 모델 호출 한도에 도달했습니다.')
    game['energy'] -= cost
    game['talks'] += 1
    game['negotiation_context'] = {'merchant': preparation['merchant'], 'proposal': proposal,
                                 'bounds': preview, 'effects': copy.deepcopy(effects), 'message': message}
    # 물자 선택은 유지하여 다음 카드도 바로 사용할 수 있게 하고, 일회성 효과만 소모합니다.
    game['preparation'] = preparation
    game['preparation_effects'] = {}
    if standard:
        offer = add_offer(game, {**proposal, 'unit_price': preview['base_price']}, preparation['merchant'], 'standard_rule')
        game['last_result'] = {'speech': '기준 조건으로 계약서를 준비했습니다.', 'fallback': False,
                               'merchant': preparation['merchant'], 'decision': 'offer', 'source': 'standard_rule'}
        return False, offer
    return True, None


def validate_card_decision(game, proposal):
    context = game.get('negotiation_context')
    if not context:
        raise GameError('협상 준비 정보가 없습니다.')
    original, bounds = context['proposal'], context['bounds']
    if any(proposal[key] != original[key] for key in ('buyer', 'seller', 'good')):
        raise GameError('거래 상대, 방향과 물자를 바꿀 수 없습니다.')
    if proposal['quantity'] != original['quantity']:
        raise GameError('준비한 수량을 유지해주세요. 다른 수량은 다음 협상에서 제안합니다.')
    if not bounds['min_price'] <= proposal['unit_price'] <= bounds['max_price']:
        raise GameError(f"카드 적용 가격 범위 {bounds['min_price']}~{bounds['max_price']}를 지켜주세요.")


def finish_round(game):
    require_round(game)
    for offer in game['offers']:
        if offer['status'] == 'active':
            offer['status'] = 'expired'
    game['discard_pile'].extend(game['hand'] + game['spent'])
    game['hand'], game['spent'] = [], []
    game['preparation'], game['preparation_effects'] = None, {}
    if game['round'] == 5:
        game['phase'], game['status'] = 'finished', 'finished'
    else:
        game['phase'] = 'reward'
        game['reward_choices'] = shuffled(game, REWARDS)[:3]


def choose_reward(game, reward):
    if game['phase'] != 'reward':
        raise GameError('지금은 카드 보상을 선택할 수 없습니다.')
    if reward is not None:
        if reward not in game['reward_choices']:
            raise GameError('제시된 보상 중에서 선택해주세요.')
        game['discard_pile'].append(make_card(game, reward))
    game['round'] += 1
    game.update(phase='round_active', energy=3, talks=0, talk_limit=2, trades_this_round=0,
                extra_used=False, reward_choices=[], last_result=None, negotiation_context=None)
    draw(game, 5)


def card_public_view(game):
    view = public_view(game)
    view.update({key: copy.deepcopy(game[key]) for key in ('ruleset', 'phase', 'energy', 'talk_limit',
                 'preparation', 'preparation_effects', 'discoveries', 'reward_choices', 'last_action')})
    view['catalog'] = {kind: {'name': name, 'cost': cost, 'description': description,
                            'family': family, 'art': index} for index, (kind, (name, cost, description, family)) in enumerate(CATALOG.items())}
    view['hand'] = [{**card, 'disabled_reason': card_error(game, card)} for card in game['hand']]
    view['piles'] = {'draw': len(game['draw_pile']), 'discard': len(game['discard_pile']),
                     'spent': len(game['spent']), 'total': sum(len(game[key]) for key in ('hand', 'draw_pile', 'discard_pile', 'spent'))}
    view['deck'] = copy.deepcopy(game['hand'] + game['draw_pile'] + game['discard_pile'] + game['spent'])
    # 덱 구성은 공개하되 정렬하여 다음에 뽑을 카드 순서를 숨깁니다.
    view['deck'].sort(key=lambda card: (card['kind'], card['id']))
    view['merchant_signals'] = {}
    for merchant in PERSONAS:
        actor = game['actors'][merchant]
        good = max(GOODS, key=lambda g: actor['target'][g] - actor['inventory'][g])
        view['merchant_signals'][merchant] = f"{GOODS[good]} 확보에 관심" if actor['target'][good] > actor['inventory'][good] else '납품 목표 확보'
    view['quote'], view['quote_error'] = None, None
    if game.get('preparation'):
        try:
            view['quote'] = quote(game)
        except GameError as exc:
            view['quote_error'] = exc.message
    view['deals'] = available_deals(game) if game['phase'] == 'round_active' else []
    return view
