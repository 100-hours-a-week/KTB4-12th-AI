import test from 'node:test';
import assert from 'node:assert/strict';
import {initializeAdmin,scheduleSessionExpiry} from '../static/admin.js';

test('30 day expiry avoids browser timer overflow and never slides', () => {
  let now = 1000, expired = 0;
  const timers = [];
  scheduleSessionExpiry(30 * 86400, () => now, (fn, delay) => timers.push({fn, delay}), () => expired++);
  assert.equal(timers[0].delay, 2147483647);
  now += timers[0].delay; timers[0].fn();
  assert.equal(timers[1].delay, 30 * 86400000 - 2147483647);
  assert.equal(expired, 0);
  now += timers[1].delay; timers[1].fn();
  assert.equal(expired, 1);
  assert.equal(timers.length, 2);
});

test('logout removes saved conversations without clearing unrelated tab storage',async t=>{
 const storage=new Map([['console-chat/synthetic','private synthetic draft'],['unrelated','keep']]);
 const previous=Object.getOwnPropertyDescriptor(globalThis,'sessionStorage');
 Object.defineProperty(globalThis,'sessionStorage',{configurable:true,value:{get length(){return storage.size;},key:i=>[...storage.keys()][i],removeItem:key=>storage.delete(key)}});
 t.after(()=>{if(previous)Object.defineProperty(globalThis,'sessionStorage',previous);else delete globalThis.sessionStorage;});
 const page=adminPage(t);const session=await initializeAdmin();assert.equal(session.authenticated,true);
 await page.element('admin-logout').onclick();
 assert.deepEqual([...storage],[['unrelated','keep']]);
});

function adminPage(t, {logout, refresh, save} = {}) {
  const elements = new Map(), requests = [], redirects = [], events = [];
  let sessionReads = 0, logoutCalls = 0;
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set(['hidden']);
      elements.set(id, {value:'',textContent:'',disabled:false,checked:false,required:false,
        closest:()=>element(id+'-label'),
        classList:{add:name=>classes.add(name),remove:name=>classes.delete(name),contains:name=>classes.has(name)}});
    }
    return elements.get(id);
  }
  const json = (body, status=200) => new Response(JSON.stringify(body), {status});
  t.mock.method(globalThis, 'setTimeout', ()=>1);
  for (const [name,value] of Object.entries({document:{getElementById:element,dispatchEvent:event=>events.push(event.type)},location:{replace:path=>redirects.push(path)}})) {
    const previous=Object.getOwnPropertyDescriptor(globalThis,name);
    Object.defineProperty(globalThis,name,{value,configurable:true,writable:true});
    t.after(()=>{if(previous)Object.defineProperty(globalThis,name,previous);else delete globalThis[name];});
  }
  t.mock.method(globalThis, 'fetch', async (path, options={}) => {
    requests.push({path,...options});
    if (path === '/api/admin/session') {
      sessionReads++;
      if (sessionReads > 1 && refresh) return refresh(json);
      return json({authenticated:true,csrf:sessionReads===1?'synthetic-initial-csrf':'synthetic-new-csrf',expiresIn:2592000});
    }
    if (path === '/api/admin/logout') return logout ? logout(++logoutCalls, options, json) : json({authenticated:false});
    if (['/api/admin/provider-key/chat','/api/admin/provider-key/profiler','/api/admin/backend-token'].includes(path)) {
      if (options.method === 'PUT' && save) return save(path,options,json);
      return json({mode:'managed',configured:false,revision:null,state:'missing',verified:false,writable:true});
    }
    throw new Error('Unexpected test route');
  });
  return {element,requests,redirects,events};
}

test('logout refreshes stale CSRF once after another tab logs in', async t => {
  const page = adminPage(t, {logout:(count,options,json)=>{
    assert.equal(options.headers.get('X-CSRF-Token'), count===1?'synthetic-initial-csrf':'synthetic-new-csrf');
    return count===1 ? json({detail:'CSRF_REJECTED'},403) : json({authenticated:false});
  }});
  await initializeAdmin();
  await page.element('admin-logout').onclick();
  assert.equal(page.requests.filter(r=>r.path==='/api/admin/session').length,2);
  assert.equal(page.requests.filter(r=>r.path==='/api/admin/logout').length,2);
  assert.deepEqual(page.redirects,['/login']);
  assert.equal(page.element('admin-logout').disabled,false);
});

for (const status of [403,503]) test(`logout HTTP ${status} gives visible generic feedback without retry`, async t => {
  const page = adminPage(t, {logout:(_count,_options,json)=>json({detail:'SYNTHETIC_PRIVATE_DETAIL'},status)});
  await initializeAdmin();
  await page.element('admin-logout').onclick();
  assert.equal(page.requests.filter(r=>r.path==='/api/admin/logout').length,1);
  assert.equal(page.requests.filter(r=>r.path==='/api/admin/session').length,1);
  assert.deepEqual(page.redirects,[]);
  assert.match(page.element('toast').textContent,/로그아웃을 확인하지 못했습니다/);
  assert.equal(page.element('toast').classList.contains('hidden'),false);
  assert.ok(!page.element('toast').textContent.includes('SYNTHETIC_PRIVATE'));
  assert.equal(page.element('admin-logout').disabled,false);
});

test('logout stops after one CSRF refresh even if the retry also fails', async t => {
  const page = adminPage(t, {logout:(_count,_options,json)=>json({detail:'CSRF_REJECTED'},403)});
  await initializeAdmin();
  await page.element('admin-logout').onclick();
  assert.equal(page.requests.filter(r=>r.path==='/api/admin/logout').length,2);
  assert.equal(page.requests.filter(r=>r.path==='/api/admin/session').length,2);
  assert.deepEqual(page.redirects,[]);
  assert.equal(page.element('toast').classList.contains('hidden'),false);
});

test('unavailable session refresh never repeats the logout write', async t => {
  const page = adminPage(t, {
    logout:(_count,_options,json)=>json({detail:'CSRF_REJECTED'},403),
    refresh:json=>json({detail:'ADMIN_SESSION_STORE_UNAVAILABLE'},503),
  });
  await initializeAdmin();
  await page.element('admin-logout').onclick();
  assert.equal(page.requests.filter(r=>r.path==='/api/admin/logout').length,1);
  assert.equal(page.requests.filter(r=>r.path==='/api/admin/session').length,2);
  assert.deepEqual(page.redirects,[]);
  assert.equal(page.element('toast').classList.contains('hidden'),false);
});

test('double click during logout sends one write and releases the button afterwards', async t => {
  let complete;
  const page = adminPage(t, {logout:(_count,_options,json)=>new Promise(resolve=>{
    complete=()=>resolve(json({authenticated:false}));
  })});
  await initializeAdmin();
  const first = page.element('admin-logout').onclick();
  assert.equal(page.element('admin-logout').disabled,true);
  await page.element('admin-logout').onclick();
  assert.equal(page.requests.filter(r=>r.path==='/api/admin/logout').length,1);
  complete(); await first;
  assert.deepEqual(page.redirects,['/login']);
  assert.equal(page.element('admin-logout').disabled,false);
});

test('connection loss while logging out is visible without exposing the exception', async t => {
  const page = adminPage(t, {logout:()=>{throw new Error('SYNTHETIC_PRIVATE_FAILURE');}});
  await initializeAdmin();
  await page.element('admin-logout').onclick();
  assert.equal(page.element('toast').classList.contains('hidden'),false);
  assert.ok(!page.element('toast').textContent.includes('SYNTHETIC_PRIVATE'));
  assert.deepEqual(page.redirects,[]);
});

for (const status of [200,409,503]) test(`settings HTTP ${status} notifies readiness only after a confirmed save`, async t => {
  const page = adminPage(t, {save:(_path,_options,json)=>json({configured:status===200},status)});
  await initializeAdmin();
  for (const kind of ['provider','profiler','backend']) {
    page.element(kind+'-key').value='synthetic-setting-value';
    await page.element(kind+'-form').onsubmit({preventDefault(){}});
    assert.equal(page.element(kind+'-key').value,'');
  }
  assert.deepEqual(page.events.filter(name=>name==='admin-settings-saved'),status===200?['admin-settings-saved','admin-settings-saved','admin-settings-saved']:[]);
  assert.equal(page.events.filter(name=>name==='provider-key-changing').length,2);
  assert.equal(page.events.filter(name=>name==='provider-key-change-finished').length,2);
});

test('lost provider save response still brackets usage invalidation without claiming readiness', async t => {
  const page=adminPage(t,{save:()=>{assert.deepEqual(page.events,['provider-key-changing']);throw new Error('synthetic lost response');}});
  await initializeAdmin();
  page.element('provider-key').value='synthetic-setting-value';
  await page.element('provider-form').onsubmit({preventDefault(){}});
  assert.deepEqual(page.events,['provider-key-changing','provider-key-change-finished']);
});
