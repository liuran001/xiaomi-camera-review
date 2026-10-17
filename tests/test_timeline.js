'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {execFileSync} = require('node:child_process');
const {test} = require('node:test');
assert.ok(fs.existsSync('static/timeline.js'), 'the shared Beijing timeline module must exist');
const T = require('../static/timeline.js');
const epoch = T.parse('2026-10-16', '20:28:45');
const clip = (id, start, length, extra = {}) => ({id, channel:'camera / 00', start, duration:length, playable_duration:length, status:'complete', ...extra});

test('strict Beijing parsing is independent of host timezone', () => {
  assert.equal(epoch, 1792153725);
  assert.equal(T.parse('2026-10-16', '20:29:38') - epoch, 53);
  assert.equal(T.parse('2024-02-29', '00:00'), Date.parse('2024-02-29T00:00:00+08:00') / 1000);
  for (const tz of ['UTC', 'America/New_York', 'Asia/Tokyo']) {
    const actual = execFileSync(process.execPath, ['-e', "process.stdout.write(String(require('./static/timeline.js').parse('2026-10-16','20:29:38')))"], {env:{...process.env, TZ:tz}, encoding:'utf8'});
    assert.equal(Number(actual), epoch + 53, tz);
  }
  assert.equal(T.day(epoch), '2026-10-16'); assert.equal(T.clock(epoch), '20:28:45');
  assert.equal(T.dayStart('2026-10-16') + 86400, T.dayStart('2026-10-17'));
});

test('invalid calendar values never roll into another date or time', () => {
  for (const [date, time] of [['2026-02-29','12:00:00'], ['2026-04-31','00:00:00'], ['2026-13-01','00:00:00'], ['2026-10-16','24:00:00'], ['2026-10-16','20:60:00'], ['2026-10-16','20:28:60'], ['2026-10-16','2:28:45'], ['26-10-16','20:28:45'], ['', ''], ['0000-01-01','00:00:00']]) assert.equal(T.parse(date, time), null, `${date} ${time}`);
});

test('locating respects boundaries, gaps, channel, unstable files and actual EOF', () => {
  const clips = [clip('a',epoch,120), clip('other',epoch+110,120,{channel:'camera / 10'}), clip('writing',epoch+120,30,{status:'unstable'}), clip('b',epoch+150,120,{media_duration:7,playable_duration:7})];
  assert.equal(T.locate(clips,epoch+53,'camera / 00').offset,53);
  assert.equal(T.locate(clips,epoch+119.9,'camera / 00').clip.id,'a');
  const gap = T.locate(clips,epoch+120,'camera / 00');
  assert.equal(gap.clip.id,'b'); assert.equal(gap.offset,0); assert.equal(gap.gap,30);
  assert.equal(T.locate(clips,epoch+157,'camera / 00'),null);
  assert.equal(T.locate(clips,epoch+53,''),null); assert.equal(T.locate(clips,epoch+53,'absent'),null);
  assert.equal(T.end(clips[3]),epoch+157);
});

test('overlap continues at wall-clock offset and skips completely covered files', () => {
  const a = clip('a',epoch,120), b = clip('b',epoch+117,120), covered = clip('covered',epoch+80,20);
  assert.equal(T.locate([a,b],epoch+118,a.channel,a.id).clip.id,'a');
  assert.equal(T.locate([a,b],epoch+118,a.channel).clip.id,'b');
  const next = T.continueFrom([a,b,covered],a,epoch+120);
  assert.equal(next.clip.id,'b'); assert.equal(next.offset,3); assert.equal(next.gap,0);
  assert.equal(T.continueFrom([a,covered],a,epoch+120),null);
  const short = clip('short',epoch,120,{media_duration:7,playable_duration:7});
  assert.equal(T.continueFrom([short,b],short,T.end(short)).gap,110);
});

test('cross-midnight coverage clips to selected day and continuation keeps channel', () => {
  const midnight = T.dayStart('2026-10-17');
  const a = clip('late',midnight-1718,1853), b = clip('tomorrow',midnight+135,120);
  const coverage = T.coverage([a,b,clip('unstable',midnight+400,50,{status:'unstable'})],'2026-10-17',a.channel);
  assert.deepEqual(coverage.map(c=>[c.start,c.end]), [[0,135],[135,255]]);
  assert.equal(T.locate([a,b],midnight+10,a.channel).offset,1728);
  const next = T.continueFrom([a,b],a,T.end(a));
  assert.equal(next.clip.id,'tomorrow'); assert.equal(next.offset,0);
  assert.equal(T.day(next.clip.start + next.offset),'2026-10-17');
});
