"""상인별 관측 → 판단 → 검증 → 최대 한 번 수정하는 LangGraph 협상 흐름."""
import asyncio
import re
import time
from dataclasses import dataclass
from typing import TypedDict
from langgraph.graph import StateGraph, END
from langsmith import traceable

from .domain import GOODS, GameError, validate_offer
from .cards import validate_card_decision
from .schemas import Decision


@dataclass
class NegotiationDeps:
    store: object
    tools: object
    model: object


class NegotiationState(TypedDict, total=False):
    observations: dict
    raw: str
    decision: Decision | None
    error: dict | None
    attempts: int
    metrics: dict
    unavailable: bool


def check_decision(raw, snapshot, merchant, observations, request_proposal=None):
    decision = Decision.model_validate_json(raw)
    if len([s for s in re.split(r'[.!?。！？]+', decision.speech) if s.strip()]) > 2:
        raise GameError('대사는 두 문장 이내여야 합니다.')
    if decision.offer:
        validate_offer(snapshot, decision.offer.model_dump(), merchant)
        if snapshot.get('ruleset') == 'cards-v2':
            validate_card_decision(snapshot, decision.offer.model_dump())
        if request_proposal and any(getattr(decision.offer, key) != request_proposal[key]
                                    for key in ('buyer', 'seller', 'good')):
            raise GameError('역제안에서도 입력 proposal의 구매자, 판매자와 물자는 유지해야 합니다.')
    if request_proposal and decision.decision == 'clarify':
        try:
            validate_offer(snapshot, request_proposal, merchant)
        except GameError:
            pass
        else:
            raise GameError('구체적이고 유효한 proposal이 있습니다. 추가 확인 대신 offer 또는 reject로 판단하세요.')
    if decision.memory_trade_id:
        trade = next((t for t in observations['memory']['trades']
                      if t['trade_id'] == decision.memory_trade_id), None)
        if not trade:
            raise GameError('기억에 없는 거래를 인용했습니다.')
        decision.speech = f"지난 거래는 {GOODS[trade['good']]} {trade['quantity']}개, 단가 {trade['unit_price']}크레딧이었습니다."
    elif re.search(r'\d', decision.speech):
        # 확정되지 않은 수치가 자유 대사를 통해 거래 조건처럼 보이지 않게 합니다.
        decision.speech = {
            'offer': '조건을 검토했습니다. 제안 카드의 내용을 확인해주세요.',
            'clarify': '거래할 물자, 수량과 단가를 정확한 조건 입력란에 적어주세요.',
            'reject': '현재 조건으로는 거래하기 어렵습니다. 물자나 가격을 바꿔 제안해주세요.',
        }[decision.decision]
        if snapshot.get('ruleset') == 'cards-v2':
            decision.speech = {
                'a': {'offer': '좋아요. 서로 필요한 걸 챙기는 거래가 되겠군요.',
                      'reject': '지금은 이 물자를 더 아껴야겠어요. 다른 조건도 살펴볼까요?',
                      'clarify': '어떤 물자를 거래할지 먼저 정해볼까요?'},
                'b': {'offer': '그 정도라면 얘기가 되네요. 계약서를 확인하죠.',
                      'reject': '이 조건은 어렵겠네요. 다음 제안을 기다리죠.',
                      'clarify': '거래할 물자와 조건부터 정해주시죠.'},
            }[merchant][decision.decision]
    return decision


def build_negotiation(deps, game_id, action, snapshot):
    def progress(stage):
        deps.store.progress(game_id, action.action_id, stage)

    async def observe(state):
        progress('기억 조회 중')
        try:
            tokens = deps.store.issue_observations(game_id, action.merchant)
            observations = await asyncio.wait_for(deps.tools.observe(tokens), timeout=5)
            return {'observations': observations, 'attempts': 0, 'error': None,
                    'unavailable': False, 'metrics': {'calls': 0, 'tokens': 0, 'validation_failures': 0}}
        except Exception:
            return {'unavailable': True, 'decision': None, 'attempts': 0,
                    'metrics': {'calls': 0, 'tokens': 0, 'validation_failures': 0,
                                'failure': 'observation_failed'}}

    async def decide(state):
        if state.get('unavailable'):
            return {'decision': None}
        progress('협상 중' if state['attempts'] == 0 else '조건 수정 중')
        if not deps.store.reserve_model_call(game_id, action.action_id):
            return {'unavailable': True, 'decision': None}
        metrics = dict(state['metrics'])
        metrics['calls'] += 1
        timeout = 20 if state['attempts'] == 0 else 10
        try:
            raw, usage = await asyncio.wait_for(deps.model.decide(action.merchant, state['observations'],
                {'message': action.message, 'proposal': action.proposal.model_dump() if action.proposal else None},
                state['error'], timeout=timeout), timeout=timeout)
            metrics['tokens'] += usage.get('tokens', 0)
            return {'raw': raw, 'attempts': state['attempts']+1, 'metrics': metrics}
        except Exception as exc:
            metrics['failure'] = type(exc).__name__
            return {'raw': '', 'attempts': state['attempts']+1, 'metrics': metrics}

    def validate(state):
        progress('조건 검증 중')
        if state.get('unavailable'):
            return {'decision': None}
        try:
            decision = check_decision(state['raw'], snapshot, action.merchant, state['observations'],
                                      action.proposal.model_dump() if action.proposal else None)
            return {'decision': decision, 'error': None}
        except Exception as exc:
            metrics = dict(state['metrics'])
            metrics['validation_failures'] += 1
            error = exc.message if isinstance(exc, GameError) else '응답 JSON 형식과 필수 항목을 확인하세요.'
            return {'decision': None, 'error': {'reason': error, 'previous_output': state['raw'][:1000]},
                    'metrics': metrics}

    graph = StateGraph(NegotiationState)
    graph.add_node('observe', observe)
    graph.add_node('decide', decide)
    graph.add_node('validate', validate)
    graph.set_entry_point('observe')
    graph.add_edge('observe', 'decide')
    graph.add_edge('decide', 'validate')
    graph.add_conditional_edges('validate', lambda s: 'retry' if s.get('error') and s['attempts'] < 2
                               and not s.get('unavailable') else 'done', {'retry': 'decide', 'done': END})
    return graph.compile()


@traceable(name='cargo_negotiation')
async def run_negotiation(deps, game_id, action):
    started = time.perf_counter()
    metrics = {'calls': 0, 'tokens': 0}
    decision = None
    try:
        snapshot = deps.store.state(game_id)
        result = await build_negotiation(deps, game_id, action, snapshot).ainvoke({},
            config={'recursion_limit': 9, 'metadata': {'game_id': game_id, 'merchant': action.merchant,
                                                     'round': snapshot['round']}})
        decision, metrics = result.get('decision'), result['metrics']
    except asyncio.CancelledError:
        metrics['failure'] = 'interrupted'
        raise
    except Exception as exc:
        metrics['failure'] = type(exc).__name__
    finally:
        metrics['elapsed_seconds'] = time.perf_counter()-started
        deps.store.finish(game_id, action, decision, metrics)
