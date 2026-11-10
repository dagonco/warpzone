/*
 * warpzone library UI: the API client and the formatting helpers the other scripts share.
 *
 * Every request the page makes goes through this file, so the paths it calls can be checked
 * against the HTTP API table of development/design/library-ui.md (tests/test_ui_files.py does).
 * Nothing here keeps the token: the login sends it once and the server answers with a cookie.
 */
(function () {
  'use strict';

  class ApiError extends Error {
    constructor(status, code, message, info) {
      super(message);
      this.status = status;
      this.code = code;
      this.info = info || {};
    }
  }

  let unauthorizedHook = null;

  async function request(method, path, body, signal) {
    const init = { method, credentials: 'same-origin', headers: { Accept: 'application/json' }, signal };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (err) {
      if (err && err.name === 'AbortError') throw err;
      throw new ApiError(0, 'network', 'The server did not answer.');
    }
    let data = null;
    if (res.status !== 204) {
      const text = await res.text();
      if (text) {
        try { data = JSON.parse(text); } catch (_) { data = null; }
      }
    }
    if (res.ok) return data;
    const e = (data && data.error) || {};
    const err = new ApiError(res.status, e.code || 'http-' + res.status, e.message || res.statusText,
      { device: e.device, game: e.game, queue: e.queue === true, body: data });
    if (res.status === 401 && path !== '/api/v1/session' && unauthorizedHook) unauthorizedHook();
    throw err;
  }

  const api = {
    onUnauthorized(fn) { unauthorizedHook = fn; },
    login: (token) => request('POST', '/api/v1/session', { token }),
    devices: () => request('GET', '/api/v1/devices'),
    systems: () => request('GET', '/api/v1/systems'),
    games: (system) => request('GET', `/api/v1/systems/${encodeURIComponent(system)}/games`),
    game: (id) => request('GET', `/api/v1/games/${encodeURIComponent(id)}`),
    search: (q, signal) => request('GET', `/api/v1/search?q=${encodeURIComponent(q)}`, undefined, signal),
    progress: () => request('GET', '/api/v1/progress'),
    changes: (body) => request('POST', '/api/v1/changes', body),
    backups: (device) => request('GET', `/api/v1/devices/${encodeURIComponent(device)}/backups`),
    backup: (device, name) => request('GET', `/api/v1/devices/${encodeURIComponent(device)}/backups/${encodeURIComponent(name)}`),
    restore: (device, body) => request('POST', `/api/v1/devices/${encodeURIComponent(device)}/restore`, body),
    catalog: () => request('GET', '/api/v1/catalog'),
    refreshCatalog: () => request('POST', '/api/v1/catalog/refresh', {}),
    recent: (limit) => request('GET', `/api/v1/recent?limit=${encodeURIComponent(limit)}`),
    raSummary: () => request('GET', '/api/v1/ra/summary'),
    recommendations: () => request('GET', '/api/v1/recommendations'),
    // The play queue and the one write that changes it; `history` is how many ended entries to list.
    queue: (history) => request('GET', `/api/v1/queue?history=${encodeURIComponent(history)}`),
    queueChange: (body) => request('POST', '/api/v1/queue', body),
    // An image URL, not a request: the browser loads it (lazily) through an <img>.
    mediaUrl: (id, kind) => `/api/v1/games/${encodeURIComponent(id)}/media/${kind}`,
    // The same for a cached RetroAchievements badge; `variant` is 'unlocked' or 'locked'.
    badgeUrl: (badge, variant) => `/api/v1/ra/badges/${encodeURIComponent(badge)}/${variant}`,
  };

  // One human sentence per error code of the design's table, plus the failures with no code.
  function humanError(err) {
    const dev = err.info && err.info.device;
    const where = dev ? ` (console: ${dev})` : '';
    switch (err.code) {
      case 'bad-request':
        return 'The server could not read that request. Reload the page and try again.' + where;
      case 'unauthorized':
        return 'You are signed out. Enter the access token again.';
      case 'bad-origin':
        return 'The server refused the change because the page was opened from a different address than the one it answers on. Open the page from the server address itself.';
      case 'not-found':
        return 'That console, system, game, queue entry or image no longer exists. The page was reloaded.' + where;
      case 'not-managed':
        return `The whitelist of ${dev || 'a console'} is not in the format the UI manages. On the hub run "warpzone whitelist migrate ${dev || '<console>'}" first, then reload.`;
      case 'covered':
        return `A folder-level exclusion of ${dev || 'a console'} keeps part of this game off, so it cannot be turned on from here. Migrate or clean that whitelist first.`;
      case 'cannot-apply':
        return 'That change would not give the result asked for, or it would also change other games, so nothing was written.' + where;
      case 'unlistable':
        return 'A file of this game has a name that cannot be written into a whitelist (a newline or leading/trailing spaces). It cannot be changed from the UI.';
      case 'not-restorable':
        return 'That backup is empty (it was taken before the whitelist existed), so it cannot be restored: an empty whitelist would let every file through.' + where;
      case 'stale':
        if (err.info.queue) return 'The queue changed since this page loaded it (another tab or the command line). Nothing was written; the page reloaded the queue. Check it and try again.';
        return `The whitelist of ${dev || 'a console'} changed since this page loaded it (another tab, the command line or another change). Nothing was written; the page reloaded the current state. Check it and try again.`;
      case 'unsupported':
        return `${dev || 'This console'} does not support that system, so it cannot be turned on there.`;
      case 'expect-missing':
        if (err.info.queue) return 'The page did not know the current version of the queue. It reloaded; try again.';
        return 'The page did not know the current version of a console whitelist. It reloaded; try again.' + where;
      case 'already-queued':
        return 'That game is already in the queue (or the target already has an entry). The page reloaded the queue.';
      case 'not-held':
        return `The queue does not hold that game on ${dev || 'that console'} (it was there before the game was queued, or the game left the library). The page reloaded the queue; keep the console instead of removing the game from it.`;
      case 'in-sync':
        return `${dev || 'That console'} already matches the queue; there was nothing to repair. The page reloaded the queue.`;
      case 'queue-damaged':
        return 'The queue file on the hub cannot be read (it is damaged or written by a newer warpzone), so the queue cannot be changed from here. It is never overwritten: restore one of the files in catalog/queue-backups/ on the hub.';
      case 'queue-full':
        return 'The queue already has 200 active entries. Finish, drop or remove some before adding more.';
      case 'queue-too-large':
        return 'The queue file would pass its size limit (4 MiB), so nothing was written. Remove ended entries from the history or games from collections, then try again.';
      case 'catalog-running':
        return 'A catalog scan is already running on the hub.';
      case 'write-failed':
        if (err.info.queue) {
          return err.info.body && err.info.body.devices
            ? 'The consoles were changed, but the server could not remove the entry from the queue file, so it is still in the queue. Remove it again.'
            : 'The server could not write the queue file. Nothing was changed; check the hub\'s disk and try again.';
        }
        if (err.info.body && err.info.body.queue === true) {
          return `The queue was saved, but the server could not write the whitelist of ${dev || 'a console'}. The entry shows what is left to do: press "Turn on" or "Remove" on it to try again.`;
        }
        return `The server could not write the whitelist of ${dev || 'a console'}. Nothing of it was changed; check the hub's disk and try again.`;
      case 'read-failed':
        return `The server could not read a whitelist${where}. On the hub, check the permissions of the library folder for the user the server runs as.`;
      case 'hub-unreachable':
        return 'The hub could not reach its Syncthing, so progress is unknown for now.';
      case 'internal':
        return 'The server failed in a way it did not expect. Reload the page and try again; the hub has the details in its log.';
      case 'network':
        return 'The server did not answer. Check the connection and try again.';
      default:
        if (err.status === 413) return 'The request was too large.';
        if (err.status >= 500) return 'The server failed to apply the change' + (err.message ? ` (${err.message})` : '') + '. Reload to see what was written.';
        return err.message || 'Something went wrong.';
    }
  }

  // ---- formatting -------------------------------------------------------------------------

  function formatBytes(n) {
    if (n == null || isNaN(n)) return '';
    if (n < 1024) return `${n} B`;
    const units = ['KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    let v = n / 1024;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    const digits = v >= 100 ? 0 : v >= 10 ? 1 : 2;
    let text = v.toFixed(digits);
    if (text.includes('.')) text = text.replace(/\.?0+$/, ''); // "1.50" is "1.5"; "100" stays "100"
    return `${text} ${units[i]}`;
  }

  // A moment given as a UTC epoch (seconds, as the play ledgers have it) or as an ISO 8601 text
  // (as RetroAchievements and the saves have it), as a Date, or null when it is neither.
  function toDate(value) {
    if (value == null || value === '' || value === false) return null;
    const d = typeof value === 'number' ? new Date(value * 1000) : new Date(String(value));
    return isNaN(d.getTime()) ? null : d;
  }

  // "35 min ago", "2 d ago": coarse on purpose. A moment ahead of the clock is "just now".
  function timeAgo(value) {
    const d = toDate(value);
    if (!d) return '';
    const sec = Math.floor((Date.now() - d.getTime()) / 1000);
    if (sec < 60) return 'just now';
    const mins = Math.floor(sec / 60);
    if (mins < 60) return `${mins} min ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours} h ago`;
    const days = Math.floor(hours / 24);
    if (days < 30) return `${days} d ago`;
    if (days < 365) return `${Math.floor(days / 30)} mo ago`;
    return `${Math.floor(days / 365)} y ago`;
  }

  // The full date of a moment, for a title attribute; empty when there is none.
  function formatMoment(value) {
    const d = toDate(value);
    return d ? d.toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' }) : '';
  }

  function formatDay(value) {
    const d = toDate(value);
    return d ? d.toLocaleDateString('en-GB', { dateStyle: 'medium' }) : '';
  }

  // Seconds of play as "17h 00m" (minutes alone below an hour); empty for none.
  function formatPlaytime(seconds) {
    if (typeof seconds !== 'number' || !(seconds > 0)) return '';
    if (seconds < 60) return '<1m';
    const m = Math.floor(seconds / 60);
    return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
  }

  // ES-DE writes dates as 19980903T000000.
  function formatDate(raw) {
    const m = /^(\d{4})(\d{2})(\d{2})/.exec(String(raw || ''));
    if (!m || m[1] === '0000') return '';
    return `${m[1]}-${m[2]}-${m[3]}`;
  }

  // ---- title key --------------------------------------------------------------------------
  // The API owns the grouping (catalog.title_key, and /search). This port exists only so the
  // search box can answer while typing, over the system lists already loaded; the server's
  // answer replaces it as soon as it arrives.

  function normalizeWords(text) {
    let s = text.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase();
    s = s.replace(/['’`]/g, '').replace(/&/g, ' and ');
    return s.replace(/[^\p{L}\p{N}]+/gu, ' ').trim().replace(/\s+/g, ' ');
  }

  function titleKey(title) {
    let s = String(title).replace(/\([^)]*\)|\[[^\]]*\]/g, ' ');
    s = s.replace(/,\s*the(?=\s*(-|:|$))/gi, ''); // "Legend of Zelda, The - ..." form
    return normalizeWords(s).replace(/^the /, '');
  }

  window.WZ = Object.assign(window.WZ || {}, {
    ApiError, api, humanError, formatBytes, formatDate, timeAgo, formatMoment, formatDay, formatPlaytime, normalizeWords, titleKey,
  });
})();
