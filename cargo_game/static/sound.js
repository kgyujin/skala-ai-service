// 원본 합성 사운드. 사용자 입력 전에는 AudioContext를 생성하거나 재생하지 않습니다.
let settings={enabled:true,music:.2,sfx:.45,reduced:matchMedia('(prefers-reduced-motion: reduce)').matches};
try{settings={...settings,...JSON.parse(localStorage.getItem('cargo-settings')||'{}')}}catch{/* 손상된 설정은 기본값 사용 */}
let audio,master,musicGain,sfxGain,loop;
export const preferences=()=>({...settings});
function save(){localStorage.setItem('cargo-settings',JSON.stringify(settings))}
export async function unlock(){
 if(!audio){audio=new AudioContext();master=audio.createGain();master.connect(audio.destination);musicGain=audio.createGain();musicGain.connect(master);sfxGain=audio.createGain();sfxGain.connect(master);}
 await audio.resume();update();
}
function tone(frequency,delay,duration,volume=.1,type='sine',target=sfxGain){if(!audio||!target)return;const oscillator=audio.createOscillator(),gain=audio.createGain(),when=audio.currentTime+delay;oscillator.type=type;oscillator.frequency.setValueAtTime(frequency,when);gain.gain.setValueAtTime(.0001,when);gain.gain.exponentialRampToValueAtTime(volume,when+.018);gain.gain.exponentialRampToValueAtTime(.0001,when+duration);oscillator.connect(gain);gain.connect(target);oscillator.start(when);oscillator.stop(when+duration+.05);oscillator.onended=()=>{oscillator.disconnect();gain.disconnect()}}
function ambient(){if(!audio||!settings.enabled||document.hidden)return;for(const [index,note] of [110,164.81,220,261.63].entries())tone(note,index*1.8,7,.025,'sine',musicGain)}
function update(){if(!audio)return;master.gain.setTargetAtTime(settings.enabled&&!document.hidden?1:0,audio.currentTime,.08);musicGain.gain.setTargetAtTime(settings.music,audio.currentTime,.1);sfxGain.gain.setTargetAtTime(settings.sfx,audio.currentTime,.1);if(settings.enabled&&!document.hidden){if(!loop){ambient();loop=setInterval(ambient,8500)}}else{clearInterval(loop);loop=null}}
export async function configure(changes){settings={...settings,...changes};save();if(settings.enabled)await unlock();else update();return preferences()}
export function cue(kind){if(!settings.enabled||!audio)return;const notes={select:[330],draw:[180,250],card:[220,330,440],offer:[220,277,330],accept:[261.63,329.63,392,523.25],reject:[164,123],reward:[293,369,440,587],round:[196,247,294]}[kind]||[330];notes.forEach((frequency,index)=>tone(frequency,index*.065,kind==='accept'?.4:.16,.08,kind==='draw'?'triangle':'sine'))}
addEventListener('visibilitychange',update);
