// Exercise the actual frontend against a minimal DOM and mocked local API.
// This is a logic check, not a replacement for a real browser/UI test.
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const code = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const ids = [...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]);
if (new Set(ids).size !== ids.length) throw Error('Duplicate HTML IDs');

class Element {
  constructor(id) {
    this.id = id; this.value = ''; this.innerHTML = ''; this.textContent = '';
    this.hidden = false; this.disabled = false; this.listeners = {};
    this.classList = {toggle() {}};
  }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  async fire(name) { if (!this.listeners[name]) throw Error(`No ${name} listener for ${this.id}`); return this.listeners[name]({target:this}); }
}
const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
elements.wechatGroup.value = '合成测试群';
elements.wechatDelay.value = '10';
elements.reportGroup.value = '合成测试群';
elements.reportPeriod.value = 'day';
elements.captureReview.hidden = true;
const document = {getElementById(id) { if (!elements[id]) throw Error(`Missing ID: ${id}`); return elements[id]; }, querySelectorAll() {return []}, createElement(){return new Element('new')}};
let saved = null;
const sample = '<script>不要执行</script>';
const response = obj => ({ok:true, json:async()=>obj});
async function fetch(url, options={}) {
  const endpoint = String(url).split('?')[0];
  if (endpoint === '/api/session') return response({token:'session-token',group:'合成测试群'});
  if (endpoint === '/api/groups') return response([]);
  if (endpoint === '/api/stats') return response({total:0,members:0,from:null,to:null,daily:[],topics:[],ranking:[],truncated:false});
  if (endpoint === '/api/report') return response({group:'合成测试群',start:'2026-11-20',end_exclusive:'2026-11-21',timezone:'Asia/Singapore',period:'day',summary:'测试汇总',coverage:{available_messages:0},totals:{messages:0,sender_names:0,active_days:0},daily:[],topics:[],ranking:[],ranking_limited:false,caveats:['完整性未知']});
  if (endpoint === '/api/auto/status') return response({enabled:true,running:false,last_success:null,last_error:null});
  if (endpoint === '/api/auto/config') {
    if (options.headers['X-WBOT-Token'] !== 'session-token') throw Error('Missing session token');
    return response({enabled:JSON.parse(options.body).enabled});
  }
  if (endpoint === '/api/history/start') {
    if (options.headers['X-WBOT-Token'] !== 'session-token') throw Error('Missing session token');
    return response({id:'synthetic-history',status:'running'});
  }
  if (endpoint === '/api/history/status') return response({status:'ready',result:{added:1,duplicates:2,conflicts:0,coverage:{available_messages:3,available_from:'2026-01-01',available_to:'2026-01-02'}}});
  if (endpoint === '/api/wechat/summary') return response(saved ? {captures:1,lines:saved.length,topics:[],keywords:[]} : {captures:0,lines:0,topics:[],keywords:[]});
  if (endpoint === '/api/wechat/search') return response(saved ? {lines:[{capture_id:1,captured_at:'2026-11-20',line_number:1,content:sample}],total:1} : {lines:[],total:0});
  if (endpoint === '/api/wechat/start') {
    if (options.headers['X-WBOT-Token'] !== 'session-token') throw Error('Missing session token');
    return response({id:'synthetic-job',status:'waiting',delay:10});
  }
  if (endpoint === '/api/wechat/status') return response({status:'ready',group:'合成测试群',result:{lines:[{text:sample,score:0.6}]}});
  if (endpoint === '/api/wechat/save') {
    if (options.headers['X-WBOT-Token'] !== 'session-token') throw Error('Missing session token');
    saved = JSON.parse(options.body).lines;
    return response({capture_id:1,lines:saved.length});
  }
  throw Error(`Unexpected endpoint ${endpoint}`);
}
const timer = (fn, ms) => { if (ms === 1000 || ms === 1500) queueMicrotask(fn); return 1; };
const sandbox = {document, fetch, URLSearchParams, URL, Blob, Date, console, setTimeout:timer,
                 window:{setTimeout:timer,scrollTo(){},print(){}}};
vm.runInNewContext(code, sandbox, {filename:'app.js'});
const tick = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  for (let i=0; i<5; i++) await tick();
  await elements.reportGenerate.fire('click');
  if (!elements.periodReport.innerHTML.includes('测试汇总')) throw Error('Period report did not render');
  await elements.autoToggle.fire('click');
  if (!elements.autoStatus.textContent.includes('已暂停')) throw Error('Automatic update did not pause');
  await elements.historySync.fire('click');
  for (let i=0; i<5; i++) await tick();
  if (!elements.historyStatus.textContent.includes('新增 1 条')) throw Error('History sync result did not render');
  await elements.captureStart.fire('click');
  for (let i=0; i<5; i++) await tick();
  if (elements.captureReview.hidden) throw Error('OCR preview did not appear');
  if (elements.captureText.value !== sample) throw Error('OCR preview text mismatch');
  if (!elements.captureConfidence.textContent.includes('1 行')) throw Error('Confidence warning missing');
  await elements.captureSave.fire('click');
  if (!saved || saved.length !== 1 || saved[0] !== sample) throw Error('Reviewed line was not saved');
  if (!elements.captureReview.hidden) throw Error('Preview should be hidden after saving');
  if (elements.wechatRecent.innerHTML.includes('<script>')) throw Error('Saved text was inserted as executable HTML');
  if (!elements.wechatRecent.innerHTML.includes('&lt;script&gt;')) throw Error('Saved text was not escaped');
  console.log('PASS frontend session, period report, capture preview, review/save, and HTML escaping');
})().catch(err => { console.error(err); process.exitCode=1; });
