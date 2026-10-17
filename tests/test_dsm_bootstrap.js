'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const base = 'https://nas.example/webman/3rdparty/XiaomiCameraReview/index.cgi';

async function bootstrap({tokenResult = {success:true,data:{synotoken:'native-secret'}},
                          tokenFailure = false, bridgeStatus = 200, stored = null,
                          storageFailure = false} = {}) {
  const requests = [], status = {textContent:''}, signIn = {hidden:true},
        reopen = {hidden:true,href:''}, saved = new Map(stored === null ? [] : [['camera-dsm-bridge:'+base+'/',stored]]);
  let reloads = 0;
  const context = vm.createContext({URL, Date,
    document:{currentScript:{src:base+'/_dsm/bootstrap.js'},
      getElementById:id=>({'dsm-status':status,'dsm-signin':signIn,'dsm-open':reopen}[id])},
    location:{reload:()=>reloads++},
    sessionStorage:{getItem:key=>{if(storageFailure)throw Error('unavailable'); return saved.get(key)||null;},
      setItem:(key,value)=>{if(storageFailure)throw Error('unavailable'); saved.set(key,value);}},
    fetch:async (url,options) => {
      requests.push({url:String(url),options});
      if(requests.length === 1) {
        if(tokenFailure)throw Error('network');
        return {ok:true,json:async()=>tokenResult};
      }
      return {ok:bridgeStatus===200,status:bridgeStatus};
    },
  });
  const completion=vm.runInContext(fs.readFileSync('synology/bootstrap.js','utf8'),context);
  await completion;
  return {requests,status,signIn,reopen,saved,reloads};
}

test('retrieves the official DSM token and posts only to the fixed same-origin bridge', async () => {
  const f=await bootstrap();
  assert.equal(f.requests[0].url,'/webapi/entry.cgi?api=SYNO.API.Auth&version=6&method=token');
  assert.equal(f.requests[0].options.credentials,'same-origin');
  assert.equal(f.requests[0].options.cache,'no-store');
  assert.equal(f.requests[0].options.redirect,'error');
  assert.equal(f.requests[0].options.referrerPolicy,'same-origin',
    'DSM 7.4 token API requires a same-origin referrer despite the bootstrap page default');
  assert.equal(f.requests[1].url,base+'/_dsm/token');
  assert.equal(f.requests[1].options.method,'POST');
  assert.equal(f.requests[1].options.credentials,'same-origin');
  assert.equal(f.requests[1].options.headers['Content-Type'],'application/json');
  assert.deepEqual(JSON.parse(f.requests[1].options.body),{token:'native-secret'});
  assert.equal(f.reloads,1);
  assert.ok([...f.saved.values()].every(v=>!v.includes('native-secret')));
  assert.ok(f.requests.every(r=>!r.url.includes('native-secret')));
});

test('unavailable or expired DSM login shows the native sign-in link without reloading', async () => {
  for(const options of [{tokenResult:{success:false}}, {tokenResult:{success:true,data:{is_portal_port:true}}},
                         {tokenFailure:true}, {bridgeStatus:401}]) {
    const f=await bootstrap(options);
    assert.equal(f.reloads,0);
    assert.equal(f.signIn.hidden,false);
    assert.ok(f.status.textContent.length);
    assert.equal(f.status.textContent.includes('native-secret'),false);
  }
});

test('failed index after a successful bridge stops automatic reload loops', async () => {
  const f=await bootstrap({stored:String(Date.now())});
  assert.equal(f.requests.length,0);
  assert.equal(f.reloads,0);
  assert.equal(f.signIn.hidden,false);
});

test('expired token transport can refresh on a later index navigation', async () => {
  const f=await bootstrap({stored:String(Date.now()-120000)});
  assert.equal(f.requests.length,2);
  assert.equal(f.reloads,1);
});

test('disabled session storage exposes a click to open instead of risking a reload loop', async () => {
  const f=await bootstrap({storageFailure:true});
  assert.equal(f.requests.length,2);
  assert.equal(f.reloads,0);
  assert.equal(f.reopen.hidden,false);
  assert.equal(f.reopen.href,base+'/');
});
