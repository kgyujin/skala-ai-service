// 실제 화면의 SSE 핸들러를 실행해 반려 초안과 최종 답변의 표시를 확인합니다.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
function screen() {
  let app, stream;
  const html = fs.readFileSync(path.join(__dirname, '../web_chat.html'), 'utf8');
  const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
  const context = {Vue: {
    createApp: options => ({mount: () => {app = options.setup();}}),
    ref: value => ({value}), reactive: value => value, nextTick: async () => {}
  }, fetch: async () => ({json: async () => ({})}),
  EventSource: class {constructor() {stream=this;} close() {}}};
  vm.runInNewContext(script, context);
  app.input.value = 'gateway-service 장애 진단'; app.onSubmit();
  return {app, emit: event => stream.onmessage({data: JSON.stringify(event)})};
}
test('검수 중인 장애 초안은 노출하지 않고 최종 답변 하나만 표시', () => {
  const {app,emit}=screen();
  emit({type:'AnswerToken',attempt:1,delta:'즉시 롤백합니다.'});
  emit({type:'Validation',verdict:'불합격'});
  emit({type:'AnswerToken',attempt:2,delta:'수정 중인 초안'});
  assert.equal(app.log.value.filter(x=>x.kind==='agent').length,0);
  emit({type:'FinalAnswer',reply:'확인된 사실만 제공합니다.'});
  assert.equal(app.log.value.filter(x=>x.kind==='agent').length,1);
  assert.equal(app.log.value.at(-1).text,'확인된 사실만 제공합니다.');
});
test('일반 대화 스트리밍은 유지하고 최종 답변으로 확정', () => {
  const {app,emit}=screen();
  emit({type:'AnswerToken',delta:'안녕'});
  assert.equal(app.log.value.at(-1).text,'안녕');
  emit({type:'FinalAnswer',reply:'안녕하세요.'});
  assert.equal(app.log.value.filter(x=>x.kind==='agent').length,1);
  assert.equal(app.log.value.at(-1).text,'안녕하세요.');
});
