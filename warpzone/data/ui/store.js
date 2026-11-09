/*
 * warpzone library UI: the data the page shows and the changes it sends.
 *
 * State lives here, in memory only. All decisions (what a change does, what it would download,
 * which error applies) belong to the API in development/design/library-ui.md; this file loads
 * the answers, keeps them in step after a write and sends the changes. The views (app.js) draw
 * what is here and register the few hooks below to be told when it changed.
 */
(function () {
  'use strict';

  const { ApiError, api, humanError, h, $, plural, throttle, systemName } = window.WZ;

  // ===== state =============================================================================

  const S = {
    devices: [],            // [{name, profile, exclude_systems, whitelist: {state, etag}}]
    systems: [],            // [{system, games, size, devices: {name: {state, whole}}}]
    etags: {},              // device -> the whitelist etag the page last saw
    lists: new Map(),       // system -> {games: [...], byId: Map}
    listLoads: new Map(),   // system -> promise (dedupes loads)
    unclaimed: new Map(),   // system -> update/DLC folders no game claims (Switch)
    byId: new Map(),        // game id -> {game, system}
    progress: {},           // device -> progress entry
    progressError: false,   // false, 'hub' or 'server'
    catalog: null,          // {ready, running, last_run, systems, games, updated} from /catalog
    recent: null,           // the games played most recently (/recent), or null when there are none
    ra: null,               // the RetroAchievements records (/ra/summary), or null when there are none
    searchResult: null,     // {q, groups} from the API
    detail: null,           // {id, data}: the game page being shown
    busy: false,
  };

  // What the views register. The defaults do nothing, so the store works without a page.
  const hooks = {
    refresh() {},           // the data a view shows changed
    devicesChanged() {},    // the consoles (their whitelist state or the list) changed
    listsChanged() {},      // more game lists arrived
    progressChanged() {},   // a progress answer (or failure) arrived
    detailChanged(id) {},   // a write touched this game: load its page data again
    confirmSystem() { return Promise.resolve(false); },
    reset() {},             // the whole catalog was loaded again: drop what was derived from it
  };

  const ensureArray = (x, key) => (Array.isArray(x) ? x : x && Array.isArray(x[key]) ? x[key] : []);

  // ===== toasts and errors =================================================================

  function toast(message, kind) {
    const box = $('#toasts');
    const close = h('button', { class: 'toast-close', type: 'button', 'aria-label': 'Dismiss' }, 'Dismiss');
    const el = h('div', { class: 'toast ' + (kind || 'info'), role: kind === 'error' ? 'alert' : 'status' },
      h('p', null, message), close);
    const remove = () => {
      el.remove();
      if (!box.children.length && box.hidePopover) { try { box.hidePopover(); } catch (_) { /* not open */ } }
    };
    close.addEventListener('click', remove);
    box.append(el);
    // Show the popover again on each toast so it sits above whatever dialog opened last.
    if (box.showPopover) {
      try { box.hidePopover(); } catch (_) { /* not open */ }
      try { box.showPopover(); } catch (_) { /* unsupported */ }
    }
    setTimeout(remove, kind === 'error' ? 14000 : 6000);
    while (box.children.length > 4) box.firstElementChild.remove();
  }

  async function handleError(err, quiet) {
    if (err && err.name === 'AbortError') return;
    if (!(err instanceof ApiError)) { console.error(err); toast('Something unexpected went wrong.', 'error'); return; }
    if (err.code === 'unauthorized') return; // the login form is already open
    if (!quiet) toast(humanError(err), 'error');
    if (['stale', 'expect-missing', 'not-found', 'cannot-apply', 'not-managed', 'covered'].includes(err.code) ||
        (err.status >= 500 && err.info && err.info.body && err.info.body.devices)) {
      await reloadAll();
    }
  }

  // ===== loading ===========================================================================

  async function loadDevices() {
    const list = ensureArray(await api.devices(), 'devices');
    S.devices = list.map((d) => ({ ...d, exclude_systems: d.exclude_systems || [] }));
    for (const d of S.devices) {
      if (d.whitelist && d.whitelist.etag) S.etags[d.name] = d.whitelist.etag;
    }
  }

  // The catalog's own state: built yet, a refresh running. A server that does not answer it
  // (or an error) leaves S.catalog null and the page behaves as if the catalog were ready.
  async function loadCatalog() {
    try { S.catalog = await api.catalog(); } catch (err) { if (err.code === 'unauthorized') throw err; S.catalog = null; }
  }

  // What the home page shows beyond the catalog: the recently played games and the records. Both
  // are optional (no ledgers, no RetroAchievements user): a failure or an empty answer leaves the
  // field null and the page shows nothing for it. Asked again at most every 30 seconds; resolves
  // true when it asked.
  let homeAskedAt = 0;
  const orNull = (promise) => promise.catch(() => null);

  async function loadHomeExtras(force) {
    if (!force && Date.now() - homeAskedAt < 30000) return false;
    homeAskedAt = Date.now();
    const [recent, summary] = await Promise.all([orNull(api.recent(12)), orNull(api.raSummary())]);
    const games = recent && Array.isArray(recent.games) ? recent.games.filter((g) => g && g.id && g.system) : [];
    S.recent = games.length ? games : null;
    S.ra = summary && summary.total && summary.user && summary.fetched ? summary : null;
    return true;
  }

  async function loadSystems() {
    S.systems = ensureArray(await api.systems(), 'systems').sort((a, b) =>
      systemName(a.system).localeCompare(systemName(b.system)));
  }

  function indexList(system, games) {
    const old = S.lists.get(system);
    if (old) for (const g of old.games) S.byId.delete(g.id);
    const byId = new Map();
    for (const g of games) {
      g.devices = g.devices || {};
      byId.set(g.id, g);
      S.byId.set(g.id, { game: g, system });
    }
    S.lists.set(system, { games, byId });
  }

  const listsChanged = throttle(() => hooks.listsChanged(), 350);

  function ensureList(system, force) {
    if (!force && S.lists.has(system)) return Promise.resolve(S.lists.get(system));
    if (!force && S.listLoads.has(system)) return S.listLoads.get(system);
    const p = api.games(system).then((r) => {
      indexList(system, ensureArray(r, 'games'));
      S.unclaimed.set(system, (r && r.unclaimed) || []);
      S.listLoads.delete(system);
      listsChanged();
      return S.lists.get(system);
    }, (err) => { S.listLoads.delete(system); throw err; });
    S.listLoads.set(system, p);
    return p;
  }

  // Load every system in the background (three at a time): the home page, the coverage bars
  // and the instant search need them. A failure just leaves that system out until the next
  // reload.
  async function preloadLists() {
    const names = S.systems.map((s) => s.system).filter((n) => !S.lists.has(n));
    let next = 0;
    const worker = async () => {
      while (next < names.length) {
        const name = names[next++];
        try { await ensureList(name); } catch (err) { if (err.code === 'unauthorized') return; }
      }
    };
    await Promise.all([worker(), worker(), worker()]);
  }

  async function reloadAll() {
    S.lists.clear();
    S.byId.clear();
    S.listLoads.clear();
    try { await Promise.all([loadDevices(), loadSystems(), loadCatalog()]); } catch (err) { await handleError(err, true); return; }
    hooks.reset();
    hooks.devicesChanged();
    hooks.refresh();
    preloadLists();
  }

  // ===== what the data says ================================================================

  const device = (name) => S.devices.find((x) => x.name === name);
  const system = (name) => S.systems.find((s) => s.system === name);

  function excludes(deviceName, sys) {
    const d = device(deviceName);
    return !!d && d.exclude_systems.includes(sys);
  }

  // A whitelist the UI may write: one in its format, or none yet (the first change creates it).
  // `needs-migration` is read-only until `warpzone whitelist migrate` has converted it.
  function isManaged(deviceName) {
    const d = device(deviceName);
    return !d || !d.whitelist || d.whitelist.state === 'managed' || d.whitelist.state === 'missing';
  }

  // Whether a change for every console can be sent: a console that needs migration would make
  // the server refuse the whole request, so the all-consoles controls wait for the migration.
  function allBlocked(sys) {
    return S.devices.some((d) => !excludes(d.name, sys) && !isManaged(d.name));
  }

  // The state of one game on one console, from whichever copy of the game is at hand.
  function gameState(devices, deviceName, sys) {
    if (excludes(deviceName, sys) || (devices && devices[deviceName] === 'unsupported')) return 'unsupported';
    return (devices && devices[deviceName]) || 'off';
  }

  // How much of a system a console has: its state from /systems, and the counts once the list
  // of the system is loaded (the aggregate has the state only).
  function coverage(sys, deviceName) {
    const entry = system(sys);
    const info = (entry && entry.devices && entry.devices[deviceName]) || { state: 'none', whole: false };
    const state = excludes(deviceName, sys) ? 'unsupported' : info.state || 'none';
    const out = { state, whole: !!info.whole, total: entry ? entry.games : 0, n: null, size: null, of: entry ? entry.size : 0 };
    const list = S.lists.get(sys);
    if (list) {
      let n = 0;
      let size = 0;
      for (const g of list.games) if (g.devices[deviceName] === 'on') { n++; size += g.size || 0; }
      out.n = n;
      out.size = size;
    } else if (state === 'all') { out.n = out.total; out.size = out.of; } else if (state === 'none') { out.n = 0; out.size = 0; }
    return out;
  }

  // What a console holds across every system whose list is loaded: {games, size, complete}.
  function holdings(deviceName) {
    let games = 0;
    let size = 0;
    let loaded = 0;
    for (const [sys, list] of S.lists) {
      if (excludes(deviceName, sys)) { loaded++; continue; }
      loaded++;
      for (const g of list.games) if (g.devices[deviceName] === 'on') { games++; size += g.size || 0; }
    }
    return { games, size, complete: loaded >= S.systems.length };
  }

  // After a write the cached copies of a game are patched, so nothing waits for a reload.
  function patchState(id, deviceName, state) {
    const known = S.byId.get(id);
    if (known) known.game.devices[deviceName] = state;
    if (S.detail && S.detail.id === id && S.detail.data) S.detail.data.devices[deviceName] = state;
    if (S.searchResult) {
      for (const group of S.searchResult.groups) {
        for (const g of group.games) if (g.id === id) { g.devices = g.devices || {}; g.devices[deviceName] = state; }
      }
    }
  }

  // ===== progress ==========================================================================

  let progressTimer = null;

  function schedulePoll(ms) {
    clearTimeout(progressTimer);
    progressTimer = setTimeout(pollProgress, ms);
  }

  const progressSoon = () => schedulePoll(300);

  async function pollProgress() {
    if (document.hidden) { schedulePoll(30000); return; }
    let busy = false;
    try {
      const list = ensureArray(await api.progress(), 'devices');
      S.progress = {};
      for (const p of list) S.progress[p.device] = p;
      S.progressError = false;
      busy = list.some((p) => p.state === 'pending' || p.state === 'downloading');
    } catch (err) {
      if (err.code === 'unauthorized') return; // the login form is up; boot() restarts polling
      S.progressError = err.code === 'hub-unreachable' ? 'hub' : 'server';
    }
    hooks.progressChanged();
    // Fast while something is pending or downloading; a slow poll otherwise, so a console that
    // comes online or finishes is noticed without a reload.
    schedulePoll(busy ? 4000 : S.progressError ? 20000 : 45000);
  }

  // ===== changes ===========================================================================

  function setBusy(on) {
    S.busy = on;
    document.body.classList.toggle('busy', on);
  }

  // Serialise writes: a second click while one runs would carry an etag the first just changed.
  async function guarded(fn) {
    if (S.busy) { toast('Another change is still being applied.', 'info'); return null; }
    setBusy(true);
    try { return await fn(); } catch (err) { await handleError(err); return null; } finally { setBusy(false); }
  }

  // The consoles a change touches, and the etag the server must find for each of them.
  function targets(deviceName, sys) {
    if (deviceName === '*') return S.devices.filter((d) => !excludes(d.name, sys)).map((d) => d.name);
    return [deviceName];
  }

  function expectFor(names) {
    const out = {};
    for (const n of names) if (S.etags[n]) out[n] = S.etags[n];
    return out;
  }

  // After a write: remember the new etags, patch the cached copies with what changed, then
  // reload the aggregates (counts and whole-system states) and start watching progress.
  async function afterWrite(res) {
    for (const d of (res && res.devices) || []) {
      if (d.etag) S.etags[d.device] = d.etag;
      for (const id of d.on || []) patchState(id, d.device, 'on');
      for (const id of d.off || []) patchState(id, d.device, 'off');
    }
    await Promise.all([loadDevices(), loadSystems()]);
    hooks.devicesChanged();
    hooks.refresh();
    progressSoon();
  }

  function skippedNote(res) {
    const sk = (res && res.skipped) || [];
    if (!sk.length) return '';
    return ' Skipped: ' + sk.map((s) => `${s.device} (${s.reason})`).join(', ') + '.';
  }

  // One game on or off for one console (or '*' for all). No confirmation: it is one game.
  async function changeGame(game, sys, deviceName, on) {
    await guarded(async () => {
      const names = targets(deviceName, sys);
      const res = await api.changes({
        changes: [{ device: deviceName, system: sys, game: game.id, on }],
        expect: expectFor(names), dry_run: false,
      });
      await afterWrite(res);
      const where = deviceName === '*' ? 'all consoles' : deviceName;
      toast(`${on ? 'Added' : 'Removed'} "${game.title}" ${on ? 'to' : 'from'} ${where}.` + skippedNote(res), 'ok');
      hooks.detailChanged(game.id);
    });
  }

  // A whole system: a dry run first, the confirmation shows what it would change, then the write.
  async function changeSystem(sys, deviceName, on, already) {
    if (already) { toast(on ? 'Already on.' : 'Already off.', 'info'); return; }
    await guarded(async () => {
      const names = targets(deviceName, sys);
      const body = { changes: [{ device: deviceName, system: sys, on }], expect: expectFor(names) };
      const dry = await api.changes({ ...body, dry_run: true });
      const ok = await hooks.confirmSystem(sys, deviceName, on, dry);
      if (!ok) return;
      // The etags may have moved while the dialog was open: use the current ones.
      const res = await api.changes({ ...body, expect: expectFor(names), dry_run: false });
      await afterWrite(res);
      await ensureList(sys, true);
      S.searchResult = null;
      hooks.refresh();
      toast(`${systemName(sys)} is now ${on ? 'on' : 'off'} for ${deviceName === '*' ? 'all consoles' : deviceName}.` + skippedNote(res), 'ok');
    });
  }

  // ===== the catalog scan ==================================================================

  // A refresh runs on the hub for a while: poll /catalog until it ends, then load what it found.
  let catalogTimer = null;

  function watchCatalog(ms) {
    clearTimeout(catalogTimer);
    catalogTimer = setTimeout(pollCatalog, ms);
  }

  async function pollCatalog() {
    const was = !!(S.catalog && S.catalog.running);
    try { await loadCatalog(); } catch (err) { return; }
    const running = !!(S.catalog && S.catalog.running);
    if (was && !running) {
      const last = S.catalog && S.catalog.last_run;
      if (last && last.ok === false) toast(`The catalog scan failed: ${last.message || 'unknown error'}.`, 'error');
      else toast('The catalog scan finished.', 'ok');
      await reloadAll();
    } else {
      hooks.refresh();
    }
    watchCatalog(running ? 3000 : 60000);
  }

  async function refreshCatalog() {
    try {
      await api.refreshCatalog();
      S.catalog = { ...(S.catalog || {}), running: true };
      hooks.refresh();
      toast('A catalog scan started on the hub.', 'ok');
      watchCatalog(1500);
    } catch (err) {
      if (err.code === 'catalog-running') {
        S.catalog = { ...(S.catalog || {}), running: true };
        hooks.refresh();
        toast('A catalog scan is already running.', 'info');
        watchCatalog(1500);
      } else await handleError(err);
    }
  }

  window.WZ.store = {
    S, hooks, toast, handleError, ensureArray,
    loadDevices, loadCatalog, loadSystems, loadHomeExtras, ensureList, preloadLists, reloadAll,
    device, system, excludes, isManaged, allBlocked, gameState, coverage, holdings, patchState,
    schedulePoll, pollProgress, progressSoon,
    changeGame, changeSystem, watchCatalog, refreshCatalog, plural,
  };
})();
