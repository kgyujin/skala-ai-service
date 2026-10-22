"""실제 HTTP, Ollama, MCP로 카드 버전 한 판을 실행하고 공개 결과만 저장합니다."""
import argparse
import json
import time
import uuid
from pathlib import Path

import httpx


def play(base_url):
    route = [('a','sell','fuel',2),('a','buy','medicine',2),('a','sell','fuel',1),
             ('a','buy','medicine',1),('b','buy','parts',1)]
    started = time.perf_counter()
    with httpx.Client(base_url=base_url, timeout=45) as client:
        response=client.post('/api/games');response.raise_for_status();created=response.json()
        client.headers['Authorization']='Bearer '+created['access_token']
        base='/api/games/'+created['game_id']
        def state():
            response=client.get(base);response.raise_for_status();return response.json()['state']
        def act(kind,**fields):
            response=client.post(base+'/actions',json={'kind':kind,'action_id':uuid.uuid4().hex,
                'expected_version':state()['version'],**fields});response.raise_for_status()
            deadline=time.monotonic()+40
            while state()['busy']:
                if time.monotonic()>deadline:raise TimeoutError('협상이 완료되지 않았습니다.')
                time.sleep(.15)
            return state()
        completed,attempts,card_uses=0,[],[]
        while state()['status']=='active':
            s=state()
            if s['phase']=='reward':
                # 시연 재현을 위해 후보의 첫 카드를 선택합니다. 숨은 재고를 사용하지 않습니다.
                act('choose_reward',reward=s['reward_choices'][0]);continue
            if completed>=len(route) or s['talks']>=s['talk_limit'] or s['trades_this_round']>=2 or s['energy']<1:
                act('end_round');continue
            merchant,direction,good,quantity=route[completed]
            deal=next(d for d in state()['deals'] if d['merchant']==merchant and d['direction']==direction
                      and d['good']==good and d['quantity']==quantity)
            act('select_deal',deal_id=deal['id'])
            s=state()
            desired='bargain' if direction=='buy' else 'premium'
            card=next((c for c in s['hand'] if c['kind']==desired and not c['disabled_reason']),None)
            if card and s['energy']>=2:
                act('play_card',card_id=card['id']);card_uses.append(card['kind'])
            s=act('negotiate',message='준비한 거래와 카드 조건을 기준으로 제안해주세요.')
            attempts.append({'round':s['round'],'merchant':merchant,'result':s['last_result']})
            offer=next((o for o in s['offers'] if o['merchant']==merchant and o['acceptable']),None)
            if offer:
                act('accept',offer_id=offer['id']);completed+=1
        final=state()
        return {'backend':'actual_ollama_http_mcp','ruleset':'cards-v2',
                'elapsed_seconds':round(time.perf_counter()-started,3),'attempts':attempts,
                'cards_used':card_uses,'all_targets_met':all(final['player']['inventory'][g]>=n for g,n in final['player']['target'].items()),
                'state':final}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--url',default='http://127.0.0.1:8200')
    parser.add_argument('--output',default='result/task-2/evaluation/cards-playthrough.json')
    args=parser.parse_args();report=play(args.url);path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'completed':report['state']['status'],'trades':len(report['state']['trades']),
                      'targets_met':report['all_targets_met'],'elapsed_seconds':report['elapsed_seconds']},ensure_ascii=False))


if __name__=='__main__':main()
