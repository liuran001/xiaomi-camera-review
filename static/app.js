'use strict';
const $ = id => document.getElementById(id);
const appBase = document.querySelector('meta[name="app-base"]')?.content || '';
const dsmMode = document.querySelector('meta[name="auth-mode"]')?.content === 'dsm';
const state = {clips: [], events: [], current: null, segment: 0, segmentDuration: 30, position: 0, token: 0, filterToken: 0, selectionToken: 0, seekingToken: null, endingToken: null, direct: false, paused: false};
const knownDurations = new Map();
let successor = null;
let pendingMedia = null;
let loadedMedia = null;
// Keep at most the visible media and one prepared replacement. All URLs retain
// their authenticated same-origin endpoint; the browser owns only transient data.
function releaseMedia(media) {
  if (!media) return;
  media.pause(); media.removeAttribute('src'); media.load();
  if (media.id !== 'video') media.remove();
}
function clearSuccessor(keep = null) {
  if (successor?.media && successor.media !== keep) releaseMedia(successor.media);
  successor = null;
}
function clearPending(keep = null) {
  const pending = pendingMedia; pendingMedia = null;
  if (pending?.media && pending.media !== keep && pending.media !== $('video')) releaseMedia(pending.media);
}
function clearPlayback() {
  clearSuccessor(); clearPending(); loadedMedia = null; releaseMedia($('video'));
}
function replacementMedia() {
  const current = $('video');
  if (!current.getAttribute('src')) return current;
  const media = element('video'); media.controls = true; media.playsInline = true;
  media.hidden = true; media.preload = 'auto'; current.insertAdjacentElement('afterend', media);
  return media;
}
function currentMedia(binding) {
  return binding === loadedMedia && binding.token === state.token && binding.media === $('video') &&
    (!binding.clip || sameSource(binding.clip, state.current)) && !!binding.media.getAttribute('src') &&
    state.seekingToken !== state.selectionToken;
}
function mediaSettings(from, to) {
  to.volume = from.volume; to.muted = from.muted; to.playbackRate = from.playbackRate;
}
function bindMedia(binding) {
  const media = binding.media;
  media.onloadedmetadata = () => {
    if (!currentMedia(binding)) return;
    if (state.direct) adoptOriginalDuration(media.duration);
  };
  media.oncanplay = () => {};
  media.onseeked = () => {};
  media.onplay = () => { if (!currentMedia(binding)) media.pause(); };
  media.onplaying = () => { if (currentMedia(binding)) prepareSuccessor(); else media.pause(); };
  media.ontimeupdate = () => {
    if (!currentMedia(binding) || !state.current) return;
    if (!$('seek').matches(':active')) updatePosition((state.direct ? 0 : state.segment) + media.currentTime);
    if (!state.direct && !media.paused && media.currentTime > 0 && media.currentTime < state.segmentDuration) prepareSuccessor();
    if ((state.direct ? 0 : state.segment) + media.currentTime >= clipDuration(state.current)) {
      media.pause(); if (state.direct) continuePlayback(); else advanceSegment();
    }
  };
  media.onended = () => {
    if (!currentMedia(binding) || !state.current) return;
    if (state.direct) { adoptOriginalDuration(media.duration, media.currentTime); updatePosition(clipDuration(state.current)); }
    return state.direct ? continuePlayback() : advanceSegment();
  };
  media.onerror = () => {
    if (currentMedia(binding)) playerMessage('浏览器无法播放该画面，可切换兼容播放或重新选择录像。');
  };
}
function stageMedia(clip, offset, start, length, url, token, direct, prepared = null, resume = true) {
  const media = prepared || replacementMedia(), old = $('video');
  const valid = () => pendingMedia === pending && token === state.token && sameSource(clip, state.current);
  const pending = pendingMedia = {media, token, clip, resume};
  const seek = () => {
    if (!valid()) return;
    if (direct) { adoptOriginalDuration(media.duration); selectedHeader(); }
    const relative = Math.max(0, Math.min(offset, clipDuration(state.current) - .001) - start);
    if (Math.abs(media.currentTime - relative) > .001) media.currentTime = relative;
  };
  const activate = () => {
    if (!valid() || media.readyState < 3) return;
    // Metadata may have arrived before this prepared media was promoted.
    seek();
    if (media.seeking || media.readyState < 3) return;
    mediaSettings(old, media); old.pause();
    if (old !== media) { old.id = ''; media.id = 'video'; }
    media.hidden = false;
    loadedMedia = {media, token, clip, start, length, direct, catalogRevision}; pendingMedia = null;
    bindMedia(loadedMedia);
    if (old !== media) releaseMedia(old);
    $('player-message').hidden = true;
    updatePosition(start + media.currentTime);
    if (pending.resume) media.play().catch(() => {});
  };
  media.onloadedmetadata = seek; media.oncanplay = activate; media.onseeked = activate;
  media.onerror = () => {
    if (!valid()) return;
    clearPending(); playerMessage('浏览器无法播放该画面，请重新选择录像。');
  };
  if (!prepared) { media.preload = 'auto'; media.src = url; media.load(); }
  else if (media.readyState >= 1) seek();
  activate();
}
function seekLoaded(clip, offset) {
  const binding = loadedMedia;
  if (!binding?.clip || !Timeline.playable(clip) || !currentMedia(binding) || !sameSource(binding.clip, clip) || pendingMedia ||
      staleSuccessor({clip, catalogRevision:binding.catalogRevision || 0})) return false;
  const start = binding.direct ? 0 : binding.start;
  const end = Math.min(clipDuration(clip), binding.direct ? clipDuration(clip) : start + binding.length);
  if (binding.media.readyState < 2 || offset < start || offset >= end - .001) return false;
  const buffered = binding.media.buffered, relative = offset - start;
  let local = false;
  for (let index = 0; index < buffered.length; index++) {
    if (relative >= buffered.start(index) && relative < buffered.end(index) - .001) { local = true; break; }
  }
  if (!local) return false;
  state.selectionToken++; state.seekingToken = null; state.endingToken = null; clearSuccessor();
  state.current = mergeDuration(clip); binding.media.currentTime = offset - start;
  updatePosition(offset); selectedHeader(); return true;
}
let segmentIndex = 0;
const previewLengths = [8, 12, 20, 30];
const previewLength = index => previewLengths[Math.min(index, previewLengths.length - 1)];
let catalogRevision = 0;
const catalogObservations = new Map();
const labels = {unstable: '等待文件稳定', pending: '等待 / 继续分析', complete: '分析完成', error: '分析失败，将重试'};
let toastTimer;
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 6500); }
function duration(value) { value = Math.max(0, Math.floor(value || 0)); return (value >= 3600 ? String(Math.floor(value / 3600)).padStart(2,'0') + ':' : '') + String(Math.floor(value / 60) % 60).padStart(2,'0') + ':' + String(value % 60).padStart(2,'0'); }
function clipDuration(clip) { return Timeline.duration(clip); }
function sameSource(a, b) { return a && b && a.id === b.id && a.generation === b.generation; }
function mergeDuration(clip, observation = {}) {
  const key = clip.generation ? clip.id + ':' + clip.generation : null, known = key ? knownDurations.get(key) : null;
  const valid = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const media = [clip.media_duration, observation.media_duration, known?.media_duration].filter(valid);
  const bounds = [clipDuration(clip), observation.playable_duration, known?.playable_duration, ...media].filter(valid);
  const result = {...clip, media_duration:media.length ? Math.min(...media) : null, playable_duration:Math.min(...bounds)};
  if (key) knownDurations.set(key, {media_duration:result.media_duration, playable_duration:result.playable_duration});
  return result;
}
function adoptDuration(clip, observation) {
  const measured = mergeDuration(clip, observation), fields = {media_duration:measured.media_duration, playable_duration:measured.playable_duration};
  if (sameSource(state.current, clip)) Object.assign(state.current, fields);
  for (const listed of state.clips.filter(c => sameSource(c, clip))) Object.assign(listed, fields);
  return measured;
}
function clock(epoch, seconds = false) { return Timeline.clock(epoch).slice(0, seconds ? 8 : 5); }
function moment(epoch) { return Timeline.day(epoch) + ' ' + clock(epoch, true); }
function selectedChannel() { return $('channel').value || state.current?.channel || ''; }
function element(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
async function api(path, data) {
  const response = await fetch(appBase + '/api/' + path, data === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
  if (response.status === 401) {
    $('login').hidden = false; $('app').hidden = true;
    state.token++; state.selectionToken++; state.filterToken++; clearPlayback();
  }
  let result; try { result = await response.json(); } catch { throw new Error('服务暂时不可用'); }
  if (!response.ok) throw new Error(result.error || '请求失败');
  return result;
}
function filterQuery() { return new URLSearchParams({date: $('day').value, channel: $('channel').value}).toString(); }
function renderClips() {
  $('clips').replaceChildren(); $('list-count').textContent = state.clips.length;
  if (!state.clips.length) $('clips').append(element('p','empty-state','当前筛选下还没有录像。\n后台会自动发现新增文件。'));
  for (const clip of state.clips) {
    const button = element('button', 'clip-card' + (state.current?.id === clip.id ? ' active' : ''));
    button.type = 'button'; button.setAttribute('aria-label', clip.channel + ' ' + clock(clip.start));
    if (clip.thumbnail) { const image = element('img','clip-image'); image.src = clip.thumbnail; image.alt = ''; image.loading = 'lazy'; button.append(image); }
    else button.append(element('span','clip-image image-placeholder','◉'));
    const info = element('span'); info.append(element('strong','clip-time',clock(clip.start) + ' — ' + clock(Timeline.end(clip))), element('span','clip-meta',clip.channel + ' · ' + (clip.media_duration !== null ? '实长 ' : '') + duration(clipDuration(clip))), element('span','clip-state' + (clip.status === 'complete' ? '' : ' pending'), clip.status === 'pending' ? '已分析 ' + clip.progress + '%' : labels[clip.status]));
    button.append(info); button.onclick = () => selectClip(clip, 0); $('clips').append(button);
  }
}
function renderEvents() {
  $('events').replaceChildren(); $('event-count').textContent = state.events.length;
  if (!state.events.length) {
    const pending = state.clips.some(c => c.status !== 'complete');
    $('events').append(element('p','empty-state', pending ? '本次筛选尚无人物结果；仍有录像未分析完成。' : state.clips.length ? '已分析的抽样帧中，未检测到人物出现。' : '选择日期与通道后，人物片段会显示在这里。'));
  }
  for (const event of state.events) {
    const button = element('button','event-card');
    if (event.thumb) { const image = element('img'); image.src = event.thumb; image.alt = '人物出现时的抽样画面'; image.loading = 'lazy'; button.append(image); }
    const info = element('div'); info.append(element('strong','', clock(event.start + event.start_offset, true) + ' · ' + duration(event.end_offset - event.start_offset)), element('small','',event.channel + ' · ' + Math.round(event.score * 100) + '%'));
    button.append(info); button.onclick = () => { const clip = state.clips.find(c => c.id === event.clip_id); if (clip) selectClip(clip, event.start_offset); }; $('events').append(button);
  }
  renderMarkers();
}
function renderMarkers() {
  $('event-markers').replaceChildren(); $('coverage-markers').replaceChildren();
  const start = Timeline.dayStart($('day').value), channel = selectedChannel();
  $('seek').max = 86399; $('seek').disabled = start === null;
  if (start === null) return;
  for (const range of Timeline.coverage(state.clips, $('day').value, channel)) {
    const marker = element('span'); marker.style.left = (range.start / 86400 * 100) + '%'; marker.style.width = ((range.end - range.start) / 86400 * 100) + '%'; $('coverage-markers').append(marker);
  }
  const playable = new Set(state.clips.filter(Timeline.playable).map(c => c.id));
  for (const event of state.events.filter(e => playable.has(e.clip_id) && (!channel || e.channel === channel))) {
    const from = Math.max(0, event.start + event.start_offset - start), to = Math.min(86400, event.start + event.end_offset - start);
    if (to <= from) continue;
    const marker = element('span'); marker.style.left = (from / 86400 * 100) + '%'; marker.style.width = ((to - from) / 86400 * 100) + '%'; $('event-markers').append(marker);
  }
}
function playerMessage(text) { $('player-message').replaceChildren(element('span','empty-icon','▷'), element('span','',text)); $('player-message').hidden = false; }
function selectedHeader() {
  const clip = state.current; if (!clip) return;
  $('current-channel').textContent = clip.channel;
  $('current-title').textContent = moment(clip.start) + ' — ' + moment(Timeline.end(clip));
  $('clip-analysis').textContent = labels[clip.status] + ' · ' + clip.progress + '%' + (clip.media_duration !== null && Math.abs(clip.duration - clipDuration(clip)) > .5 ? ' · 实际可播 ' + duration(clipDuration(clip)) + ' / 文件标注 ' + duration(clip.duration) : '');
  const index = state.clips.findIndex(c => c.id === clip.id); $('previous').disabled = index <= 0; $('next').disabled = index < 0 || index >= state.clips.length - 1;
  renderMarkers(); renderClips();
}
async function selectClip(clip, offset = 0) {
  clip = mergeDuration(clip);
  if (!Timeline.playable(clip)) { toast('录像仍在写入，文件稳定后即可播放。'); return; }
  offset = Math.max(0, Math.min(offset, clipDuration(clip) - .001));
  if (seekLoaded(clip, offset)) return;
  state.selectionToken++; clearSuccessor(); segmentIndex = 0;
  state.current = clip;
  offset = Math.max(0, Math.min(offset, clipDuration(clip) - .001));
  syncDay(clip.start + offset); $('jump-date').value = Timeline.day(clip.start + offset); $('jump-time').value = clock(clip.start + offset, true);
  selectedHeader(); await playSegment(offset);
}
function syncDay(epoch) {
  const date = Timeline.day(epoch);
  if ($('day').value === date) return;
  $('jump-date').value = date;
  if (!Array.from($('day').options).some(o => o.value === date)) { const option = element('option','',date); option.value = date; $('day').append(option); }
  $('day').value = date;
  refreshClips().catch(e => toast(e.message));
}
function updatePosition(offset) {
  if (!state.current) return;
  state.position = Math.max(0, Math.min(clipDuration(state.current), offset));
  const epoch = state.current.start + state.position;
  syncDay(epoch);
  $('seek').value = Math.min(86399, Math.max(0, epoch - Timeline.dayStart($('day').value)));
  $('position').textContent = moment(epoch) + ' 北京时间';
}
async function channelClips(channel) {
  const clips = await api('clips?' + new URLSearchParams({channel}).toString());
  return clips.map(clip => mergeDuration(clip));
}
function reportGap(result) {
  if (result.gap > .001) toast('已跳过无录像时段 ' + (result.gap < 1 ? result.gap.toFixed(1) + ' 秒' : duration(result.gap)) + '，从 ' + moment(result.clip.start) + ' 继续');
}
async function seekTime(epoch) {
  const channel = selectedChannel();
  if (!channel) { toast('请先选择摄像头 / 通道，再按北京时间定位。'); return; }
  const local = loadedMedia?.clip;
  if (local && local.channel === channel && sameSource(local, state.current) && seekLoaded(state.current, epoch - local.start)) return;
  const selection = ++state.selectionToken; state.token++; clearSuccessor(); clearPending(); $('video').pause();
  state.seekingToken = selection;
  try {
    const clips = await channelClips(channel);
    if (selection !== state.selectionToken) return;
    const result = Timeline.locate(clips, epoch, channel, state.current?.id);
    if (!result) { toast('该通道在此时刻及之后无可播放录像。'); return; }
    reportGap(result); await selectClip(result.clip, result.offset);
  } catch (error) { if (selection === state.selectionToken) toast(error.message + '，请重新跳转重试'); }
  finally {
    if (state.seekingToken === selection) state.seekingToken = null;
    // A missing target or catalog error keeps the old, paused recording usable.
    if (selection === state.selectionToken && loadedMedia?.clip && sameSource(loadedMedia.clip, state.current) && !pendingMedia)
      loadedMedia.token = state.token;
  }
}
async function continuePlayback() {
  const current = state.current; if (!current || state.endingToken === state.selectionToken || state.seekingToken === state.selectionToken) return;
  const selection = ++state.selectionToken;
  state.endingToken = selection;
  try {
    const clips = await channelClips(current.channel);
    if (selection !== state.selectionToken) return;
    const bounded = mergeDuration(current);
    const result = Timeline.continueFrom(clips, bounded, Timeline.end(bounded));
    if (!result) { toast('此通道已播放到最后一段，无后续可播放录像。'); return; }
    reportGap(result); await selectClip(result.clip, result.offset);
  } catch (error) { if (selection === state.selectionToken) toast(error.message + '，请重新跳转重试'); }
  finally { if (state.endingToken === selection) state.endingToken = null; }
}
async function previewJob(clip, offset, seconds, prefetch, valid, reuse = true) {
  let job = await api('preview', {clip_id:clip.id, offset, seconds, prefetch, reuse});
  while (valid() && (job.status === 'pending' || job.status === 'processing')) {
    if (job.generation !== clip.generation) throw new Error('录像版本已更新，请重新选择录像');
    await new Promise(resolve => setTimeout(resolve, 500));
    if (!valid()) return null;
    job = await api('preview/' + job.id);
  }
  if (!valid()) return null;
  if (job.generation !== clip.generation) throw new Error('录像版本已更新，请重新选择录像');
  if (job.status !== 'ready' || !job.url) throw new Error(job.error || '画面转换失败，请稍后重试');
  const start = Number.isFinite(job.offset) ? job.offset : offset;
  const end = Math.min(start + job.duration, clipDuration(mergeDuration(clip, job)));
  const minimum = Math.min(seconds, 4, Math.max(0, clipDuration(mergeDuration(clip, job)) - offset));
  if (start > offset + .001 || end - offset < minimum - .001) {
    if (reuse) return previewJob(clip, offset, seconds, prefetch, valid, false);
    throw new Error('返回画面未覆盖定位时刻，请重新选择录像');
  }
  return job;
}
function staleSuccessor(prepared, clip = prepared.clip) {
  const observation = clip && catalogObservations.get(clip.id);
  return observation && observation.revision > prepared.catalogRevision && !sameSource(observation.clip, clip);
}
function prepareSuccessor() {
  if (state.direct || !state.current || pendingMedia || !$('video').getAttribute('src') || state.seekingToken === state.selectionToken) return;
  const token = state.token, selection = state.selectionToken, current = state.current;
  if (successor?.token === token) return;
  const prepared = successor = {token, selection, catalogRevision, clip:null, offset:null, job:null};
  const valid = () => successor === prepared && token === state.token && selection === state.selectionToken && sameSource(state.current, current);
  prepared.promise = (async () => {
    const offset = state.segment + state.segmentDuration;
    let result;
    if (offset < clipDuration(current) - .001) result = {clip:current, offset};
    else {
      const clips = await channelClips(current.channel);
      if (!valid()) return;
      result = Timeline.continueFrom(clips, mergeDuration(current), Timeline.end(mergeDuration(current)));
    }
    if (!result || !valid()) return;
    if (staleSuccessor(prepared, result.clip)) { clearSuccessor(); return; }
    prepared.clip = result.clip; prepared.offset = result.offset;
    prepared.seconds = previewLength(segmentIndex + 1);
    prepared.result = result;
    prepared.job = await previewJob(result.clip, result.offset, prepared.seconds, true, valid);
    if (!valid() || !prepared.job || staleSuccessor(prepared)) return;
    prepared.media = replacementMedia(); prepared.media.preload = 'auto';
    prepared.media.onerror = () => {
      if (!valid() || !prepared.media) return;
      const broken = prepared.media; prepared.media = null; releaseMedia(broken);
    };
    prepared.media.src = prepared.job.url; prepared.media.load();
  })().catch(() => { prepared.failed = true; });
}
async function playSegment(offset, preparedJob = null, seconds = previewLength(0), preparedMedia = null, resume = true) {
  if (!state.current) return;
  const token = ++state.token, clip = state.current;
  clearSuccessor(preparedMedia); clearPending(preparedMedia);
  $('video').pause(); state.direct = false; state.segment = offset;
  updatePosition(offset); $('playback-mode').textContent = '兼容播放';
  if (clip.status === 'unstable') { playerMessage('录像仍在写入，文件稳定后即可播放。'); return; }
  // An existing picture remains visible during encoding and media buffering.
  if (!$('video').getAttribute('src')) playerMessage('正在准备兼容画面… 首次转换需要一点时间');
  try {
    const valid = () => token === state.token && sameSource(state.current, clip);
    const job = preparedJob || await previewJob(clip, offset, seconds, false, valid);
    if (!valid() || !job) { if (preparedMedia) releaseMedia(preparedMedia); return; }
    if (job.generation !== clip.generation) throw new Error('录像版本已更新，请重新选择录像');
    adoptDuration(clip, job); selectedHeader();
    state.segment = Number.isFinite(job.offset) ? job.offset : offset;
    state.segmentDuration = Math.max(0, Math.min(job.duration, clipDuration(state.current) - state.segment));
    if (!state.segmentDuration || state.segment + state.segmentDuration <= offset + .001) { await continuePlayback(); return; }
    stageMedia(clip, offset, state.segment, state.segmentDuration, job.url, token, false, preparedMedia, resume);
  } catch (error) { if (token === state.token) { playerMessage(error.message + '（拖动时间轴或重新点击录像可重试）'); toast(error.message); } }
}
async function advanceSegment() {
  const prepared = successor;
  const valid = prepared && prepared.token === state.token && prepared.selection === state.selectionToken && !staleSuccessor(prepared);
  if (valid && prepared.clip) {
    const clip = prepared.clip, offset = prepared.offset;
    // A not-yet-ready speculative job is reused and promoted by the foreground POST.
    if (!sameSource(clip, state.current)) {
      state.current = mergeDuration(clip); state.selectionToken++;
      syncDay(clip.start + offset); selectedHeader();
      reportGap(prepared.result);
    }
    segmentIndex++;
    return playSegment(offset, prepared.job, prepared.seconds, prepared.media);
  }
  const next = state.segment + state.segmentDuration;
  if (next < clipDuration(state.current) - .001) { segmentIndex++; return playSegment(next, null, previewLength(segmentIndex)); }
  return continuePlayback();
}
function nextClip(delta) {
  const index = state.clips.findIndex(c => c.id === state.current?.id); const clip = state.clips[index + delta]; if (clip) selectClip(clip);
}
function adoptOriginalDuration(...observations) {
  const clip = state.current; if (!clip) return;
  const values = [clip.media_duration, ...observations].filter(value => typeof value === 'number' && Number.isFinite(value) && value > 0);
  if (!values.length) return;
  adoptDuration(clip, {media_duration:Math.min(...values)});
  selectedHeader();
}
async function refreshClips() {
  const token = ++state.filterToken, selection = state.selectionToken, query = filterQuery();
  let clips, events;
  try { [clips, events] = await Promise.all([api('clips?' + query), api('events?' + query)]); }
  catch (error) { if (token === state.filterToken) throw error; return; }
  if (token !== state.filterToken) return;
  const selected = clips.find(c => c.id === state.current?.id);
  if (selected && selection !== state.selectionToken && !sameSource(selected, state.current)) return;
  catalogRevision++;
  for (const clip of clips) catalogObservations.set(clip.id, {clip, revision:catalogRevision});
  state.clips = clips.map(clip => mergeDuration(clip)); state.events = events;
  if (successor?.clip) {
    const refreshed = state.clips.find(c => c.id === successor.clip.id);
    if (refreshed && !sameSource(refreshed, successor.clip)) clearSuccessor();
  }
  if (state.current) {
    const current = state.clips.find(c => c.id === state.current.id);
    if (current) {
      if (!sameSource(current, state.current)) {
        state.token++; state.selectionToken++; clearPlayback();
        playerMessage('录像版本已更新，请重新选择录像。');
      }
      state.current = current;
    }
    selectedHeader();
  }
  renderClips(); renderEvents();
}
async function refreshStatus() {
  const status = await api('status'); state.paused = status.paused;
  $('clip-count').textContent = status.clip_count; $('timezone').textContent = '北京时间 UTC+08:00';
  $('worker-badge').textContent = status.worker_alive ? '● 后台在线' : '● 后台未连接'; $('worker-badge').className = 'chip ' + (status.worker_alive ? 'online' : 'warn');
  const percent = status.total_seconds ? (status.completed_seconds / status.total_seconds * 100).toFixed(1) : '0.0';
  $('analysis-title').textContent = status.paused ? '人物索引已暂停' : '人物索引';
  $('analysis-detail').textContent = `${percent}% · ${duration(status.completed_seconds)} / ${duration(status.total_seconds)} 源时间 · 播放队列 ${(status.queue.pending || 0) + (status.queue.processing || 0)}`;
  $('overall-progress').style.width = percent + '%'; $('pause').textContent = status.paused ? '继续分析' : '暂停分析';
  $('system-error').textContent = [status.worker_error, status.scan_error ? '录像扫描异常（保留已有索引）：' + status.scan_error : null, ...status.errors.map(e => '录像分析异常：' + e.error), ...(status.thumbnail_errors || []).map(e => '封面生成异常（独立重试）：' + e.error)].filter(Boolean).join('\n');
}
async function initialize() {
  await refreshStatus(); $('login').hidden = true; $('app').hidden = false;
  await refreshFilters(); await refreshClips();
}
async function refreshFilters() {
  const [days, channels] = await Promise.all([api('days'), api('channels')]);
  if ($('day').value && !days.includes($('day').value)) days.push($('day').value);
  days.sort().reverse();
  for (const [id, values] of [['day', days.length ? days : ['']], ['channel', ['', ...channels]]]) {
    const select = $(id), selected = select.value;
    if (JSON.stringify(Array.from(select.options, option => option.value)) === JSON.stringify(values)) continue;
    select.replaceChildren();
    for (const value of values) { const option = element('option','',value || (id === 'day' ? '暂无录像' : '全部通道')); option.value = value; select.append(option); }
    if (values.includes(selected)) select.value = selected;
  }
  if (!$('jump-date').value) $('jump-date').value = $('day').value;
}
$('login-form').hidden = dsmMode; $('dsm-login').hidden = !dsmMode; $('dsm-signin').href = '/?launchApp=com.xiaomi.camera.review';
if (dsmMode) $('logout').textContent = '返回 DSM';
$('login-form').onsubmit = async event => { event.preventDefault(); if (dsmMode) { location.href = '/'; return; } $('login-error').textContent = ''; try { await api('login', {password: $('password').value}); $('password').value = ''; await initialize(); } catch (error) { $('login-error').textContent = error.message; } };
$('logout').onclick = async () => { state.token++; state.selectionToken++; state.filterToken++; clearPlayback(); if (dsmMode) { location.href = '/'; return; } try { await api('logout', {}); } finally { $('app').hidden = true; $('login').hidden = false; } };
for (const id of ['day', 'channel']) $(id).onchange = () => {
  // Cancel immediately, before the new date/channel response can arrive.
  state.token++; state.selectionToken++; clearPlayback();
  if (id === 'channel' && $('channel').value && state.current?.channel !== $('channel').value) state.current = null;
  $('jump-date').value = $('day').value;
  playerMessage('选择录像或输入北京时间，开始回看'); renderMarkers();
  return refreshClips().catch(e => toast(e.message));
};
$('jump-form').onsubmit = event => {
  event.preventDefault();
  const epoch = Timeline.parse($('jump-date').value, $('jump-time').value);
  if (epoch === null) { toast('请输入有效的日期和北京时间（时:分:秒）。'); return; }
  return seekTime(epoch);
};
$('refresh').onclick = () => initialize().catch(e => toast(e.message));
$('pause').onclick = async () => { try { await api(state.paused ? 'resume' : 'pause', {}); await refreshStatus(); } catch (e) { toast(e.message); } };
$('previous').onclick = () => nextClip(-1); $('next').onclick = () => nextClip(1);
$('seek').oninput = () => { const start = Timeline.dayStart($('day').value); if (start !== null) $('position').textContent = moment(start + Number($('seek').value)) + ' 北京时间'; };
$('seek').onchange = () => { const start = Timeline.dayStart($('day').value); if (start !== null) return seekTime(start + Number($('seek').value)); };
loadedMedia = {media:$('video'), token:state.token, clip:null};
bindMedia(loadedMedia);
$('original').onclick = () => {
  if (!state.current || !Timeline.playable(state.current)) return;
  const old = $('video'), resume = !old.getAttribute('src') || !old.paused;
  const token = ++state.token, offset = state.position; state.selectionToken++;
  clearSuccessor(); clearPending(); old.pause(); state.direct = true;
  stageMedia(state.current, offset, 0, clipDuration(state.current), appBase + '/api/original/' + state.current.id, token, true, null, resume);
  $('playback-mode').textContent = '原始画质';
};
$('compatible').onclick = () => { const resume = !$('video').paused; state.selectionToken++; return playSegment(state.position, null, previewLength(0), null, resume); };
initialize().catch(() => {});
setInterval(() => { if (!$('app').hidden) Promise.all([refreshStatus(), refreshFilters().then(refreshClips)]).catch(e => toast(e.message)); }, 15000);
