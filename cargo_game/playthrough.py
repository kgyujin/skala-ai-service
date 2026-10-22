"""실제 로컬 서버에서 카드 협상, 체결과 5라운드 종료를 재현합니다.

게임 접근 토큰은 메모리에서만 사용하고 결과 파일이나 로그에 쓰지 않습니다.
"""
import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path
import httpx


async def play(url, output):
    async with httpx.AsyncClient(base_url=url, timeout=40) as client:
        created = (await client.post('/api/games')).json()
        gid = created['game_id'];base = '/api/games/'+gid
        client.headers['Authorization'] = 'Bearer '+created['access_token']
        records = []
        async def state():
            response = await client.get(base);response.raise_for_status()
            return response.json()['state']
        async def action(kind, **fields):
            before = await state()
            request = {'action_id': uuid.uuid4().hex, 'expected_version': before['version'], 'kind': kind, **fields}
            started = time.perf_counter()
            response = await client.post(base+'/actions', json=request);response.raise_for_status()
            deadline = time.monotonic()+36
            while True:
                current = await state()
                if not current['busy']: break
                if time.monotonic() > deadline: raise RuntimeError('Negotiation did not finish')
                await asyncio.sleep(.1)
            records.append({'kind': kind, 'input': fields, 'round': before['round'],
                            'seconds': time.perf_counter()-started, 'result': current['last_result'],
                            'trades': len(current['trades']), 'inventory': current['player']['inventory']})
            return before, current
        async def negotiate(merchant, proposal):
            before, current = await action('negotiate', merchant=merchant,
                message='현재 제안 카드 조건을 검토해주세요. 가능하면 같은 거래 방향으로 제안해주세요.', proposal=proposal)
            old_ids = {o['id'] for o in before['offers']}
            offer = next((o for o in current['offers'] if o['merchant'] == merchant
                          and o['source'] == 'agent' and o['id'] not in old_ids and o['acceptable']), None)
            if offer: await action('accept', offer_id=offer['id'])
            print('round', current['round'], merchant, 'accepted', bool(offer), flush=True)
        def proposal(merchant, buy, good, quantity, price):
            return {'buyer': 'player' if buy else merchant, 'seller': merchant if buy else 'player',
                    'good': good, 'quantity': quantity, 'unit_price': price}
        for round_number in range(1, 6):
            if round_number == 1:
                await negotiate('a', proposal('a', False, 'fuel', 2, 10))
                await negotiate('a', proposal('a', True, 'medicine', 2, 15))
            elif round_number == 2:
                await negotiate('a', proposal('a', False, 'fuel', 1, 10))
                await negotiate('b', proposal('b', True, 'parts', 1, 15))
            elif round_number == 3:
                await negotiate('a', proposal('a', True, 'medicine', 1, 15))
            elif round_number == 4:
                for merchant in ['a', 'b']:
                    await action('negotiate', merchant=merchant, message='우리의 가장 최근 거래 물자와 단가를 알려주세요.')
            await action('end_round')
        final = await state()
        assert final['status'] == 'finished'
        assert final['trades'], 'No actual AI-generated trade was accepted'
        event_response = await client.get(base+'/events?once=true')
        event_response.raise_for_status()
        assert 'ActionCompleted' in event_response.text
        result = {'backend': 'actual_ollama_http_mcp', 'complete': True, 'records': records,
                  'final': final, 'sse_completed': True}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print('PASS actual game finished', len(final['trades']), 'trades; gain', final['player']['gain'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8200')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(play(args.url, args.output))
