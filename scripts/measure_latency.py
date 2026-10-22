"""라스트 카고 협상의 단일 호출(a)과 Agent(b) 응답 시간만 측정한다. 게임 로직은 import만 하고 수정하지 않는다.

작성자: Git 이력 참조
작성일: 2026-10-22
실행: 프로젝트 루트에서 .venv/bin/python -m scripts.measure_latency --runs 3 (Ollama 실행 필요)
주의:
- 시나리오와 측정 구간은 cargo_game.cards_evaluate.evaluate()를 그대로 사용하고, 20건 중 앞 N건만 남긴다.
  (a) 시간 = 모델 1회 호출 + 서버 검증(check_decision). 관측값 준비는 측정 구간 밖이다.
  (b) 시간 = run_negotiation 전체(MCP 기억 조회 + 호출 + 검증 + 최대 1회 수정).
- 서버 검증 통과: (a)는 check_decision 통과, (b)는 fallback 없이 최종 결정이 확정된 경우다.
- 각 실행마다 (a)를 먼저, (b)를 나중에 같은 상태로 실행하므로 Ollama 프롬프트 캐시가 (b)에 유리할 수 있다.
- 모델 다운로드와 유료 API 호출을 하지 않고, LangSmith 추적은 끈다.
"""
import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
from pathlib import Path

# 측정 중 실행 기록이 외부 추적 서비스로 전송되지 않게 import 전에 끈다.
os.environ['LANGSMITH_TRACING'] = 'false'
os.environ['LANGCHAIN_TRACING_V2'] = 'false'

import httpx
from cargo_game import cards_evaluate
from cargo_game.models import OllamaModel

OLLAMA_URL = 'http://127.0.0.1:11434'  # cargo_game.models.OllamaModel의 기본 주소와 같아야 한다.


class ErrorRecordingModel(OllamaModel):
    """evaluate()가 예외를 타입명만 남기므로 모델 호출 예외 원문을 따로 기록한다."""

    errors = []

    async def decide(self, merchant, observations, request, error=None, timeout=20):
        try:
            return await super().decide(merchant, observations, request, error, timeout)
        except BaseException as exc:
            ErrorRecordingModel.errors.append(f'{type(exc).__name__}: {exc}')
            raise


def hardware_info():
    def read(command):
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            return f'확인 실패: {type(exc).__name__}'
    profile = read(['system_profiler', 'SPHardwareDataType', 'SPDisplaysDataType'])
    wanted = ('Model Name', 'Chip', 'Total Number of Cores', 'Memory', 'Chipset Model', 'Metal Support')
    return {'cpu': read(['sysctl', '-n', 'machdep.cpu.brand_string']),
            'os': f'macOS {platform.mac_ver()[0]}',
            'profile': [line.strip() for line in profile.splitlines() if line.strip().startswith(wanted)]}


async def warm_up(model_name):
    """첫 시나리오에 모델 적재 시간이 섞이지 않도록 측정 전에 모델을 메모리에 올린다."""
    async with httpx.AsyncClient(base_url=OLLAMA_URL) as client:
        version = (await client.get('/api/version', timeout=5)).json()['version']
        response = await client.post('/api/generate', json={'model': model_name, 'prompt': '', 'keep_alive': '10m'}, timeout=120)
        response.raise_for_status()
        return version, response.json().get('load_duration', 0)/1e9


def collect(reports):
    samples = {}
    for run_index, report in enumerate(reports, start=1):
        for row in report['rows']:
            entry = samples.setdefault(row['case']['id'], {'a': [], 'b': []})
            entry['a'].append({'run': run_index, 'seconds': row['baseline']['seconds'], 'passed': row['baseline']['valid'],
                               'error': row['baseline']['error']})
            entry['b'].append({'run': run_index, 'seconds': row['agent']['seconds'], 'passed': row['agent']['valid'],
                               'calls': row['agent']['calls'], 'validation_failures': row['agent']['validation_failures']})
    return samples


def render_table(samples, runs):
    def cell(entries):
        values = ' / '.join(f"{e['seconds']:.2f}" for e in entries)
        return f"{statistics.mean(e['seconds'] for e in entries):.2f} ({values})" if runs > 1 else values
    lines = ['| 시나리오 | (a) 단일 호출 [초] | (b) Agent [초] | (b) 모델 호출 수 |', '|---|---:|---:|---:|']
    for case_id, entry in samples.items():
        lines.append(f"| {case_id} | {cell(entry['a'])} | {cell(entry['b'])} | {' / '.join(str(e['calls']) for e in entry['b'])} |")
    for mode in ('a', 'b'):
        seconds = [e['seconds'] for entry in samples.values() for e in entry[mode]]
        passed = sum(e['passed'] for entry in samples.values() for e in entry[mode])
        lines.append(f"\n({mode}) 평균 {statistics.mean(seconds):.2f}초, 중앙값 {statistics.median(seconds):.2f}초, "
                     f"서버 검증 통과 {passed}/{len(seconds)}건")
    return '\n'.join(lines)


async def measure(model_name, case_count, runs):
    all_cases = cards_evaluate.cases()
    # evaluate()는 모듈 전역 cases()와 OllamaModel을 호출 시점에 찾으므로, 파일 수정 없이 범위와 오류 기록만 바꾼다.
    cards_evaluate.cases = lambda: all_cases[:case_count]
    cards_evaluate.OllamaModel = ErrorRecordingModel
    ollama_version, load_seconds = await warm_up(model_name)
    reports = []
    for _ in range(runs):
        reports.append(await cards_evaluate.evaluate(model_name))
    return {'model': model_name, 'ollama_version': ollama_version, 'warm_up_load_seconds': load_seconds,
            'runs': runs, 'cases': [c['id'] for c in all_cases[:case_count]], 'hardware': hardware_info(),
            'method': reports[0]['method'], 'source_sha256': reports[0]['source_sha256'],
            'model_errors': ErrorRecordingModel.errors, 'samples': collect(reports)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='qwen3.5:2b')
    parser.add_argument('--cases', type=int, default=5)
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--output', default='eval/results/last_cargo_latency.json')
    args = parser.parse_args()
    report = asyncio.run(measure(args.model, args.cases, args.runs))
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(render_table(report['samples'], args.runs))
    print(f"\n모델 {report['model']}, Ollama {report['ollama_version']}, 실행 {args.runs}회, "
          f"워밍업 적재 {report['warm_up_load_seconds']:.2f}초(측정 제외)")
    print('\n'.join([report['hardware']['cpu'], report['hardware']['os'], *report['hardware']['profile']]))
    print('모델 호출 오류:', '\n'.join(report['model_errors']) if report['model_errors'] else '없음')


if __name__ == '__main__':
    main()
