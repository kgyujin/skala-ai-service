// 상품 카드와 협상 카드로만 플레이합니다. 조건 입력이나 거래 준비 화면을 두지 않습니다.
import * as API from './api.js';
import * as FX from './effects.js';
import * as Sound from './sound.js';
const $=selector=>document.querySelector(selector);
const esc=text=>String(text??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names={a:'이안',b:'베라'},goods={fuel:'연료',food:'식량',medicine:'의약품',parts:'부품'};
const artReady=Promise.allSettled(['shop-background.png','shop-counter.png','expressions.png','card-art.png'].map(name=>{const image=new Image();image.src='/assets/'+name;return image.decode()}));
let state=null,working=false,starting=false,selectedDeal=null,stage='',streamController;
let noticeTimer,departureTimer,pendingFlight=null,shownPhase='',exchangeCard=null,exchangeIds=new Set();
const browsing=new Set();
function notify(text){$('#notice').textContent=text;$('#notice').classList.add('visible');clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>$('#notice').classList.remove('visible'),4000)}
function modal(selector){const dialog=$(selector);if(!dialog.open)dialog.showModal();return dialog}
function closeAll(){document.querySelectorAll('dialog[open]').forEach(dialog=>dialog.close())}
function currentDeal(){return state?.deals.find(deal=>deal.id===selectedDeal)}
function cardReason(card){if(card.disabled_reason==='먼저 물자 카드를 골라주세요.'&&currentDeal())return null;return card.disabled_reason}
function cardMarkup(card,reward=false){
 const def=state.catalog[card.kind],position=`${(def.art%4)*100/3}% ${Math.floor(def.art/4)*50}%`;
 const reason=reward?null:cardReason(card);
 return `<button class="tactic-card ${reason?'unavailable':''} ${exchangeIds.has(card.id)?'exchange-selected':''}" data-card="${esc(card.id)}" aria-label="${esc(def.name)}. 비용 ${def.cost}. ${esc(def.description)}${reason?' '+esc(reason):''}"><span class="cost">${def.cost}</span><span class="card-art" style="background-position:${position}"></span><span class="card-family">${esc(def.family)}</span><span class="card-name">${esc(def.name)}</span><span class="card-copy">${esc(def.description)}</span></button>`;
}
function goodsCard(deal){
 const selected=selectedDeal===deal.id;
 return `<button class="goods-card ${selected?'selected':''}" data-deal="${deal.id}" aria-pressed="${selected}" aria-label="${names[deal.merchant]} ${deal.direction==='buy'?'입고':'판매'} ${goods[deal.good]} ${deal.quantity}개 ${deal.total}크레딧" ${state.busy||working||exchangeCard?'disabled':''}><span class="deal-direction">${deal.direction==='buy'?'상점에 입고':'상점에서 판매'}</span><span class="deal-quantity">${deal.quantity}개</span><span class="deal-title"><i class="goods-icon ${deal.good}" aria-hidden="true"></i>${goods[deal.good]}</span><span class="deal-price">${deal.total}<small> CR</small></span><span class="deal-hint">${deal.uses_reserved_stock?'보급 물자 사용':selected?'선택한 물자':'클릭해서 선택'}</span></button>`;
}
function offerCard(offer){
 return `<div class="offer-card" id="offer-${offer.merchant}"><span class="offer-label">${offer.source==='standard_rule'?'기준 계약':'상인의 역제안'}</span><div class="offer-main"><span>${goods[offer.good]} ${offer.quantity}개 ${offer.buyer==='player'?'입고':'판매'}</span><strong>${offer.total}<small> CR</small></strong></div><p class="sub">단가 ${offer.unit_price} CR</p><button class="primary" data-accept="${offer.id}" ${!offer.acceptable||working||exchangeCard?'disabled':''}>${offer.buyer==='player'?`${offer.total} CR 내고 입고`:`${offer.total} CR 받고 판매`}</button></div>`;
}
function renderMerchants(){
 const selected=currentDeal();
 $('#merchants').innerHTML=Object.entries(names).map(([id,name])=>{
  const offer=state.offers.find(o=>o.merchant===id),showOffer=offer&&!browsing.has(id);
  const last=[...state.dialogues].reverse().find(d=>d.merchant===id&&d.role==='assistant');
  const busy=state.busy&&selected?.merchant===id;
  const expression=busy?'thinking':state.last_result?.merchant===id?(state.last_result.decision==='offer'?'agree':state.last_result.decision==='reject'||state.last_result.fallback?'refuse':'neutral'):'neutral';
  const greeting=id==='a'?'마을에서 필요한 것들을 모아왔어요. 서로 필요한 물건을 바꿔볼까요?':'쓸 만한 부품이 좀 있어요. 필요한 물건이 있으면 골라봐요.';
  const speech=busy?stage||'잠깐만요. 물건과 장부를 살펴볼게요.':last?.text||greeting;
  const choices=state.deals.filter(deal=>deal.merchant===id);
  const chosen=selected?.merchant===id;
  const discoveries=Object.entries(state.discoveries[id]||{}).map(([good,known])=>`${goods[good]} ${known.demand}${known.shortage!==undefined?` / ${known.shortage}개 부족`:''}`).join(' / ');
  const cannotTalk=state.busy||working||exchangeCard||!chosen||state.talks>=state.talk_limit||state.energy<(state.preparation_effects.swift?0:1)||state.phase!=='round_active';
  return `<article class="merchant ${chosen?'selected':''} ${busy?'thinking':''}" id="merchant-${id}"><div class="merchant-speech"><div class="speech-heading"><b>${name}</b><span>${id==='a'?'마을 보급 담당':'유랑 수리공'}</span></div><p aria-live="polite">${esc(speech)}</p>${discoveries?`<div class="merchant-discovery">${esc(discoveries)}</div>`:''}</div><div class="merchant-figure"><div class="portrait ${id}" data-expression="${expression}" style="--row:${id==='a'?'0%':'100%'}" aria-hidden="true"></div></div><div class="deal-list">${showOffer?offerCard(offer):choices.map(goodsCard).join('')||'<p class="empty-deals">지금 교환할 수 있는 물자가 없어요.</p>'}</div><div class="visitor-actions">${showOffer?`<button class="text-button" data-browse="${id}">다른 물건도 보기</button>`:chosen?`<button class="primary" data-negotiate="${id}" ${cannotTalk?'disabled':''}>${busy?'흥정하는 중':state.preparation_effects.standard?'기준 계약 받기':'이 물자로 흥정'}<small>협상력 ${state.preparation_effects.swift?0:1}</small></button>${offer?`<button class="text-button" data-offer="${id}">이전 제안</button>`:''}`:`<span class="visitor-hint">${state.busy?'상인이 생각하고 있습니다.':'물자 카드를 골라주세요.'}</span>`}</div></article>`;
 }).join('');
}
function render(previous=null){
 $('#start-screen').hidden=true;$('#board').hidden=false;
 if(state.preparation){const p=state.preparation;selectedDeal=state.deals.find(d=>d.merchant===p.merchant&&d.direction===p.direction&&d.good===p.good&&d.quantity===p.quantity)?.id||selectedDeal;}
 if(selectedDeal&&!state.deals.some(deal=>deal.id===selectedDeal))selectedDeal=null;
 $('#rounds').innerHTML=Array.from({length:5},(_,i)=>`<b class="round-dot ${state.round===i+1?'current':state.round>i+1?'done':''}">${i+1}</b>`).join('')+`<span>${state.status==='finished'?'닷새의 거래 완료':`${state.round}일째 / 5일`}</span>`;
 $('#credits').textContent=state.player.credits;
 const occupied=Object.values(state.player.inventory).reduce((a,b)=>a+b,0);
 $('#capacity').textContent=`${occupied} / 12`;$('#capacity-bar').style.width=`${occupied/12*100}%`;
 $('#gain').textContent=`${state.player.gain>0?'+':''}${state.player.gain}`;
 $('#inventory').innerHTML=Object.entries(goods).map(([key,label])=>`<div class="stock-row ${state.player.inventory[key]>=state.player.target[key]?'complete':''}" id="stock-${key}"><div><i class="goods-icon ${key}" aria-hidden="true"></i>${label}</div><b>${state.player.inventory[key]}</b><small>보급 ${state.player.target[key]}개 / ${state.player.inventory[key]>=state.player.target[key]?'확보':'부족 '+(state.player.target[key]-state.player.inventory[key])}</small></div>`).join('');
 $('#energy').textContent=state.energy;$('#limits').textContent=`흥정 ${state.talk_limit-state.talks}회 / 거래 ${2-state.trades_this_round}회`;
 $('#deck-count').textContent=`${state.piles.total}장`;$('#draw-count').textContent=state.piles.draw;$('#spent-count').textContent=`${state.piles.spent} / ${state.piles.discard}`;
 $('#hand').innerHTML=state.hand.map(card=>cardMarkup(card)).join('');
 document.querySelectorAll('#hand .tactic-card').forEach((node,index)=>node.style.setProperty('--angle',`${(index-(state.hand.length-1)/2)*3}deg`));
 $('#end-round').textContent=state.round===5?'보급대 보내기 →':'하루 마감 →';$('#end-round').disabled=state.busy||working||!!exchangeCard||state.phase!=='round_active';
 $('#redraw-controls').hidden=!exchangeCard;$('#redraw-confirm').disabled=exchangeIds.size===0||working;
 const chosen=currentDeal(),q=state.quote;
 $('#card-instruction').textContent=exchangeCard?`바꿀 카드 1~2장을 고르세요. 현재 ${exchangeIds.size}장 선택`:chosen?`${goods[chosen.good]} ${chosen.direction==='buy'?'입고':'판매'} 선택${q?` / 단가 ${q.min_price}~${q.max_price} CR로 흥정`:''} · 상품을 바꾸면 사용한 효과가 사라집니다.`:'물자 카드를 고르고, 손패로 조건을 바꿔 흥정하세요.';
 if(previous&&state.version>previous.version){
  const old=new Set(previous.offers.map(offer=>offer.id));
  state.offers.filter(offer=>!old.has(offer.id)).forEach(offer=>browsing.delete(offer.merchant));
 }
 renderMerchants();
 if(state.phase==='reward'){
  if(shownPhase!=='reward'){closeAll();$('#rewards').innerHTML=state.reward_choices.map(kind=>cardMarkup({id:`reward-${kind}`,kind},true)).join('');modal('#reward-dialog');document.querySelectorAll('#rewards .tactic-card').forEach((node,index)=>FX.flip(node,index));Sound.cue('reward');}
  shownPhase='reward';
 }else if(state.phase==='finished'){
  if(shownPhase!=='finished'){closeAll();ending();if(!FX.isReduced()){$('#departure').hidden=false;departureTimer=setTimeout(showEnding,1800);Sound.cue('round');}else showEnding();}
  shownPhase='finished';
 }else{
  if(shownPhase==='reward'){$('#reward-dialog').close();FX.deal();Sound.cue('draw');}
  shownPhase='round_active';
 }
 if(previous&&state.version>previous.version){
  if(state.player.credits!==previous.player.credits){FX.trade();Sound.cue('accept');}
  const oldIds=new Set(previous.offers.map(o=>o.id));state.offers.filter(o=>!oldIds.has(o.id)).forEach(o=>{FX.flip($(`#offer-${o.merchant}`));Sound.cue('offer');});
  if(state.last_action?.kind==='play_card'&&state.last_action.id!==previous.last_action?.id){if(pendingFlight){FX.flight(pendingFlight,$(`#merchant-${currentDeal()?.merchant||'a'}`));pendingFlight=null;}Sound.cue('card');}
  if(previous.busy&&!state.busy){notify(state.last_result?.speech||'상인이 답을 건넸습니다.');if(state.last_result?.decision==='reject'||state.last_result?.fallback)Sound.cue('reject');}
 }
}
async function refresh(){
 const response=await API.load();if(state&&response.state.version<state.version)return response;
 const previous=state;state=response.state;if(state.ruleset!=='cards-v2'||!state.deals)throw Error('새 상점으로 시작해주세요.');render(previous);return response;
}
async function action(fields){
 if(working||!state)return false;working=true;
 try{const response=await API.act({action_id:crypto.randomUUID(),expected_version:state.version,...fields});await refresh();if(response.pending){stage='장부와 물건을 살펴보는 중이에요.';setTimeout(()=>{if(state?.busy)refresh().catch(()=>{});},800);}return true;}
 catch(error){notify(error.message);await refresh().catch(()=>{});return false;}
 finally{working=false;if(state)render(state);}
}
async function follow(cursor){
 streamController?.abort();const controller=new AbortController();streamController=controller;
 while(!controller.signal.aborted){
  try{await API.subscribe(cursor,event=>{cursor=Math.max(cursor,event.id);if(event.type==='Progress'){stage=({'기억 조회 중':'지난 거래를 장부에서 찾고 있어요.','협상 중':'물건을 살펴보고 있어요.','조건 검증 중':'마지막으로 계산을 확인할게요.','조건 수정 중':'조건을 다시 생각해볼게요.'}[event.stage]||'잠깐만 기다려주세요.');const id=currentDeal()?.merchant;const node=$(`#merchant-${id} .merchant-speech p`);if(node)node.textContent=stage;}else refresh().catch(error=>notify(error.message));},controller.signal);}
  catch{if(controller.signal.aborted)return;$('#connection').textContent='연결 복구 중';}
  if(!controller.signal.aborted){await new Promise(resolve=>setTimeout(resolve,1000));if(!controller.signal.aborted){try{const latest=await refresh();cursor=latest.event_cursor;$('#connection').textContent='상점 연결됨';}catch{}}}
 }
}
async function start(){
 if(starting)return;starting=true;$('#start').disabled=true;
 try{await Sound.unlock();const assets=await artReady;await document.fonts.ready;if(assets.some(a=>a.status==='rejected'))notify('일부 그림을 불러오지 못했습니다.');closeAll();streamController?.abort();state=(await API.create()).state;selectedDeal=null;shownPhase='';exchangeCard=null;exchangeIds.clear();browsing.clear();render();FX.deal();Sound.cue('draw');const latest=await API.load();follow(latest.event_cursor);$('#continue').hidden=false;}
 catch(error){notify(error.message);}
 finally{starting=false;$('#start').disabled=false;}
}
async function chooseDeal(id){
 if(state.busy||working||exchangeCard)return;const deal=state.deals.find(d=>d.id===id);if(!deal)return;
 if(await action({kind:'select_deal',deal_id:id})){selectedDeal=id;browsing.add(deal.merchant);render(state);Sound.cue('select');}
}
async function ensureSelection(){
 if(state.preparation)return true;const deal=currentDeal();if(!deal){notify('먼저 상인의 물자 카드 한 장을 골라주세요.');return false;}
 return await action({kind:'select_deal',deal_id:deal.id});
}
async function useCard(id){
 if(state.busy||working||state.phase!=='round_active')return;
 if(exchangeCard){if(id===exchangeCard)return;if(exchangeIds.has(id))exchangeIds.delete(id);else if(exchangeIds.size<2)exchangeIds.add(id);else{notify('카드는 최대 2장까지 교환합니다.');return;}render(state);return;}
 let card=state.hand.find(c=>c.id===id);if(!card)return;
 if(card.kind==='redraw'){
  if(card.disabled_reason){notify(card.disabled_reason);return;}
  exchangeCard=id;exchangeIds.clear();render(state);return;
 }
 if(!['focus','extra'].includes(card.kind)&&!state.preparation){if(!await ensureSelection())return;card=state.hand.find(c=>c.id===id);}
 if(card.disabled_reason){notify(card.disabled_reason);return;}
 const target=currentDeal()?.merchant;if(target)browsing.add(target);
 pendingFlight=FX.capture($(`#hand [data-card="${id}"]`));if(!await action({kind:'play_card',card_id:id}))pendingFlight=null;
}
function showEnding(){clearTimeout(departureTimer);$('#departure').hidden=true;modal('#ending-dialog');}
function ending(){
 const complete=Object.keys(goods).every(g=>state.player.inventory[g]>=state.player.target[g]);
 $('#ending-summary').textContent=complete?'보급대에 필요한 물자를 모두 모았습니다. 당신의 상점이 마을의 내일을 조금 더 버티게 했습니다.':'보급대가 떠납니다. 닷새 동안 상점에 남긴 물자와 자금을 정산합니다.';
 $('#ranking').innerHTML=state.ranking.map(a=>`<div class="ranking-row"><strong>${a.rank}</strong><div>${esc(a.id==='player'?'나의 상점':a.name)}<small>목표 ${Object.entries(a.target).map(([g,n])=>`${goods[g]} ${n}`).join(' / ')}</small></div><strong>${a.gain>0?'+':''}${a.gain}</strong></div>`).join('');
 $('#recap').textContent=`거래 ${state.trades.length}건 / 모델 호출 ${state.model_calls}회. 순위는 초기 대비 자산 증가분입니다.`;
}
function detail(title,html){$('#detail-content').innerHTML=`<h2>${esc(title)}</h2>${html}`;modal('#detail-dialog');}
function deck(){if(!state)return;const counts={};state.deck.forEach(c=>counts[c.kind]=(counts[c.kind]||0)+1);detail(`협상 카드 / ${state.piles.total}장`,`<p>사용한 카드는 오늘 다시 뽑히지 않습니다.</p><div class="deck-list">${Object.entries(counts).map(([kind,n])=>`<div class="deck-row"><b>${state.catalog[kind].name} × ${n}</b><p>${state.catalog[kind].description}</p></div>`).join('')}</div>`);}
$('#start').onclick=start;$('#restart').onclick=start;
$('#continue').onclick=async()=>{try{await Sound.unlock();const latest=await refresh();follow(latest.event_cursor);}catch(error){notify(error.message);}};
$('#hand').onclick=event=>{const card=event.target.closest('[data-card]');if(card)useCard(card.dataset.card);};
$('#hand').addEventListener('focusin',event=>{const button=event.target.closest('[data-card]');if(button&&!exchangeCard){const card=state.hand.find(c=>c.id===button.dataset.card);if(card)$('#card-instruction').textContent=cardReason(card)||state.catalog[card.kind].description;}});
$('#redraw-cancel').onclick=()=>{exchangeCard=null;exchangeIds.clear();render(state);};
$('#redraw-confirm').onclick=async()=>{const id=exchangeCard,discard_ids=[...exchangeIds];if(!id||!discard_ids.length)return;pendingFlight=FX.capture($(`#hand [data-card="${id}"]`));exchangeCard=null;exchangeIds.clear();if(!await action({kind:'play_card',card_id:id,discard_ids}))pendingFlight=null;};
$('#merchants').onclick=async event=>{
 const deal=event.target.closest('[data-deal]'),talk=event.target.closest('[data-negotiate]'),accept=event.target.closest('[data-accept]'),browse=event.target.closest('[data-browse]'),offer=event.target.closest('[data-offer]');
 if(deal){await chooseDeal(deal.dataset.deal);return;}
 if(accept){await action({kind:'accept',offer_id:accept.dataset.accept});return;}
 if(browse){browsing.add(browse.dataset.browse);render(state);return;}
 if(offer){browsing.delete(offer.dataset.offer);render(state);return;}
 if(talk){if(!await ensureSelection())return;const effects=state.preparation_effects;const message=effects.history?'지난 거래를 기억하시죠? 장부의 실제 거래를 참고해서 제안해주세요.':effects.advantage?'협상 카드로 준비한 양보 조건을 반영해 제안해주세요.':effects.standard?'상점의 기준 조건으로 계약해주세요.':'골라둔 물자를 이 조건으로 거래하고 싶어요. 장부를 보고 제안해주세요.';await action({kind:'negotiate',message});}
};
$('#end-round').onclick=()=>action({kind:'end_round'});
$('#rewards').onclick=event=>{const card=event.target.closest('[data-card]');if(card)action({kind:'choose_reward',reward:card.dataset.card.replace('reward-','')});};
$('#skip-reward').onclick=()=>action({kind:'choose_reward',reward:null});
for(const button of document.querySelectorAll('[data-close]'))button.onclick=()=>button.closest('dialog').close();
$('#reward-dialog').addEventListener('cancel',event=>event.preventDefault());$('#ending-dialog').addEventListener('cancel',event=>event.preventDefault());
addEventListener('keydown',event=>{if(event.key==='Escape'&&exchangeCard){exchangeCard=null;exchangeIds.clear();render(state);}});
$('#deck-open').onclick=deck;$('#draw-pile').onclick=deck;
$('#history-open').onclick=()=>detail('상점 장부',state.trades.map(t=>`<div class="trade-row">${t.round}일째 / ${names[t.merchant]}<br>${t.buyer==='player'?'입고':'판매'} ${goods[t.good]} ${t.quantity}개 / ${t.total} CR</div>`).join('')+(state.trades.length?'':'<p>아직 거래한 물자가 없습니다.</p>')+'<h3>상인과 나눈 이야기</h3>'+state.dialogues.slice(-12).map(d=>`<div class="trade-row"><small>${d.role==='user'?'상점 주인':names[d.merchant]}</small><br>${esc(d.text)}</div>`).join(''));
$('#rules-open').onclick=()=>detail('폐허 상점 안내','<h3>닷새 동안의 거래</h3><p>보급대가 떠나기 전에 마을에 필요한 물자를 모으세요. 상인의 물자 카드를 고르고 손패를 사용한 뒤 흥정합니다. 입고는 상점이 물자를 사는 거래, 판매는 상점의 물자를 내주는 거래입니다.</p><h3>한 번의 선택으로</h3><p>물자 카드를 누르면 수량과 기준 조건이 자동으로 정해집니다. 협상 카드는 누르는 즉시 사용됩니다. 손패 교환만 바꿀 카드 1~2장을 선택합니다. 제안 카드의 거래 버튼을 누르면 바로 체결됩니다.</p><h3>하루의 기회</h3><p>손패 5장, 협상력 3, 흥정 2회와 거래 2회입니다. 협상력은 최대 4이며 다음 날로 넘어가지 않습니다. 하루를 마감하면 남은 기회와 제안이 사라집니다. 새로운 물자를 고르면 이전에 사용한 준비 효과가 사라집니다.</p><h3>카드와 상인</h3><p>가격 효과는 개당 최대 2입니다. 상인은 재고, 잔액, 공간과 손실 방지 조건을 지킵니다. 한꺼번에 카드는 가능한 범위에서 수량을 자동으로 늘립니다. 악수 카드는 모델 없이 기준 조건으로 제안합니다. 모델 실패는 별도로 표시하고 사용한 카드는 돌려주지 않습니다.</p><h3>상점 정산</h3><p>자산은 크레딧 + 보급 목표 이내 물자 가치의 2배 + 초과 물자 가치의 절반 + 전체 목표 달성 100점입니다. 기준 가치는 연료 10, 식량 8, 의약품 14, 부품 12입니다. 처음보다 늘어난 자산으로 순위를 정합니다.</p>');
$('#skip-departure').onclick=showEnding;
const prefs=Sound.preferences();FX.setReduced(prefs.reduced);$('#reduce-motion').checked=prefs.reduced;$('#music-volume').value=prefs.music*100;$('#sfx-volume').value=prefs.sfx*100;
function soundLabel(){const enabled=Sound.preferences().enabled;$('#sound-toggle').textContent=enabled?'소리 켜짐':'소리 꺼짐';$('#sound-toggle').setAttribute('aria-label',enabled?'소리 끄기':'소리 켜기');}
$('#sound-toggle').onclick=async()=>{await Sound.configure({enabled:!Sound.preferences().enabled});soundLabel();Sound.cue('select');};
$('#settings-open').onclick=()=>modal('#settings-dialog');$('#music-volume').oninput=e=>Sound.configure({music:Number(e.target.value)/100});$('#sfx-volume').oninput=e=>Sound.configure({sfx:Number(e.target.value)/100});$('#reduce-motion').onchange=e=>{FX.setReduced(e.target.checked);Sound.configure({reduced:e.target.checked});};soundLabel();
if(API.current())$('#continue').hidden=false;
API.api('/api/health').then(health=>{$('#connection').textContent=health.model.ready&&health.mcp_ready?'상인들이 기다립니다':'AI 연결 확인 필요';if(!health.model.ready)notify('로컬 AI 모델 연결을 확인해주세요.');}).catch(()=>{$('#connection').textContent='서버 연결 확인 필요';});
addEventListener('pagehide',()=>streamController?.abort());
