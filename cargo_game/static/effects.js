// 상태 확정 이후의 표현만 담당합니다. 거래나 카드 효과를 계산하지 않습니다.
let reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
export function setReduced(value){reduced=value;document.body.classList.toggle('reduced-motion',value)}
export const isReduced=()=>reduced;
export function capture(node){if(!node)return null;return {rect:node.getBoundingClientRect(),html:node.outerHTML}}
export function flight(snapshot,target){
 if(reduced||!snapshot||!target)return;measure('card-use');
 const end=target.getBoundingClientRect(),start=snapshot.rect,wrapper=document.createElement('div');
 wrapper.className='flying-card';wrapper.inert=true;wrapper.setAttribute('aria-hidden','true');wrapper.innerHTML=snapshot.html;
 Object.assign(wrapper.style,{left:`${start.x}px`,top:`${start.y}px`,width:`${start.width}px`,height:`${start.height}px`});
 const child=wrapper.firstElementChild;Object.assign(child.style,{transform:'none',margin:'0',width:`${start.width}px`,height:`${start.height}px`});
 document.body.append(wrapper);
 wrapper.animate([{transform:'translate(0,0) rotate(0deg)',opacity:1},{transform:`translate(${end.x+end.width/2-start.x-start.width/2}px,${end.y+end.height/2-start.y-start.height/2}px) rotate(12deg) scale(.3)`,opacity:0}],{duration:440,easing:'cubic-bezier(.25,.7,.2,1)'}).finished.then(()=>wrapper.remove());
 burst(end.x+end.width/2,end.y+end.height/2,20);
}
export function deal(){if(reduced)return;measure('card-deal');document.querySelectorAll('#hand .tactic-card').forEach((card,index)=>{card.animate([{translate:'-80px 90px',opacity:0},{translate:'0 0',opacity:1}],{duration:400,delay:index*75,easing:'cubic-bezier(.2,.8,.2,1)',fill:'backwards'})})}
export function flip(node,index=0){if(!node||reduced)return;node.animate([{transform:'perspective(800px) rotateY(85deg)',opacity:0},{transform:'perspective(800px) rotateY(0deg)',opacity:1}],{duration:420,delay:index*100,easing:'ease-out',fill:'backwards'})}
export function pulse(node){if(!node||reduced)return;node.classList.remove('number-change');void node.offsetWidth;node.classList.add('number-change')}
const canvas=document.querySelector('#particles'),context=canvas.getContext('2d');let particles=[],frame=0,last=0;
function size(){canvas.width=innerWidth;canvas.height=innerHeight}addEventListener('resize',size);size();
export function burst(x,y,count=24){if(reduced||document.hidden)return;for(let i=0;i<Math.min(count,60-particles.length);i++){const angle=Math.random()*Math.PI*2,speed=25+Math.random()*100;particles.push({x,y,vx:Math.cos(angle)*speed,vy:Math.sin(angle)*speed,life:.55+Math.random()*.25,max:.8,size:1+Math.random()*3})}if(!frame){last=performance.now();frame=requestAnimationFrame(tick)}}
function tick(time){const delta=Math.min(.04,(time-last)/1000);last=time;context.clearRect(0,0,canvas.width,canvas.height);particles=particles.filter(p=>p.life>0);for(const p of particles){p.x+=p.vx*delta;p.y+=p.vy*delta;p.vy+=80*delta;p.life-=delta;context.globalAlpha=Math.max(0,p.life/p.max);context.fillStyle='#c6c8a9';context.fillRect(p.x,p.y,p.size,p.size)}context.globalAlpha=1;if(particles.length&&!document.hidden){frame=requestAnimationFrame(tick)}else{frame=0;particles=[];context.clearRect(0,0,canvas.width,canvas.height)}}
addEventListener('visibilitychange',()=>{if(document.hidden){cancelAnimationFrame(frame);frame=0;particles=[];context.clearRect(0,0,canvas.width,canvas.height)}});
export function trade(){measure('trade-complete');burst(innerWidth*.5,innerHeight*.48,40);for(const selector of ['#credits','#capacity','#gain'])pulse(document.querySelector(selector))}

// 실제 브라우저 프레임 간격만 기록합니다. 게임 상태에는 관여하지 않습니다.
let measurement=0;
function measure(label){
 const run=++measurement,frames=[];let last=performance.now();
 function sample(now){if(run!==measurement||document.hidden)return;frames.push(now-last);last=now;
 if(frames.length<60){requestAnimationFrame(sample);return;}
 frames.shift();frames.sort((a,b)=>a-b);
 document.documentElement.dataset.frameSample=JSON.stringify({label,samples:frames.length,medianMs:frames[Math.floor(frames.length*.5)],p95Ms:frames[Math.floor(frames.length*.95)],window:'about one second after animation trigger'});
 }requestAnimationFrame(sample);
}
