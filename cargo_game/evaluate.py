"""고정 협상 질의의 단일 호출/상인 Agent 비교. 실제 모델과 대역 결과를 분리합니다.

작성자: Git 이력 참조
작성일: 2026-10-21
실행: python -m cargo_game.evaluate --split evaluation --output result/task-2/evaluation/comparison.json
주의: 모델 다운로드와 과금 호출을 수행하지 않습니다. 모델 판단을 게임에 자동 체결하지 않습니다.
"""
import argparse
import asyncio
import copy
import hashlib
import json
import os
import statistics
import tempfile
import time
from pathlib import Path

# 비교 실행은 로컬로 완료되며 .env의 추적 설정을 읽지 않습니다.
os.environ['LANGSMITH_TRACING'] = 'false'
os.environ['LANGCHAIN_TRACING_V2'] = 'false'

from .domain import RULES, accept_offer, add_offer
from .models import OllamaModel
from .negotiation import NegotiationDeps, check_decision, run_negotiation
from .schemas import Action
from .store import GameStore
from .tools import CargoTools

CASES = Path(__file__).with_name('evaluation_cases.json')


def seed_case(store, case):
    created = store.create();gid = created['game_id']
    game = store.state(gid)
    if case['memory']:
        offer = add_offer(game, {'buyer': 'player', 'seller': case['merchant'],
            'good': 'medicine' if case['merchant'] == 'a' else 'parts', 'quantity': 1, 'unit_price': 15}, case['merchant'], 'fixture')
        accept_offer(game, offer['id'])
        game['trades'][-1]['trade_id'] = 'fixture-trade-001'
    for index, offer in enumerate(game['offers']):
        offer['id'] = f'fixture-offer-{index}'
    for dialogue in game['dialogues']:
        dialogue.pop('action_id', None)
    store._save(gid, game)
    return gid


def is_valid(raw, snapshot, merchant, observations, proposal=None):
    try:
        check_decision(raw, snapshot, merchant, observations, proposal)
        return True
    except Exception:
        return False


async def evaluate(split, output, model_name):
    cases = [c for c in json.loads(CASES.read_text()) if c['split'] == split]
    model = OllamaModel(model_name)
    availability = await model.available()
    if not availability['ready']:
        await model.close()
        raise SystemExit('선택한 Ollama 모델을 먼저 실행 가능한 상태로 준비해주세요.')
    rows = []
    with tempfile.TemporaryDirectory() as directory:
        store = GameStore(Path(directory)/'evaluation.db');tools = CargoTools(store.path)
        await tools.start()
        try:
            for case in cases:
                gid = seed_case(store, case)
                snapshot = store.state(gid)
                # 첫 호출과 수정 호출을 실제로 기록하되 프롬프트나 접근 토큰은 저장하지 않습니다.
                recordings = []
                class RecordingModel:
                    async def decide(self, merchant, observations, request, error=None, timeout=20):
                        raw, usage = await model.decide(merchant, observations, request, error, timeout)
                        recordings.append({'raw': raw, 'usage': usage, 'observations': copy.deepcopy(observations)})
                        return raw, usage
                tokens = store.issue_observations(gid, case['merchant'])
                full = await tools.observe(tokens)
                baseline_obs = {**full, 'memory': {'trades': [], 'dialogues': []}}
                baseline_started = time.perf_counter()
                try:
                    raw, usage = await model.decide(case['merchant'], baseline_obs,
                        {'message': case['message'], 'proposal': case['proposal']})
                    baseline = {'raw': raw, 'tokens': usage['tokens'],
                                'valid': is_valid(raw, snapshot, case['merchant'], baseline_obs, case['proposal']), 'calls': 1}
                    parsed = json.loads(raw)
                    baseline['expected_decision'] = parsed.get('decision') in case['expected_decisions']
                    baseline['memory_correct'] = parsed.get('memory_trade_id') == 'fixture-trade-001' if case['memory'] else None
                except Exception as exc:
                    baseline = {'valid': False, 'calls': 1, 'tokens': 0, 'failure': type(exc).__name__,
                                'expected_decision': False, 'memory_correct': False if case['memory'] else None}
                baseline['seconds'] = time.perf_counter()-baseline_started
                action = Action(action_id='eval-'+case['id'], expected_version=snapshot['version'],
                                kind='negotiate', merchant=case['merchant'], message=case['message'], proposal=case['proposal'])
                store.begin(gid, action)
                await run_negotiation(NegotiationDeps(store, tools, RecordingModel()), gid, action)
                last = store.state(gid)['last_result']
                final_raw = recordings[-1]['raw'] if recordings else ''
                try: parsed = json.loads(final_raw)
                except ValueError: parsed = {}
                after = {'initial_valid': bool(recordings) and is_valid(recordings[0]['raw'], snapshot,
                            case['merchant'], recordings[0]['observations'], case['proposal']),
                         'final_valid_proposal': not last['fallback'] and parsed.get('decision') == 'offer',
                         'final_valid_response': not last['fallback'], 'fallback': last['fallback'],
                         'expected_decision': not last['fallback'] and parsed.get('decision') in case['expected_decisions'],
                         'memory_correct': (not last['fallback'] and parsed.get('memory_trade_id') == 'fixture-trade-001') if case['memory'] else None,
                         'raw': final_raw, **last['metrics'], 'seconds': last['metrics']['elapsed_seconds']}
                rows.append({'id': case['id'], 'category': case['category'], 'reason': case['reason'],
                             'baseline': baseline, 'agent': after})
                print(case['id'], 'baseline', baseline['valid'], 'agent', not after['fallback'], flush=True)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({'status': 'running', 'cases': rows}, ensure_ascii=False, indent=2))
        finally:
            await tools.close();store.close();await model.close()
    summary = {}
    for name in ('baseline', 'agent'):
        entries = [r[name] for r in rows]
        summary[name] = {'cases': len(entries), 'first_response_valid': sum(e['valid' if name == 'baseline' else 'initial_valid'] for e in entries),
            'expected_decision_correct': sum(e['expected_decision'] for e in entries),
            'memory_cases': sum(e['memory_correct'] is not None for e in entries),
            'memory_correct': sum(e['memory_correct'] is True for e in entries),
            'seconds_median': statistics.median(e['seconds'] for e in entries),
            'model_calls': sum(e['calls'] for e in entries), 'tokens': sum(e['tokens'] for e in entries)}
    summary['agent'].update(final_response_valid=sum(r['agent']['final_valid_response'] for r in rows),
                             final_valid_proposals=sum(r['agent']['final_valid_proposal'] for r in rows),
                             fallbacks=sum(r['agent']['fallback'] for r in rows))
    result = {'status': 'complete', 'model': model_name, 'backend': 'actual_ollama', 'split': split,
              'dataset_sha256': hashlib.sha256(CASES.read_bytes()).hexdigest(),
              'context': 'single call with rules/current observation vs MCP memory + validation + one repair; combined configuration comparison',
              'source_sha256': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                for name in ['models.py', 'negotiation.py', 'domain.py']},
              'evaluation_note': 'Authored fixed scenarios. Evaluation cases were inspected during development; this is a repeatable comparison, not a held-out benchmark.',
              'summary': summary, 'cases': rows}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split', choices=['development', 'evaluation'], default='evaluation')
    parser.add_argument('--model', default=os.getenv('CARGO_MODEL', 'qwen3.5:2b'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(evaluate(args.split, args.output, args.model))
