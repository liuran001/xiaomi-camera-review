(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.Timeline = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const DAY = 86400, BEIJING = 8 * 3600;
  function parse(date, time) {
    const d = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date || '');
    const t = /^(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(time || '');
    if (!d || !t) return null;
    const [year, month, day] = d.slice(1).map(Number);
    const [hour, minute, second] = [Number(t[1]), Number(t[2]), Number(t[3] || 0)];
    if (year < 1 || month < 1 || month > 12 || day < 1 || hour > 23 || minute > 59 || second > 59) return null;
    const value = new Date(0);
    value.setUTCFullYear(year, month - 1, day); value.setUTCHours(hour, minute, second, 0);
    if (value.getUTCFullYear() !== year || value.getUTCMonth() !== month - 1 || value.getUTCDate() !== day) return null;
    return value.getTime() / 1000 - BEIJING;
  }
  function dayStart(date) { return parse(date, '00:00:00'); }
  function day(epoch) { return new Date((epoch + BEIJING) * 1000).toISOString().slice(0, 10); }
  function clock(epoch) { return new Date((epoch + BEIJING) * 1000).toISOString().slice(11, 19); }
  function duration(clip) {
    const values = [clip.duration, clip.playable_duration, clip.media_duration].filter(value => typeof value === 'number' && Number.isFinite(value));
    return values.length ? Math.max(0, Math.min(...values)) : 0;
  }
  function end(clip) { return clip.start + duration(clip); }
  function playable(clip) { return clip.status !== 'unstable' && clip.status !== 'missing' && duration(clip) > 0; }
  function locate(clips, epoch, channel, currentId) {
    if (!channel || !Number.isFinite(epoch)) return null;
    const candidates = clips.filter(c => c.channel === channel && playable(c));
    const covering = candidates.filter(c => c.start <= epoch && epoch < end(c));
    const current = covering.find(c => c.id === currentId);
    const clip = current || covering.sort((a, b) => b.start - a.start || a.id.localeCompare(b.id))[0]
      || candidates.filter(c => c.start > epoch).sort((a, b) => a.start - b.start || a.id.localeCompare(b.id))[0];
    return clip ? {clip, offset: Math.max(0, epoch - clip.start), gap: Math.max(0, clip.start - epoch)} : null;
  }
  function continueFrom(clips, current, epoch) {
    return locate(clips.filter(c => c.id !== current.id), epoch, current.channel);
  }
  function coverage(clips, date, channel) {
    const start = dayStart(date); if (start === null) return [];
    return clips.filter(c => playable(c) && (!channel || c.channel === channel) && c.start < start + DAY && end(c) > start)
      .map(c => ({clip:c, start:Math.max(0, c.start - start), end:Math.min(DAY, end(c) - start)}));
  }
  return {parse, dayStart, day, clock, duration, end, playable, locate, continueFrom, coverage};
});
