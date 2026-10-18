/* Presentation only: no trading rule, observation journal, or backtest mutation. */
(function (root) {
  'use strict';
  function selectMarkers(signals, detailed = false, includeInvalid = false) {
    const ordered = signals.slice().sort((a,b) => a.available_at.localeCompare(b.available_at) || a.kind.localeCompare(b.kind));
    const points = new Map();
    const stats = { total: signals.length, candidates: 0, auxiliary: 0, invalid: 0, repeated: 0, merged: 0 };
    for (const s of ordered) {
      const side = /buy/i.test(s.kind) ? 'B' : /sell/i.test(s.kind) ? 'S' : null;
      if (!side) continue;
      if (!s.active) { stats.invalid++; if (!includeInvalid) continue; }
      if (s.status !== 'confirmed') { stats.candidates++; if (!detailed) continue; }
      if (['T06','T07'].includes(s.rule_id)) { stats.auxiliary++; if (!detailed) continue; }
      // Status and provenance remain separate: a candidate can never upgrade to confirmed.
      const key = [s.available_at,side,s.status,!!s.active,s.mode].join('|');
      const existing = points.get(key);
      if (existing) { existing.members.push(s); stats.merged++; }
      else points.set(key, {...s, members: [s], side});
    }
    const selected = [];
    let session = null, direction = null;
    for (const s of points.values()) {
      const next = s.available_at.slice(0,10) + (s.available_at.slice(11,16) <= '11:30' ? '-am' : '-pm');
      if (next !== session) { direction = null; session = next; }
      if (!detailed && s.side === direction) { stats.repeated++; continue; }
      selected.push(s);
      if (s.active && s.status === 'confirmed') direction = s.side;
    }
    return {markers:selected,stats};
  }
  root.OpusIntradayMarkers = {selectMarkers};
  if (typeof module !== 'undefined' && module.exports) module.exports = {selectMarkers};
})(typeof globalThis !== 'undefined' ? globalThis : this);
