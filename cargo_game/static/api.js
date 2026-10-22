// 게임 접근 토큰은 현재 브라우저 세션에만 보관하고 URL에는 넣지 않습니다.
let session;
try{session=JSON.parse(sessionStorage.getItem('last-cargo')||'null')}catch{session=null}
export const current=()=>session;
export async function api(path,body){
 const response=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json',...(session?{Authorization:`Bearer ${session.access_token}`}:{})},...(body?{body:JSON.stringify(body)}:{})});
 const result=await response.json();
 if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'입력한 조건을 확인해주세요.');
 return result;
}
export async function create(){const result=await api('/api/games',{});session={game_id:result.game_id,access_token:result.access_token};sessionStorage.setItem('last-cargo',JSON.stringify(session));return result;}
export const load=()=>api(`/api/games/${session.game_id}`);
export const act=body=>api(`/api/games/${session.game_id}/actions`,body);
export function clear(){session=null;sessionStorage.removeItem('last-cargo')}
export async function subscribe(cursor,onEvent,signal){
 const response=await fetch(`/api/games/${session.game_id}/events?after=${cursor}`,{headers:{Authorization:`Bearer ${session.access_token}`},signal});
 if(!response.ok)throw new Error('진행 상태 연결을 복구하지 못했습니다.');
 const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
 try{while(true){const {done,value}=await reader.read();if(done)return;buffer+=decoder.decode(value,{stream:true});let boundary;
  while((boundary=buffer.indexOf('\n\n'))>=0){const frame=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);const data=frame.split('\n').find(line=>line.startsWith('data: '));if(data)onEvent(JSON.parse(data.slice(6)));}
 }}finally{await reader.cancel().catch(()=>{});reader.releaseLock();}
}
