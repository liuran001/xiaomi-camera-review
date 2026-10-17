'use strict';
// Run real app.js handlers/fetch flow; only browser and network boundaries are fixtures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const timeline = fs.existsSync('static/timeline.js') ? require('../static/timeline.js') : undefined;
const midnight = Date.parse('2026-10-17T00:00:00+08:00') / 1000;
const generation = '1'.repeat(32), nextGeneration = '2'.repeat(32);
const clip = (id, start, length, extra = {}) => ({id, generation, camera:'camera',channel:'camera / 00',date:new Date((start+28800)*1000).toISOString().slice(0,10),start,end:start+length,duration:length,playable_duration:length,media_duration:length,status:'complete',completed:length,progress:100,thumbnail:null,error:null,thumbnail_error:null,...extra});
function deferred() { let resolve; const promise = new Promise(r=>resolve=r); return {promise,resolve}; }
function fixture(route = () => [], config = {}) {
  const nodes = new Map(), requests = [], media = [];
  function node(tag = 'div') {
    return {tag,hidden:true,value:'',max:'',disabled:false,style:{},children:[],currentTime:0,src:'',options:[],textContent:'',paused:true,volume:1,muted:false,playbackRate:1,readyState:config.manualMedia?0:4,loads:0,buffered:{length:1,start:()=>0,end:()=>120},
      set id(value) { if (this._id && nodes.get(this._id)===this) nodes.delete(this._id); this._id=value; if(value) nodes.set(value,this); }, get id() { return this._id || ''; },
      insertAdjacentElement(position, next) { media.push(next); }, remove() { this.removed=true; },
      append(...items) { this.children.push(...items); if (tag === 'select') this.options.push(...items); },
      replaceChildren(...items) { this.children=[]; this.options=[]; this.append(...items); },
      setAttribute(name,value) { this[name]=value; }, getAttribute(name) { return this[name]; }, removeAttribute(name) { this[name]=''; },
      matches() { return false; }, pause() { this.paused=true; }, load() { this.loads++; if(this.src && !config.manualMedia) { this.onloadedmetadata?.(); this.oncanplay?.(); } }, async play() { this.paused=false; }};
  }
  const get = id => { if (!nodes.has(id)) { const item=node(['day','channel'].includes(id)?'select':id==='video'?'video':'div'); item.id=id; } return nodes.get(id); };
  get('day').value='2026-10-16'; get('jump-date').value='2026-10-16'; get('jump-time').value='20:29:38';
  const context = vm.createContext({
    document:{getElementById:get,createElement:node,querySelector:selector=>({content:selector.includes('app-base')?(config.base||''):(config.authMode||'password')})}, Timeline:timeline, URLSearchParams, Intl, Date,
    location:{href:''},
    fetch:async (url,options={}) => { const request={url,options,body:options.body?JSON.parse(options.body):null}; requests.push(request); if (url.endsWith('/api/status')) throw new Error('initialization disabled'); const body = await route(request); return {ok:!config.status||config.status<400,status:config.status||200,json:async()=>{if(config.invalidJSON)throw new Error('not json'); return body;}}; },
    setInterval:()=>0,setTimeout:()=>0,clearTimeout:()=>{},
  });
  vm.runInContext(fs.readFileSync('static/app.js','utf8'),context);
  return {get,requests,media,context,run:code=>vm.runInContext(code,context),set:(name,value)=>{context[name]=value;}};
}
function ready(id,duration,playable,sourceGeneration = generation) { return {id:'job-'+id,generation:sourceGeneration,status:'ready',url:'/media/'+id,duration,playable_duration:playable,media_duration:playable}; }
const settle = () => new Promise(resolve=>setImmediate(resolve));

test('DSM meta prefixes API and original media paths while keeping server media URLs intact', async () => {
  const base='/webman/3rdparty/XiaomiCameraReview/index.cgi';
  const f=fixture(r=>r.url===base+'/api/preview'?{...ready('a',8,120),url:base+'/api/media/a.mp4'}:[], {base,authMode:'dsm'});
  await f.run("api('clips?date=2026-10-17')");
  assert.ok(f.requests.some(r=>r.url===base+'/api/clips?date=2026-10-17'));
  f.set('a',clip('a',midnight,120)); f.run('state.current=a; state.clips=[a];');
  await f.run('playSegment(0)'); assert.equal(f.get('video').src,base+'/api/media/a.mp4');
  f.get('original').onclick(); assert.equal(f.get('video').src,base+'/api/original/a');
});

test('DSM expiry exposes native sign-in and cancels playback even for non-JSON denial', async () => {
  for (const invalidJSON of [false,true]) {
    const f=fixture(()=>({error:'DSM session expired',auth:'dsm',login_url:'/'}),{base:'/dsm',authMode:'dsm',status:401,invalidJSON});
    f.get('video').src='/dsm/api/media/a.mp4'; f.run('state.token=5;');
    await assert.rejects(f.run("api('clips')"));
    assert.equal(f.get('login').hidden,false); assert.equal(f.get('app').hidden,true);
    assert.equal(f.get('login-form').hidden,true); assert.equal(f.get('dsm-login').hidden,false);
    assert.equal(f.get('dsm-signin').href,'/?launchApp=com.xiaomi.camera.review'); assert.equal(f.get('video').paused,true);
    assert.equal(f.get('video').src,''); assert.equal(f.run('state.token'),6);
  }
});

test('DSM return action navigates to native desktop without a password logout call', async () => {
  const f=fixture(()=>[],{base:'/dsm',authMode:'dsm'});
  assert.equal(f.get('logout').textContent,'返回 DSM');
  await f.get('logout').onclick();
  assert.equal(f.context.location.href,'/');
  assert.equal(f.requests.some(r=>r.url.endsWith('/api/logout')),false);
});

test('direct expiry retains password login and logout mode', async () => {
  const f=fixture(()=>({error:'请先登录'}),{status:401});
  await assert.rejects(f.run("api('clips')"));
  assert.equal(f.get('login-form').hidden,false); assert.equal(f.get('dsm-login').hidden,true);
  await f.get('logout').onclick().catch(()=>{});
  assert.ok(f.requests.some(r=>r.url==='/api/logout'));
  assert.equal(f.context.location.href,'');
});

test('real ended handler forwards overlap offset and reads beyond date filter', async () => {
  const a=clip('a',midnight-20,30), b=clip('b',midnight+7,40), covered=clip('covered',midnight-10,5), other=clip('other',midnight+10,50,{channel:'camera / 10'}), writing=clip('writing',midnight+10,50,{status:'unstable'});
  const f=fixture(r=>r.url.startsWith('/api/clips?')?[a,b,covered,other,writing]:r.url.startsWith('/api/preview')?ready(r.body.clip_id,30,40):[]);
  f.get('video').src='/media/a';
  f.set('initial',a); f.run('state.current=initial; state.clips=[initial]; state.segment=0; state.segmentDuration=30;');
  await f.get('video').onended(); await settle();
  const request=f.requests.find(r=>r.url==='/api/preview');
  assert.ok(request,'ended must request the next playable recording');
  assert.equal(request.body.clip_id,'b'); assert.equal(request.body.offset,3,'skip the three repeated wall-clock seconds');
  assert.ok(f.requests.some(r=>r.url.startsWith('/api/clips?')&&!r.url.includes('date=')));
  assert.equal(f.get('day').value,'2026-10-17'); assert.equal(f.get('video').src,'/media/b');
});

test('original ends at actual EOF, preview chunks retain source offsets', async () => {
  const a=clip('a',midnight,120,{media_duration:7,playable_duration:7}), b=clip('b',midnight+5,40);
  const f=fixture(r=>r.url.startsWith('/api/clips?')?[a,b]:r.url.startsWith('/api/preview')?ready(r.body.clip_id,30,40):[]);
  f.get('video').src='/api/original/a';
  f.set('initial',a); f.run('state.current=initial; state.clips=[initial]; state.direct=true; state.segment=90; state.segmentDuration=30;');
  await f.get('video').onended(); await settle();
  assert.ok(f.requests.find(r=>r.url==='/api/preview'),'original EOF must request another clip');
  assert.equal(f.requests.find(r=>r.url==='/api/preview').body.offset,2);
  const g=fixture(r=>ready(r.body.clip_id,30,120)); g.set('initial',clip('long',midnight,120));
  g.get('video').src='/media/long';
  g.run('state.current=initial; state.clips=[initial]; state.segment=53; state.segmentDuration=30;');
  await g.get('video').onended();
  assert.equal(g.requests.find(r=>r.url==='/api/preview').body.offset,83);
});

test('jump form uses Beijing seconds, requires channel and preserves player after final EOF', async () => {
  const a=clip('a',Date.parse('2026-10-16T20:28:45+08:00')/1000,120);
  const f=fixture(r=>r.url.startsWith('/api/clips?')?[a]:r.url.startsWith('/api/preview')?ready('a',30,120):[]);
  assert.equal(typeof f.get('jump-form').onsubmit,'function','date/time form must be wired');
  await f.get('jump-form').onsubmit({preventDefault(){}});
  assert.equal(f.requests.filter(r=>r.url==='/api/preview').length,0); assert.match(f.get('toast').textContent,/选择.*通道/);
  f.get('channel').value=a.channel;
  await f.get('jump-form').onsubmit({preventDefault(){}});
  assert.equal(f.requests.find(r=>r.url==='/api/preview').body.offset,53);
  f.get('jump-date').value='2026-02-29'; await f.get('jump-form').onsubmit({preventDefault(){}}); assert.match(f.get('toast').textContent,/有效|无效/);
  const oldSrc=f.get('video').src; f.get('jump-date').value='2026-10-17'; f.get('jump-time').value='23:59:59';
  await f.get('jump-form').onsubmit({preventDefault(){}}); assert.equal(f.get('video').src,oldSrc); assert.match(f.get('toast').textContent,/无.*录像/);
});

test('day slider resolves across files and paints full-day coverage and person markers', async () => {
  const a=clip('a',midnight-20,60), b=clip('b',midnight+100,120);
  const f=fixture(r=>r.url.startsWith('/api/clips?')?[a,b]:r.url.startsWith('/api/preview')?ready(r.body.clip_id,30,120):[]);
  f.get('day').value='2026-10-17'; f.get('channel').value=a.channel; f.set('clips',[a,b]);
  f.run('state.clips=clips; state.events=[{clip_id:"a",channel:clips[0].channel,start:clips[0].start,start_offset:25,end_offset:35}]; renderEvents();');
  assert.equal(Number(f.get('seek').max),86399); assert.equal(f.get('coverage-markers').children.length,2);
  assert.equal(f.get('event-markers').children[0].style.left,(5/86400*100)+'%');
  f.get('seek').value='153'; await f.get('seek').onchange();
  assert.equal(f.requests.find(r=>r.url==='/api/preview').body.clip_id,'b'); assert.equal(f.requests.find(r=>r.url==='/api/preview').body.offset,53);
});

test('stale preview cannot publish after date changes or another clip is selected', async () => {
  const stale=deferred(), filtered=deferred(), a=clip('a',midnight-60,120), b=clip('b',midnight+100,120);
  const f=fixture(r=>r.url==='/api/preview'?(r.body.clip_id==='a'?stale.promise:ready('b',30,120)):filtered.promise);
  f.set('a',a); f.set('b',b); const pending=f.run('selectClip(a,5)');
  f.get('day').value='2026-10-17'; const changing=f.get('day').onchange(); stale.resolve(ready('old',30,120)); await pending;
  assert.notEqual(f.get('video').src,'/media/old');
  filtered.resolve([]); await changing;
  const stale2=deferred(), g=fixture(r=>r.url==='/api/preview'?(r.body.clip_id==='a'?stale2.promise:ready('b',30,120)):[]);
  g.set('a',a);g.set('b',b); const first=g.run('selectClip(a,5)'); await g.run('selectClip(b,10)'); stale2.resolve(ready('old',30,120)); await first;
  assert.equal(g.get('video').src,'/media/b');
});

test('stale complete-catalog continuation cannot override a newer manual clip', async () => {
  const stale=deferred(), a=clip('a',midnight-20,30), b=clip('b',midnight+7,40), chosen=clip('chosen',midnight+100,50);
  const f=fixture(r=>r.url.startsWith('/api/clips?')&&!r.url.includes('date=')?stale.promise:r.url==='/api/preview'?ready(r.body.clip_id,30,50):[]);
  f.get('video').src='/media/a';
  f.set('a',a);f.set('chosen',chosen); f.run('state.current=a; state.clips=[a]; state.segmentDuration=30;');
  const next=f.get('video').onended(); await settle();
  assert.ok(f.requests.some(r=>r.url.startsWith('/api/clips?')&&!r.url.includes('date=')),'exercise an outstanding catalog request');
  await f.run('selectClip(chosen,4)'); stale.resolve([a,b]); await next; await settle();
  assert.equal(f.run('state.current.id'),'chosen'); assert.equal(f.get('video').src,'/media/chosen');
});

test('newer time jump wins when full catalogs return out of order', async () => {
  const first=deferred(), a=clip('a',midnight,120), b=clip('b',midnight+120,120); let calls=0;
  const f=fixture(r=>r.url.startsWith('/api/clips?')&&!r.url.includes('date=')?(++calls===1?first.promise:[a,b]):r.url==='/api/preview'?ready(r.body.clip_id,30,120):[]);
  f.get('channel').value=a.channel; f.set('midnight',midnight);
  const old=f.run('seekTime(midnight+10)'); await f.run('seekTime(midnight+173)');
  first.resolve([a,b]); await old;
  assert.equal(f.run('state.current.id'),'b'); assert.equal(f.get('video').src,'/media/b');
  assert.equal(f.requests.filter(r=>r.url==='/api/preview').length,1);
});

test('timeupdate crosses midnight within a file and limits original playback to playable boundary once', async () => {
  const pending=deferred(), a=clip('a',midnight-20,60), b=clip('b',midnight+40,60); let catalogs=0;
  const f=fixture(r=>r.url.startsWith('/api/clips?')&&!r.url.includes('date=')?(catalogs++,pending.promise):r.url==='/api/preview'?ready(r.body.clip_id,30,60):[]);
  f.set('a',a); f.run('state.current=a; state.clips=[a]; state.direct=true;'); f.get('video').src='/api/original/a';
  f.get('video').currentTime=25; await f.get('video').ontimeupdate();
  assert.equal(f.get('day').value,'2026-10-17'); assert.match(f.get('position').textContent,/2026-10-17 00:00:05/);
  f.get('video').currentTime=65; await f.get('video').ontimeupdate(); f.get('video').ontimeupdate(); await settle();
  assert.equal(catalogs,1,'physical media longer than playable duration must stop at boundary and queue continuation once');
  assert.equal(f.get('video').paused,true);
  pending.resolve([a,b]); await settle();
  assert.equal(f.requests.find(r=>r.url==='/api/preview').body.offset,0);
});

test('date/channel change invalidates outstanding jumps and old ended events immediately', async () => {
  for (const filter of ['day','channel']) {
    const pending=deferred(), a=clip('a',midnight-60,120);
    const f=fixture(r=>r.url.startsWith('/api/clips?')&&!r.url.includes('date=')?pending.promise:r.url==='/api/preview'?ready('a',30,120):[]);
    f.get('channel').value=a.channel; f.set('a',a); await f.run('selectClip(a,10)');
    f.set('midnight',midnight); const jump=f.run('seekTime(midnight-20)');
    f.get(filter).value=filter==='day'?'2026-10-17':'camera / 10'; await f.get(filter).onchange();
    await f.get('video').onended();
    pending.resolve([a]); await jump;
    assert.equal(f.get('video').src,'',filter+' must stop old playback');
    assert.equal(f.requests.filter(r=>r.url==='/api/preview').length,1,filter+' must invalidate queued jumps/ended');
  }
});

test('typing a date remains possible while playback time updates', async () => {
  const a=clip('a',midnight,120), f=fixture(r=>r.url==='/api/preview'?ready('a',30,120):[]);
  f.get('day').value='2026-10-17'; f.set('a',a); await f.run('selectClip(a,10)');
  f.get('jump-date').value='2026-10-15'; f.get('video').currentTime=5; f.get('video').ontimeupdate();
  assert.equal(f.get('jump-date').value,'2026-10-15');
});

test('catalog refresh updates analysis progress while ready preview keeps measured boundary', async () => {
  const pending=deferred(), a=clip('a',midnight,120,{status:'pending',progress:0,completed:0,media_duration:null}), complete={...a,status:'complete',progress:100,completed:7,media_duration:7,playable_duration:7};
  const f=fixture(r=>r.url==='/api/preview'?pending.promise:r.url.startsWith('/api/clips?')?[complete]:[]);
  f.get('day').value='2026-10-17'; f.set('a',a); const play=f.run('selectClip(a,1)');
  await f.run('refreshClips()');
  assert.equal(f.run('state.current.progress'),100,'analysis header should continue polling progress');
  pending.resolve(ready('a',6,7)); await play;
  assert.equal(f.run('clipDuration(state.current)'),7); assert.equal(f.get('video').src,'/media/a');
});

test('an outstanding user jump has priority over old ended and direct timeupdate events', async () => {
  for (const direct of [false,true]) {
    const pending=deferred(), a=clip('a',midnight,120), target=clip('target',midnight+300,120); let catalogs=0;
    const f=fixture(r=>r.url.startsWith('/api/clips?')&&!r.url.includes('date=')?(++catalogs===1?pending.promise:[a,target]):r.url==='/api/preview'?ready(r.body.clip_id,30,120):[]);
    f.get('day').value='2026-10-17'; f.get('channel').value=a.channel; f.set('a',a); f.set('midnight',midnight); await f.run('selectClip(a,90)');
    f.set('direct',direct); f.run('state.direct=direct;');
    const jump=f.run('seekTime(midnight+353)');
    f.get('video').currentTime=120; if (direct) f.get('video').ontimeupdate();
    const ended=f.get('video').onended(); await settle();
    assert.equal(catalogs,1,'old EOF must not start competing continuation');
    assert.equal(f.requests.filter(r=>r.url==='/api/preview').length,1,'old EOF must not request another segment');
    pending.resolve([a,target]); await jump; await ended;
    assert.equal(f.run('state.current.id'),'target'); assert.equal(f.requests.filter(r=>r.url==='/api/preview').at(-1).body.offset,53);
  }
});

test('original metadata and EOF measure an unprobed clip before choosing continuation offset', async () => {
  for (const metadata of [true,false]) {
    const a=clip('a',midnight,120,{media_duration:null}), b=clip('b',midnight+5,40);
    const f=fixture(r=>r.url.startsWith('/api/clips?')?[a,b]:r.url==='/api/preview'?ready('b',30,40):[]);
    f.get('day').value='2026-10-17'; f.set('a',a); f.run('state.current=a; state.clips=[a]; state.position=0;');
    f.get('original').onclick();
    if (metadata) { f.get('video').duration=7; f.get('video').onloadedmetadata(); assert.equal(f.run('clipDuration(state.current)'),7); }
    else f.get('video').duration=NaN;
    f.get('video').currentTime=7; await f.get('video').onended();
    const request=f.requests.find(r=>r.url==='/api/preview');
    assert.ok(request,'original EOF must continue after actual media end');
    assert.equal(request.body.clip_id,'b'); assert.equal(request.body.offset,2,'actual 7-second EOF overlaps next start by 2 seconds');
  }
});

test('final EOF stops in the same channel and manual navigation follows visible list', async () => {
  const a=clip('a',midnight,30), other=clip('other',midnight+20,60,{channel:'camera / 10'});
  const f=fixture(r=>r.url.startsWith('/api/clips?')?[a,other]:r.url==='/api/preview'?ready(r.body.clip_id,30,60):[]);
  f.get('day').value='2026-10-17'; f.set('clips',[a,other]); f.run('state.current=clips[0]; state.clips=clips; state.segmentDuration=30;'); f.get('video').src='/media/a';
  await f.get('video').onended();
  assert.equal(f.run('state.current.id'),'a'); assert.equal(f.requests.filter(r=>r.url==='/api/preview').length,0); assert.match(f.get('toast').textContent,/无后续/);
  await f.get('next').onclick(); await settle();
  assert.equal(f.requests.find(r=>r.url==='/api/preview').body.clip_id,'other');
});

test('events from the old media cannot move or advance a newly selected pending preview', async () => {
  const pending=deferred(), a=clip('a',midnight,120), b=clip('b',midnight+300,120);
  const f=fixture(r=>r.url==='/api/preview'?(r.body.clip_id==='b'?pending.promise:ready('a',30,120)):[]);
  f.get('day').value='2026-10-17'; f.set('a',a); f.set('b',b); await f.run('selectClip(a,10)');
  f.get('video').currentTime=30; const changing=f.run('selectClip(b,53)');
  f.get('video').ontimeupdate(); f.get('video').onended(); await settle();
  assert.equal(f.run('state.position'),53,'paused old source time must not change the new source position');
  assert.equal(f.requests.filter(r=>r.url==='/api/preview').length,2,'only the requested new preview may be queued');
  pending.resolve(ready('b',30,120)); await changing; assert.equal(f.get('video').src,'/media/b');
});

test('day refresh preserves a measured 7-second boundary against a non-null 120-second value', async () => {
  const a=clip('a',midnight,120), f=fixture(r=>r.url.startsWith('/api/clips?')?[clip('a',midnight,120)]:[]);
  f.get('day').value='2026-10-17'; f.set('a',a); f.run('state.current=a; state.clips=[a];');
  f.get('original').onclick(); f.get('video').duration=7; f.get('video').onloadedmetadata();
  assert.equal(f.run('clipDuration(state.current)'),7);
  await f.run('refreshClips()');
  assert.equal(f.run('clipDuration(state.current)'),7);
  assert.equal(f.run('state.clips[0].media_duration'),7);
  assert.equal(f.get('coverage-markers').children[0].style.width,(7/86400*100)+'%');
});

test('full catalog jump never requests offset 60 in a clip already measured as 7 seconds', async () => {
  const a=clip('a',midnight,120,{media_duration:null}), b=clip('b',midnight+100,120);
  const f=fixture(r=>r.url.startsWith('/api/clips?')?[clip('a',midnight,120,{media_duration:null}),b]:r.url==='/api/preview'?ready(r.body.clip_id,30,120):[]);
  f.get('day').value='2026-10-17'; f.get('channel').value=a.channel; f.set('a',a); f.set('midnight',midnight); f.run('state.current=a; state.clips=[a];');
  f.get('original').onclick(); f.get('video').duration=7; f.get('video').onloadedmetadata();
  f.get('jump-date').value='2026-10-17'; f.get('jump-time').value='00:01:00'; await f.get('jump-form').onsubmit({preventDefault(){}});
  const request=f.requests.find(r=>r.url==='/api/preview');
  assert.equal(request.body.clip_id,'b'); assert.equal(request.body.offset,0);
});

test('switching away and selecting the measured clip again retains its boundary through a longer preview', async () => {
  const a=clip('a',midnight,120), b=clip('b',midnight+200,120);
  const f=fixture(r=>r.url.startsWith('/api/clips?')?[clip('a',midnight,120),clip('b',midnight+200,120)]:r.url==='/api/preview'?ready(r.body.clip_id,30,120):[]);
  f.get('day').value='2026-10-17'; f.set('a',a); f.set('b',b); f.run('state.current=a; state.clips=[a,b];');
  f.get('original').onclick(); f.get('video').duration=7; f.get('video').onloadedmetadata();
  await f.run('selectClip(b,0)'); await f.run('refreshClips()'); await f.get('clips').children[0].onclick();
  assert.equal(f.run('state.current.id'),'a'); assert.equal(f.run('clipDuration(state.current)'),7);
  assert.equal(f.run('state.segmentDuration'),7,'preview duration must respect the retained boundary');
  f.get('video').currentTime=8; f.get('video').ontimeupdate(); await settle();
  assert.ok(f.requests.some(r=>r.url.startsWith('/api/clips?')&&!r.url.includes('date=')),'the physical longer preview must stop at the known boundary');
});

test('continuation merges known boundaries for other clips before choosing a successor', async () => {
  const measured=clip('measured',midnight+5,120), current=clip('current',midnight,15), next=clip('next',midnight+20,60);
  const f=fixture(r=>r.url.startsWith('/api/clips?')?[clip('measured',midnight+5,120),current,next]:r.url==='/api/preview'?ready(r.body.clip_id,r.body.clip_id==='current'?15:30,r.body.clip_id==='current'?15:60):[]);
  f.get('day').value='2026-10-17'; f.set('measured',measured); f.set('current',current); f.run('state.current=measured; state.clips=[measured,current];');
  f.get('original').onclick(); f.get('video').duration=7; f.get('video').onloadedmetadata();
  await f.run('selectClip(current,0)'); await f.get('video').onended();
  assert.equal(f.requests.filter(r=>r.url==='/api/preview').at(-1).body.clip_id,'next','fully covered measured source must be skipped');
});

test('a new generation of the same path can use a longer duration after an old short measurement', async () => {
  const a=clip('a',midnight,120), fresh=clip('a',midnight,120,{generation:nextGeneration});
  const f=fixture(r=>r.url.startsWith('/api/clips?')?[fresh]:r.url==='/api/preview'?ready('a',30,120,nextGeneration):[]);
  f.get('day').value='2026-10-17'; f.get('channel').value=a.channel; f.set('a',a); f.set('midnight',midnight); f.run('state.current=a; state.clips=[a];');
  f.get('original').onclick(); f.get('video').duration=7; f.get('video').onloadedmetadata();
  await f.run('refreshClips()');
  assert.equal(f.run('state.current.generation'),nextGeneration); assert.equal(f.run('clipDuration(state.current)'),120);
  assert.equal(f.get('video').src,'','new generation must detach old media before metadata can affect it');
  f.get('video').duration=3; f.get('video').onloadedmetadata(); f.get('video').currentTime=3; await f.get('video').onended();
  assert.equal(f.run('clipDuration(state.current)'),120,'old original metadata/EOF must not shorten the new generation');
  await f.run('seekTime(midnight+60)');
  assert.equal(f.requests.find(r=>r.url==='/api/preview').body.offset,60); assert.equal(f.run('clipDuration(state.current)'),120);
});

test('pending old-generation preview cannot publish into a new-generation current clip', async () => {
  const pending=deferred(), old=clip('a',midnight,120), fresh=clip('a',midnight,120,{generation:nextGeneration});
  const f=fixture(r=>r.url==='/api/preview'?pending.promise:r.url.startsWith('/api/clips?')?[fresh]:[]);
  f.get('day').value='2026-10-17'; f.set('old',old); const play=f.run('selectClip(old,0)');
  await f.run('refreshClips()'); pending.resolve(ready('old',7,7)); await play;
  assert.equal(f.run('state.current.generation'),nextGeneration); assert.equal(f.run('clipDuration(state.current)'),120);
  assert.notEqual(f.get('video').src,'/media/old');
});

test('preview response generation must match the selected source before publication', async () => {
  const old=clip('a',midnight,120), f=fixture(r=>r.url==='/api/preview'?ready('wrong-generation',7,7,nextGeneration):[]);
  f.get('day').value='2026-10-17'; f.set('old',old); await f.run('selectClip(old,0)');
  assert.equal(f.run('state.current.generation'),generation); assert.equal(f.run('clipDuration(state.current)'),120);
  assert.notEqual(f.get('video').src,'/media/wrong-generation');
});

test('day response begun before selecting a new generation cannot revert the selected source', async () => {
  const pending=deferred(), old=clip('a',midnight,120), fresh=clip('a',midnight,120,{generation:nextGeneration});
  const f=fixture(r=>r.url.startsWith('/api/clips?')?pending.promise:r.url==='/api/preview'?ready('a',30,120,nextGeneration):[]);
  f.get('day').value='2026-10-17'; f.set('fresh',fresh);
  const refresh=f.run('refreshClips()'); await f.run('selectClip(fresh,60)');
  pending.resolve([old]); await refresh;
  assert.equal(f.run('state.current.generation'),nextGeneration); assert.equal(f.get('video').src,'/media/a');
});

test('cross-midnight continuation keeps the pending new-day list refresh', async () => {
  const pending=deferred(), a=clip('a',midnight-20,30), b=clip('b',midnight+10,120);
  const f=fixture(r=>r.url.startsWith('/api/clips?')?(r.url.includes('date=')?pending.promise:[a,b]):r.url==='/api/preview'?ready('b',30,120):[]);
  f.set('a',a); f.run('state.current=a; state.clips=[a]; state.segmentDuration=30;'); f.get('video').src='/media/a'; f.get('video').currentTime=30;
  f.get('video').ontimeupdate(); await settle(); assert.equal(f.run('state.current.id'),'b');
  pending.resolve([a,b]); await settle();
  assert.equal(f.run('state.clips.some(c => c.id === "b")'),true,'new-day response for the selected generation must remain publishable');
});

test('short first preview prepares one successor during playback and consumes it at ended', async () => {
  const a=clip('a',midnight,120);
  const f=fixture(r=>r.url==='/api/preview'?ready('a-'+r.body.offset,r.body.seconds,120):[]);
  f.get('day').value='2026-10-17'; f.set('a',a); await f.run('selectClip(a,0)');
  assert.equal(f.requests.find(r=>r.url==='/api/preview').body.seconds,8);
  f.get('video').currentTime=1; await f.get('video').ontimeupdate(); await settle();
  let posts=f.requests.filter(r=>r.url==='/api/preview');
  assert.equal(posts.length,2); assert.equal(posts[1].body.offset,8); assert.equal(posts[1].body.seconds,12); assert.equal(posts[1].body.prefetch,true);
  await f.get('video').ontimeupdate(); await settle(); assert.equal(f.requests.filter(r=>r.url==='/api/preview').length,2);
  await f.get('video').onended(); assert.equal(f.get('video').src,'/media/a-8');
  f.get('video').currentTime=1; await f.get('video').ontimeupdate(); await settle();
  posts=f.requests.filter(r=>r.url==='/api/preview'); assert.equal(posts.at(-1).body.offset,20); assert.equal(posts.at(-1).body.seconds,20);
  await f.get('video').onended();
  f.get('video').currentTime=1; await f.get('video').ontimeupdate(); await settle();
  posts=f.requests.filter(r=>r.url==='/api/preview'); assert.equal(posts.at(-1).body.offset,40); assert.equal(posts.at(-1).body.seconds,30);
  assert.equal(f.run('clipDuration(state.current)'),120);
});

test('cross-file midnight prefetch keeps selection until ended and reuses prepared job', async () => {
  const a=clip('a',midnight-8,8), b=clip('b',midnight,60);
  const f=fixture(r=>r.url.startsWith('/api/clips?')?[a,b]:r.url==='/api/preview'?ready(r.body.clip_id,r.body.seconds,r.body.clip_id==='a'?8:60):[]);
  f.set('a',a); await f.run('selectClip(a,0)');
  const title=f.get('current-title').textContent;
  f.get('video').onplaying(); await settle();
  assert.equal(f.run('state.current.id'),'a'); assert.equal(f.get('day').value,'2026-10-16'); assert.equal(f.get('current-title').textContent,title);
  const prepared=f.requests.filter(r=>r.url==='/api/preview'); assert.equal(prepared.length,2); assert.equal(prepared[1].body.clip_id,'b');
  await f.get('video').onended();
  assert.equal(f.run('state.current.id'),'b'); assert.equal(f.get('day').value,'2026-10-17'); assert.equal(f.get('video').src,'/media/b');
  assert.equal(f.requests.filter(r=>r.url==='/api/preview').length,2);
});

test('unfinished speculative successor is promoted at ended and failed preparation retries foreground', async () => {
  for (const fail of [false,true]) {
    const a=clip('a',midnight,120);
    const f=fixture(r=>{
      if (r.url!=='/api/preview') return [];
      if (r.body.prefetch) {
        if (fail) throw new Error('speculative failure');
        return {id:'pending',generation,status:'pending'};
      }
      return ready('a-'+r.body.offset,r.body.seconds,120);
    });
    f.get('day').value='2026-10-17'; f.set('a',a); await f.run('selectClip(a,0)');
    f.get('video').onplaying(); await settle(); await f.get('video').onended();
    const posts=f.requests.filter(r=>r.url==='/api/preview'); assert.equal(posts.length,3);
    assert.equal(posts[2].body.offset,8); assert.equal(posts[2].body.seconds,12); assert.equal(posts[2].body.prefetch,false);
    assert.equal(f.get('video').src,'/media/a-8');
  }
});

test('stale speculative response cannot change a new playback intent or its known boundary', async () => {
  const pending=deferred(),a=clip('a',midnight,120),b=clip('b',midnight+300,120);
  const f=fixture(r=>r.url==='/api/preview'?(r.body.prefetch?pending.promise:ready(r.body.clip_id,r.body.seconds,120)):[]);
  f.get('day').value='2026-10-17'; f.set('a',a); f.set('b',b); await f.run('selectClip(a,0)');
  f.get('video').onplaying(); await f.run('selectClip(b,0)'); pending.resolve(ready('stale',1,1)); await settle();
  assert.equal(f.get('video').src,'/media/b'); assert.equal(f.run('clipDuration(state.current)'),120);
  assert.equal(f.run('knownDurations.get(a.id+":"+a.generation).playable_duration'),120);
});

test('catalog replacement of a prefetched future generation prevents stale publication', async () => {
  const a=clip('a',midnight,8), b=clip('b',midnight+8,60), fresh={...b,generation:nextGeneration}; let refreshed=false;
  const f=fixture(r=>r.url.startsWith('/api/clips?')?[a,refreshed?fresh:b]:r.url==='/api/preview'?ready(r.body.clip_id,r.body.seconds,r.body.clip_id==='a'?8:60,r.body.clip_id==='b'&&refreshed?nextGeneration:generation):[]);
  f.get('day').value='2026-10-17'; f.set('a',a); await f.run('selectClip(a,0)'); f.get('video').onplaying(); await settle();
  refreshed=true; await f.run('refreshClips()'); await f.get('video').onended();
  assert.equal(f.run('state.current.generation'),nextGeneration); assert.equal(f.requests.filter(r=>r.url==='/api/preview').length,3);
});

test('delayed old full catalog cannot prepare a generation replaced while candidate was unknown', async () => {
  const delayed=deferred(), a=clip('a',midnight,8), old=clip('b',midnight+8,60), fresh={...old,generation:nextGeneration}; let catalogs=0;
  const f=fixture(r=>{
    if (r.url.startsWith('/api/clips?')) return r.url.includes('date=')?[a,fresh]:(++catalogs===1?delayed.promise:[a,fresh]);
    if (r.url==='/api/preview') return ready(r.body.clip_id,r.body.seconds,r.body.clip_id==='a'?8:60,r.body.clip_id==='a'?generation:nextGeneration);
    return [];
  });
  f.get('day').value='2026-10-17'; f.set('a',a); await f.run('selectClip(a,0)');
  f.get('video').onplaying(); await settle(); await f.run('refreshClips()');
  delayed.resolve([a,old]); await settle(); await f.get('video').onended();
  assert.equal(f.run('state.current.generation'),nextGeneration); assert.equal(f.get('video').src,'/media/b');
});

test('covering cached preview seeks relative to actual interval and continues at its end', async () => {
  const a=clip('a',midnight,120);
  const f=fixture(r=>r.url==='/api/preview'?{...ready('a-'+r.body.offset,30,120),offset:r.body.offset===53?40:r.body.offset}:[]);
  f.get('day').value='2026-10-17'; f.set('a',a); await f.run('selectClip(a,53)');
  assert.equal(f.requests.find(r=>r.url==='/api/preview').body.reuse,true);
  assert.equal(f.run('state.segment'),40); assert.equal(f.get('video').currentTime,13);
  f.get('video').ontimeupdate(); assert.equal(f.run('state.position'),53);
  await settle(); assert.equal(f.requests.filter(r=>r.url==='/api/preview').at(-1).body.offset,70);
});

test('timeline seeks inside the current generation interval without network or media reload', async () => {
  const a=clip('a',midnight,120), f=fixture(r=>r.url==='/api/preview'?{...ready('a',30,120),offset:40}:[]);
  f.get('day').value='2026-10-17'; f.get('channel').value=a.channel; f.set('a',a); f.set('midnight',midnight);
  await f.run('selectClip(a,53)'); const video=f.get('video'), requests=f.requests.length, loads=video.loads;
  video.pause(); video.volume=.3; video.playbackRate=1.5;
  await f.run('seekTime(midnight+61)');
  video.ontimeupdate(); await settle();
  assert.equal(f.requests.length,requests); assert.equal(video.loads,loads); assert.equal(f.get('video'),video);
  assert.equal(video.currentTime,21); assert.equal(f.run('state.position'),61); assert.equal(video.paused,true);
  assert.equal(video.volume,.3); assert.equal(video.playbackRate,1.5);
});

test('replacement retains last picture until playable and old callbacks cannot move the new intent', async () => {
  const delayed=deferred(), a=clip('a',midnight,120), b=clip('b',midnight+300,120);
  const f=fixture(r=>r.url==='/api/preview'?(r.body.clip_id==='b'?delayed.promise:ready('a',8,120)):[]);
  f.get('day').value='2026-10-17'; f.set('a',a); f.set('b',b); await f.run('selectClip(a,0)');
  const old=f.get('video'), oldUpdate=old.ontimeupdate, oldEnded=old.onended;
  const change=f.run('selectClip(b,53)');
  assert.equal(old.src,'/media/a'); assert.equal(old.hidden,false); assert.equal(old.paused,true);
  old.currentTime=7; oldUpdate(); oldEnded(); await settle(); assert.equal(f.run('state.position'),53);
  delayed.resolve(ready('b',8,120)); await change;
  assert.notEqual(f.get('video'),old); assert.equal(f.get('video').src,'/media/b'); assert.equal(old.src,'');
  oldUpdate(); oldEnded(); assert.equal(f.run('state.current.id'),'b');
});

test('successor media is loaded while current plays and swapped without a second media load', async () => {
  const a=clip('a',midnight,120), f=fixture(r=>r.url==='/api/preview'?{...ready('a-'+r.body.offset,r.body.seconds,120),offset:r.body.offset}:[]);
  f.get('day').value='2026-10-17'; f.set('a',a); await f.run('selectClip(a,0)');
  const old=f.get('video'); old.volume=.4; old.playbackRate=1.75; old.muted=true;
  old.onplaying(); await settle();
  const next=f.run('successor.media'); assert.ok(next,'prefetch must create a browser media element');
  assert.equal(next.src,'/media/a-8'); assert.equal(next.preload,'auto'); assert.equal(next.paused,true); assert.equal(next.hidden,true);
  const loads=next.loads; old.currentTime=8; await old.onended();
  assert.equal(f.get('video'),next); assert.equal(next.loads,loads); assert.equal(old.paused,true); assert.equal(old.src,'');
  assert.equal(next.volume,.4); assert.equal(next.playbackRate,1.75); assert.equal(next.muted,true); assert.equal(next.paused,false);
});

test('unready media keeps the previous frame and logout clears both elements before late readiness', async () => {
  const a=clip('a',midnight,120), b=clip('b',midnight+300,120);
  const f=fixture(r=>r.url==='/api/preview'?{...ready(r.body.clip_id,8,120),offset:r.body.offset}:[],{manualMedia:true});
  f.get('day').value='2026-10-17'; f.set('a',a); f.set('b',b); await f.run('selectClip(a,0)');
  let first=f.get('video'); first.readyState=4; first.onloadedmetadata(); first.oncanplay();
  await f.run('selectClip(b,53)'); const candidate=f.run('pendingMedia.media'), canplay=candidate.oncanplay;
  assert.equal(first.src,'/media/a'); assert.equal(first.hidden,false); assert.equal(candidate.hidden,true);
  await f.get('logout').onclick(); assert.equal(first.src,''); assert.equal(candidate.src,'');
  candidate.readyState=4; canplay(); assert.equal(f.get('app').hidden,true); assert.equal(candidate.paused,true);
});

test('original metadata captured before switching modes cannot shorten the replacement clip', async () => {
  const a=clip('a',midnight,120), f=fixture(r=>r.url==='/api/preview'?ready('a',8,120):[]);
  f.get('day').value='2026-10-17'; f.set('a',a); f.run('state.current=a; state.clips=[a];'); f.get('original').onclick();
  const old=f.get('video'), metadata=old.onloadedmetadata; await f.get('compatible').onclick();
  old.duration=2; metadata(); assert.equal(f.run('clipDuration(state.current)'),120);
});

test('a covering preview with no useful remaining interval retries exact offset once', async () => {
  const a=clip('a',midnight,120), f=fixture(r=>r.url==='/api/preview'?{...ready(r.body.reuse?'tail':'exact',r.body.reuse?30:8,120),offset:r.body.reuse?40:r.body.offset}:[]);
  f.get('day').value='2026-10-17'; f.set('a',a); await f.run('selectClip(a,69.99)');
  const posts=f.requests.filter(r=>r.url==='/api/preview'); assert.equal(posts.length,2); assert.equal(posts[1].body.reuse,false);
  assert.equal(f.get('video').src,'/media/exact'); assert.equal(f.run('state.segment'),69.99);
});

test('a failed time jump leaves the retained recording replayable', async () => {
  const a=clip('a',midnight,120), f=fixture(r=>r.url==='/api/preview'?ready('a',8,120):[]);
  f.get('day').value='2026-10-17'; f.get('channel').value=a.channel; f.set('a',a); f.set('midnight',midnight);
  await f.run('selectClip(a,0)'); const video=f.get('video');
  await f.run('seekTime(midnight+300)'); video.currentTime=3; video.ontimeupdate();
  assert.equal(f.run('state.position'),3,'failed jump must restore retained media event ownership');
  await video.play(); video.onplay(); assert.equal(video.paused,false);
});

test('a browser preload failure retries the ready URL in a fresh foreground element', async () => {
  const a=clip('a',midnight,120), f=fixture(r=>r.url==='/api/preview'?ready('a-'+r.body.offset,r.body.seconds,120):[]);
  f.get('day').value='2026-10-17'; f.set('a',a); await f.run('selectClip(a,0)');
  f.get('video').onplaying(); await settle(); const broken=f.run('successor.media');
  broken.readyState=0; broken.error={code:2}; broken.onerror?.();
  await f.get('video').onended();
  assert.notEqual(f.get('video'),broken); assert.equal(f.get('video').src,'/media/a-8'); assert.equal(broken.src,'');
});

test('expiry during a ready browser preload removes both sources and blocks captured successor events', async () => {
  const a=clip('a',midnight,120); let expired=false;
  const f=fixture(r=>{if(expired) throw new Error('unused'); return r.url==='/api/preview'?ready('a-'+r.body.offset,r.body.seconds,120):[];});
  f.get('day').value='2026-10-17'; f.set('a',a); await f.run('selectClip(a,0)');
  const video=f.get('video'); video.onplaying(); await settle(); const preloaded=f.run('successor.media');
  f.run("fetch=async()=>({status:401,ok:false,json:async()=>({error:'expired'})})");
  await assert.rejects(f.run("api('clips')"));
  assert.equal(video.src,''); assert.equal(preloaded.src,''); assert.equal(video.paused,true); assert.equal(preloaded.paused,true);
  await video.onended(); assert.equal(f.run('successor'),null); assert.equal(f.get('app').hidden,true);
});

test('unbuffered times in a loaded interval use authenticated foreground resolution', async () => {
  const a=clip('a',midnight,120), f=fixture(r=>r.url==='/api/preview'?{...ready('a',30,120),offset:40}:r.url.startsWith('/api/clips?')?[a]:[]);
  f.get('day').value='2026-10-17'; f.get('channel').value=a.channel; f.set('a',a); f.set('midnight',midnight);
  await f.run('selectClip(a,53)'); f.get('video').pause(); f.get('video').buffered={length:1,start:()=>0,end:()=>15};
  const before=f.requests.length; await f.run('seekTime(midnight+61)');
  assert.ok(f.requests.slice(before).some(r=>r.url.startsWith('/api/clips?')),'unbuffered source time must not use a blind local seek');
  assert.ok(f.requests.slice(before).some(r=>r.url==='/api/preview' && r.body.reuse));
});

test('a prepared covering interval waits for its relative seek to finish before replacing the picture', async () => {
  const a=clip('a',midnight,120), f=fixture(r=>r.url==='/api/preview'?{...ready('a-'+r.body.offset,r.body.prefetch?12:8,120),offset:r.body.prefetch?4:0}:[],{manualMedia:true});
  f.get('day').value='2026-10-17'; f.set('a',a); await f.run('selectClip(a,0)');
  const old=f.get('video'); old.readyState=4; old.onloadedmetadata(); old.oncanplay();
  old.onplaying(); await settle(); const prepared=f.run('successor.media'); prepared.readyState=4;
  let time=0; Object.defineProperty(prepared,'currentTime',{get:()=>time,set:value=>{time=value;prepared.seeking=true;prepared.readyState=1;}});
  await old.onended(); assert.equal(f.get('video'),old); assert.equal(old.src,'/media/a-0'); assert.equal(time,4);
  prepared.readyState=4; prepared.oncanplay(); assert.equal(f.get('video'),old,'canplay before seek completion must not expose the wrong picture');
  prepared.seeking=false; prepared.onseeked();
  assert.equal(f.get('video'),prepared); assert.equal(f.run('state.segment'),4); assert.equal(f.run('state.position'),8);
});
