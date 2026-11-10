/*
 * warpzone library UI.
 *
 * A static page with no build step. It shows the catalog and which console gets which game,
 * and turns games and systems on and off. All decisions (what a change does, what it would
 * download, which error applies) belong to the API in development/design/library-ui.md; the
 * scripts only draw the answers and send the changes:
 *
 *   api.js         the API client, error messages, formatting
 *   kit.js         DOM builder, pixel icons, names and colours of systems and consoles
 *   store.js       the data, the progress poll and the writes
 *   components.js  art, tiles, slots, levers, system cards, console status
 *   views.js       the pages
 *   app.js         this file: the frame (header, rail, bottom bar), router, dialogs, keyboard
 *                  and gamepad navigation, login, boot
 *
 * The page is served with Content-Security-Policy: default-src 'self', so there is no inline
 * script or style and no data: URL. Dynamic styling goes through custom properties set with
 * element.style.setProperty, which the policy allows.
 */
(function () {
  'use strict';

  const W = window.WZ;
  const { humanError, formatBytes, formatMoment, timeAgo, h, $, $$, plural, debounce, icon, tint, systemName, systemCode, systemColor,
    consoleCode, api, store, ui, views } = W;
  const { S, toast } = store;

  const V = {
    route: null,        // {name, system, id, q}
    key: '',            // routeKey(route) of the page shown
    view: null,
    q: '',              // the search box
    pop: false,         // the last navigation was the browser's back or forward
    depth: 0,           // navigations made inside the page since it loaded
    focusMemory: new Map(),
  };

  // ===== the frame =========================================================================

  function renderLeds() {
    $('#leds').replaceChildren(...S.devices.map(ui.led));
    ui.paintProgress();
  }

  function railItem(href, nav, ic, label, kbd) {
    return h('a', { class: 'r-item', href, dataset: { nav, fk: 'r-' + nav } }, icon(ic), h('span', null, label),
      kbd ? h('kbd', { 'aria-hidden': 'true' }, kbd) : null);
  }

  function renderRail() {
    const fk = document.activeElement && document.activeElement.closest && document.activeElement.closest('#rail')
      ? document.activeElement.dataset.fk : null;
    const total = S.systems.reduce((n, s) => n + (s.games || 0), 0);
    const size = S.systems.reduce((n, s) => n + (s.size || 0), 0);
    $('#rail').replaceChildren(h('div', { class: 'rail-in' },
      railItem('#/', 'home', 'home', 'Home'),
      railItem('#/search/', 'search', 'search', 'Search', '/'),
      railItem('#/consoles', 'consoles', 'pad', 'Consoles'),
      h('p', { class: 'r-h' }, h('span', null, 'Systems'),
        h('span', { class: 'r-cons', 'aria-hidden': 'true' }, S.devices.map((d) => h('i', { title: d.name }, consoleCode(d.name)[0])))),
      S.systems.map((s) => h('a', {
        class: 'r-sys', href: ui.systemHref(s.system), vars: ui.systemTint(s.system), title: systemName(s.system),
        dataset: { nav: 'system/' + s.system, fk: 'r-' + s.system },
      },
      h('span', { class: 'pip' }),
      h('span', { class: 'nm' }, systemName(s.system)),
      h('span', { class: 'ct' }, s.games.toLocaleString('en-US')),
      h('span', { class: 'pips' }, S.devices.map((d) => {
        const st = store.excludes(d.name, s.system) ? 'unsupported' : ((s.devices || {})[d.name] || {}).state || 'none';
        return h('i', { class: 'p ' + st, title: `${d.name}: ${st === 'unsupported' ? 'not offered' : st}` });
      })))),
      S.systems.length ? h('div', { class: 'r-foot' }, h('p', null, plural(total, 'game')), h('p', null, formatBytes(size))) : null));
    markNav();
    if (fk) { const el = $(`[data-fk="${CSS.escape(fk)}"]`); if (el) el.focus({ preventScroll: true }); }
  }

  function renderBnav() {
    const item = (k, href, ic, label) => h('a', { class: 'bn', href, dataset: { bn: k, fk: 'bn-' + k } }, icon(ic), h('span', null, label));
    $('#bnav').replaceChildren(item('home', '#/', 'home', 'Home'), item('systems', '#/systems', 'grid', 'Systems'),
      item('search', '#/search/', 'search', 'Search'), item('consoles', '#/consoles', 'pad', 'Consoles'));
  }

  function markNav() {
    const r = V.route;
    if (!r) return;
    const key = r.name === 'system' ? 'system/' + r.system : r.name === 'game' ? 'system/' + r.system : r.name;
    for (const a of $$('[data-nav]')) {
      if (a.dataset.nav === key) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    }
    const bn = r.name === 'home' ? 'home' : ['systems', 'system', 'game'].includes(r.name) ? 'systems' : r.name;
    for (const a of $$('.bn')) {
      if (a.dataset.bn === bn) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    }
  }

  // ===== router ============================================================================

  function parseRoute() {
    const raw = location.hash.replace(/^#/, '') || '/';
    const qi = raw.indexOf('?');
    const path = (qi < 0 ? raw : raw.slice(0, qi)) || '/';
    const params = new URLSearchParams(qi < 0 ? '' : raw.slice(qi + 1));
    const legacy = params.get('game'); // links of the first version of the page: ?game=system/id
    if (legacy && legacy.includes('/')) {
      const [system, id] = legacy.split('/');
      return { name: 'game', system: decodeURIComponent(system), id: decodeURIComponent(id) };
    }
    let m = /^\/game\/([^/]+)\/([^/]+)$/.exec(path);
    if (m) return { name: 'game', system: decodeURIComponent(m[1]), id: decodeURIComponent(m[2]) };
    m = /^\/system\/(.+)$/.exec(path);
    if (m) return { name: 'system', system: decodeURIComponent(m[1]) };
    m = /^\/search(?:\/(.*))?$/.exec(path);
    if (m) return { name: 'search', q: decodeURIComponent(m[1] || '') };
    if (path === '/systems') return { name: 'systems' };
    if (path === '/consoles') return { name: 'consoles' };
    return { name: 'home' };
  }

  const routeKey = (r) => (r.name === 'game' ? `game/${r.system}/${r.id}` : r.name === 'system' ? 'system/' + r.system : r.name === 'search' ? 'search' : r.name);

  function goUp() {
    const r = V.route;
    if (!r || r.name === 'home') return;
    if (V.depth > 0) { history.back(); return; }
    location.hash = r.name === 'game' ? ui.systemHref(r.system) : '#/';
  }

  function mount(route) {
    const key = routeKey(route);
    const old = V.view;
    if (old) { old.dead = true; if (old.dispose) old.dispose(); }
    if (V.key && document.activeElement && document.activeElement.dataset && document.activeElement.dataset.fk) {
      V.focusMemory.set(V.key, document.activeElement.dataset.fk);
    }
    V.route = route;
    V.key = key;
    document.body.dataset.route = route.name;
    $('#main').setAttribute('aria-busy', 'true');
    views.setPendingScroll(V.pop ? views.scrollMemory.get(key) : null, V.pop ? V.focusMemory.get(key) : null);
    if (!V.pop) window.scrollTo(0, 0);
    switch (route.name) {
      case 'game': V.view = views.viewGame(route.system, route.id); break;
      case 'system': V.view = views.viewSystem(route.system); break;
      case 'systems': V.view = views.viewSystems(); break;
      case 'consoles': V.view = views.viewConsoles(); break;
      case 'search':
        V.q = route.q.trim();
        S.searchResult = null;
        V.view = views.viewSearch(() => V.q);
        runApiSearch();
        break;
      default: V.view = views.viewHome();
    }
    const search = $('#search');
    if (route.name === 'search') {
      if (document.activeElement !== search) search.value = V.q;
      if (!V.q) search.focus({ preventScroll: true });
    } else {
      V.q = '';
      if (document.activeElement !== search) search.value = '';
    }
    markNav();
    $('#main').setAttribute('aria-busy', 'false');
  }

  function renderRoute() {
    const route = parseRoute();
    if (V.key) views.scrollMemory.set(V.key, window.scrollY);
    if (V.key && V.key === routeKey(route) && V.view) {
      V.pop = false;
      if (route.name === 'search' && route.q.trim() !== V.q) {
        V.q = route.q.trim();
        S.searchResult = null;
        $('#search').value = V.q;
        V.view.refresh();
        runApiSearch();
      }
      return;
    }
    if (V.key && !V.pop) V.depth++;
    else if (V.pop) V.depth = Math.max(0, V.depth - 1);
    mount(route);
    V.pop = false;
  }

  // ===== search ============================================================================

  const runApiSearch = debounce(async () => {
    const q = V.q;
    if (!q) return;
    if (runApiSearch.abort) runApiSearch.abort.abort();
    const ctl = new AbortController();
    runApiSearch.abort = ctl;
    try {
      const r = await api.search(q, ctl.signal);
      if (q !== V.q) return;
      S.searchResult = { q, groups: r.groups || [] };
      if (V.view && V.view.kind === 'search') V.view.refresh();
    } catch (err) {
      if (err.name === 'AbortError') return;
      if (err.code !== 'unauthorized') S.searchResult = null; // keep the local answer
    }
  }, 170);

  function onSearchInput(e) {
    const q = e.target.value.trim();
    if (V.route && V.route.name === 'search') {
      if (q === V.q) return;
      V.q = q;
      S.searchResult = null;
      history.replaceState(null, '', '#/search/' + (q ? encodeURIComponent(q) : ''));
      V.route = { name: 'search', q };
      V.view.refresh();
      runApiSearch();
    } else if (q) {
      location.hash = '#/search/' + encodeURIComponent(q);
    }
  }

  // ===== the confirmation of a whole-system change =========================================

  function staticSegs(frac, cls) {
    const el = h('span', { class: 'segs' + (cls ? ' ' + cls : ''), 'aria-hidden': 'true' });
    const lit = Math.round(frac * 20);
    for (let i = 0; i < 20; i++) el.append(h('i', { class: i < lit ? 'on' : '' }));
    return el;
  }

  // Filled from the dry run: per console, how many games change, how much would be downloaded
  // or freed, and what the system looks like afterwards. Resolves true on confirm, false on
  // cancel or Escape.
  function confirmSystem(sys, deviceKey, on, dry) {
    const dlg = $('#confirm');
    const entry = store.system(sys);
    const total = entry ? entry.games : 0;
    const devs = (dry && dry.devices) || [];
    const name = systemName(sys);
    const where = deviceKey === '*' ? 'all consoles' : deviceKey;
    const n = (x) => x.toLocaleString('en-US');
    const blocks = devs.map((d) => {
      const got = (d.on || []).length;
      const lost = (d.off || []).length;
      const cov = store.coverage(sys, d.device);
      const now = cov.n;
      const changed = on ? got : lost;
      const after = on ? total : 0;
      return h('div', { class: 'dblock' },
        devs.length > 1 ? h('h3', null, d.device) : null,
        h('dl', { class: 'mstat' },
          h('div', null, h('dt', null, 'Games that change'), h('dd', null, n(changed))),
          h('div', null, h('dt', null, on ? 'To download' : 'To free up'), h('dd', null, formatBytes(on ? d.bytes_on : d.bytes_off) || '0 B')),
          h('div', null, h('dt', null, 'Result'), h('dd', null, n(after), h('small', null, ` / ${n(total)}`)))),
        now != null && total ? h('div', { class: 'mflow' },
          h('span', { class: 'mf-l' }, 'NOW'), staticSegs(now / total),
          h('span', { class: 'mf-l' }, 'AFTER'), staticSegs(after / total, on ? '' : 'out')) : null,
        !got && !lost ? h('p', { class: 'mute small' }, `No game changes on ${d.device}.`) : null,
        // The other direction too: turning a system on can drop single-game choices, and off can too.
        on && lost ? h('p', { class: 'mute small' }, `${plural(lost, 'game')} that were on are dropped from the choices.`) : null);
    });
    const skipped = ((dry && dry.skipped) || []).map((s) => `${s.device} (${s.reason})`);
    return new Promise((resolve) => {
      let result = false;
      const confirmBtn = h('button', { type: 'button', class: 'btn ' + (on ? 'primary' : 'danger'), onclick: () => { result = true; dlg.close(); } },
        on ? 'Turn on' : 'Turn off');
      const cancelBtn = h('button', { type: 'button', class: 'btn', onclick: () => dlg.close() }, 'Cancel');
      dlg.className = 'modal ' + (on ? 'insert' : 'eject');
      setTintOn(dlg, on ? systemColor(sys) : '#ff5470');
      dlg.replaceChildren(h('div', { class: 'modal-inner' },
        h('div', { class: 'mh' }, h('i'), on ? 'Confirm insert' : 'Confirm eject'),
        h('div', { class: 'mb' },
          h('h2', { id: 'confirm-title' }, `Turn ${name} ${on ? 'on' : 'off'} for ${where}?`),
          blocks,
          h('p', { class: 'note' + (on ? ' warn' : '') }, on
            ? 'The whole system will be offered to the console, including games added to it later. Activating a system overrides its exclusions: games you excluded come back and single-game choices inside it are dropped.'
            : 'Every game of this system will be taken off the console, including single games you added one by one.'),
          skipped.length ? h('p', { class: 'mute small' }, 'Skipped: ' + skipped.join(', ') + '.') : null,
          on ? null : h('p', { class: 'mute small' }, 'The files are deleted from the console about an hour later; turning it on again before then costs nothing.')),
        h('div', { class: 'macts' }, cancelBtn, confirmBtn)));
      dlg.addEventListener('close', () => resolve(result), { once: true });
      dlg.showModal();
      (on ? confirmBtn : cancelBtn).focus();
    });
  }

  // ===== the history of a console's whitelist ==============================================

  // The backups the hub keeps of a console's whitelist (one per write): pick one to see its diff
  // against the current file and the games it would turn on and off, then restore it. The
  // restore carries the etag the diff was shown against, so a whitelist that changed meanwhile
  // is refused by the server, never overwritten.
  const PATHS_SHOWN = 30;

  async function openHistory(deviceName) {
    const dlg = $('#history');
    const st = { list: null, name: null, detail: null, error: '', loading: false };

    function pathList(title, cls, paths) {
      if (!paths.length) return null;
      const more = paths.length - PATHS_SHOWN;
      return h('div', { class: 'plist ' + cls },
        h('h3', null, `${title} (${paths.length})`),
        h('ul', null, paths.slice(0, PATHS_SHOWN).map((p) => h('li', null, p)),
          more > 0 ? h('li', { class: 'mute' }, `and ${more.toLocaleString('en-US')} more`) : null));
    }

    function detailBlock(d) {
      const n = (x) => x.toLocaleString('en-US');
      return h('div', { class: 'hdetail' },
        d.warning ? h('p', { class: 'note warn' }, d.warning + '.') : null,
        d.managed ? null : h('p', { class: 'note warn' },
          `This version is not in the format the UI manages. After restoring it the UI cannot change ${deviceName} until you run "warpzone whitelist migrate ${deviceName}" on the hub.`),
        d.on || d.off ? h('dl', { class: 'mstat' },
          h('div', null, h('dt', null, 'Games back on'), h('dd', null, n((d.on || []).length))),
          h('div', null, h('dt', null, 'To download'), h('dd', null, formatBytes(d.bytes_on) || '0 B')),
          h('div', null, h('dt', null, 'Games off'), h('dd', null, n((d.off || []).length), h('small', null, ` / ${formatBytes(d.bytes_off) || '0 B'}`)))) : null,
        pathList('Back on', 'in', d.included || []),
        pathList('Taken off', 'out', d.excluded || []),
        d.diff.length
          ? h('pre', { class: 'diff', tabindex: '0', 'aria-label': 'Changes from the current whitelist to this version' },
            d.diff.map((line) => h('span', { class: line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : line.startsWith('@') ? 'hunk' : '' }, line + '\n')))
          : h('p', { class: 'mute small' }, 'The text is the same as the current whitelist.'));
    }

    function row(b) {
      const picked = st.name === b.name;
      return h('li', null, h('button', {
        type: 'button', class: 'hist-row' + (picked ? ' on' : ''), 'aria-pressed': String(picked), disabled: b.empty || null,
        dataset: { fk: 'hb:' + b.name }, onclick: () => pick(b.name),
      }, h('b', { title: formatMoment(b.time) }, formatMoment(b.time) || b.name), h('small', null, timeAgo(b.time)),
      h('span', null, b.summary)));
    }

    function paint() {
      const backups = st.list ? st.list.backups : [];
      const current = st.detail;
      const canRestore = !!current && !current.same;
      const fk = document.activeElement && dlg.contains(document.activeElement) ? document.activeElement.dataset.fk : null;
      const restoreBtn = h('button', {
        type: 'button', class: 'btn primary', disabled: canRestore ? null : true,
        onclick: restore,
      }, 'Restore');
      dlg.className = 'modal history';
      setTintOn(dlg, '#38f9ff');
      dlg.replaceChildren(h('div', { class: 'modal-inner' },
        h('div', { class: 'mh' }, h('i'), 'History'),
        h('div', { class: 'mb' },
          h('h2', { id: 'history-title' }, `Whitelist of ${deviceName}`),
          h('p', { class: 'mute small' }, 'Every change keeps the file it replaced. Restoring one writes it back, after keeping the current file as a backup too.'),
          st.error ? h('p', { class: 'error-text', role: 'alert' }, st.error) : null,
          !st.list ? h('p', { class: 'mute' }, 'Loading...')
            : backups.length ? h('ul', { class: 'hist' }, backups.map(row))
              : h('p', { class: 'mute' }, 'No backups yet.'),
          st.loading ? h('p', { class: 'mute small' }, 'Loading this version...') : null,
          current ? detailBlock(current) : null),
        h('div', { class: 'macts' },
          h('button', { type: 'button', class: 'btn', onclick: () => dlg.close() }, 'Close'),
          st.name ? restoreBtn : null)));
      const again = fk && $(`[data-fk="${CSS.escape(fk)}"]`, dlg);
      if (again) again.focus({ preventScroll: true });
    }

    async function loadList() {
      try {
        st.list = await api.backups(deviceName);
        st.error = '';
      } catch (err) {
        st.error = humanError(err);
      }
      paint();
    }

    async function pick(name) {
      st.name = name;
      st.detail = null;
      st.loading = true;
      st.error = '';
      paint();
      try {
        const d = await api.backup(deviceName, name);
        if (st.name !== name) return;
        st.detail = d;
      } catch (err) {
        if (st.name === name) st.error = humanError(err);
      }
      st.loading = false;
      paint();
    }

    async function restore() {
      const d = st.detail;
      if (!d) return;
      const res = await store.restoreBackup(deviceName, d.backup, d.etag);
      if (res) {
        dlg.close();
        toast(`Restored the whitelist of ${deviceName} to ${formatMoment(d.time) || d.backup}.` +
          (d.warning ? ' The console will download what came back on.' : ''), 'ok');
        return;
      }
      // Refused or failed: the error is shown; look at the file as it is now.
      st.detail = null;
      st.name = null;
      await loadList();
    }

    paint();
    dlg.showModal();
    await loadList();
  }

  function setTintOn(el, color) { for (const [k, v] of Object.entries(tint(color))) el.style.setProperty('--' + k, v); }

  // ===== keyboard and gamepad ==============================================================

  // Arrow keys (and a gamepad's d-pad) move focus to the nearest focusable element in that
  // direction, so a grid, a shelf and the levers can be crossed as well as with Tab. Enter
  // activates (links and buttons do that natively); Escape closes a dialog or goes back.
  const NAVSEL = 'a[href]:not([tabindex="-1"]), button:not([disabled]):not([tabindex="-1"]), input:not([disabled]), select:not([disabled]), summary, video[controls]';

  function visible(el) {
    const r = el.getBoundingClientRect();
    return (r.width > 0 || r.height > 0) && getComputedStyle(el).visibility !== 'hidden' && !el.closest('[hidden]');
  }

  function move(dir) {
    const open = $$('dialog[open]');
    const root = open.length ? open[open.length - 1] : document;
    if (root.id === 'login') return;
    const els = $$(NAVSEL, root).filter((el) => !el.classList.contains('skip') && visible(el));
    if (!els.length) return;
    const cur = document.activeElement;
    if (!els.includes(cur)) {
      const first = open.length ? els[0] : els.find((el) => $('#main').contains(el) && el.getBoundingClientRect().top >= 0 && el.getBoundingClientRect().top < innerHeight) || els[0];
      first.focus();
      return;
    }
    const a = cur.getBoundingClientRect();
    const ax = a.left + a.width / 2;
    const ay = a.top + a.height / 2;
    let best = null;
    let bestScore = Infinity;
    for (const el of els) {
      if (el === cur) continue;
      const r = el.getBoundingClientRect();
      const dx = r.left + r.width / 2 - ax;
      const dy = r.top + r.height / 2 - ay;
      let along; let across;
      if (dir === 'right') { along = dx; across = Math.abs(dy); if (r.left < a.right - a.width * 0.5) continue; }
      else if (dir === 'left') { along = -dx; across = Math.abs(dy); if (r.right > a.left + a.width * 0.5) continue; }
      else if (dir === 'down') { along = dy; across = Math.abs(dx); if (r.top < a.bottom - a.height * 0.5) continue; }
      else { along = -dy; across = Math.abs(dx); if (r.bottom > a.top + a.height * 0.5) continue; }
      if (along <= 2) continue;
      const score = along + across * 2.6;
      if (score < bestScore) { bestScore = score; best = el; }
    }
    if (best) { best.focus(); if (best.scrollIntoView) best.scrollIntoView({ block: 'nearest', inline: 'nearest' }); }
  }

  function onKey(e) {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    const t = e.target;
    const typing = t.matches && t.matches('input[type="text"], input[type="search"], input[type="password"]');
    const dialogOpen = !!$('dialog[open]');
    if (e.key === 'Escape') {
      if (dialogOpen) return; // <dialog> closes itself
      if (t.id === 'search') {
        e.preventDefault();
        if (t.value) { t.value = ''; onSearchInput({ target: t }); } else { t.blur(); if (V.route && V.route.name === 'search') goUp(); }
        return;
      }
      if (t.dataset && t.dataset.fk === 'filter' && t.value) {
        e.preventDefault(); t.value = ''; t.dispatchEvent(new Event('input', { bubbles: true })); return;
      }
      if (t.matches && t.matches('select')) return;
      e.preventDefault();
      goUp();
      return;
    }
    if (e.key === '/' && !typing && !(t.matches && t.matches('select, textarea')) && !dialogOpen) {
      e.preventDefault();
      const search = $('#search');
      if (V.route && V.route.name !== 'search' && getComputedStyle(search.closest('form')).display === 'none') location.hash = '#/search/';
      else { search.focus(); search.select(); }
      return;
    }
    const dir = { ArrowRight: 'right', ArrowLeft: 'left', ArrowDown: 'down', ArrowUp: 'up' }[e.key];
    if (!dir || e.shiftKey) return;
    if (t.matches && t.matches('select')) return;
    if (typing && (dir === 'left' || dir === 'right')) return;
    if (t.id === 'login-token') return;
    e.preventDefault();
    move(dir);
  }

  function startGamepad() {
    let raf = 0;
    let repeatAt = 0;
    let held = false;
    const last = {};
    const poll = () => {
      const pads = navigator.getGamepads ? Array.from(navigator.getGamepads()) : [];
      const gp = pads.find(Boolean);
      if (gp) {
        const now = performance.now();
        const b = (i) => !!(gp.buttons[i] && gp.buttons[i].pressed);
        const dirs = [
          ['up', b(12) || gp.axes[1] < -0.6], ['down', b(13) || gp.axes[1] > 0.6],
          ['left', b(14) || gp.axes[0] < -0.6], ['right', b(15) || gp.axes[0] > 0.6],
        ];
        const any = dirs.find((d) => d[1]);
        if (any && now - repeatAt > (held ? 190 : 380)) { move(any[0]); repeatAt = now; held = true; } else if (!any) held = false;
        const edge = (k, i, fn) => { const p = b(i); if (p && !last[k]) fn(); last[k] = p; };
        edge('a', 0, () => { const el = document.activeElement; if (el && el.click) el.click(); });
        edge('b', 1, () => { const dlg = $('dialog[open]'); if (dlg && dlg.id !== 'login') dlg.close(); else if (!dlg) goUp(); });
        edge('start', 9, () => $('#search').focus());
      }
      raf = requestAnimationFrame(poll);
    };
    addEventListener('gamepadconnected', () => { if (!raf) raf = requestAnimationFrame(poll); });
    addEventListener('gamepaddisconnected', () => { cancelAnimationFrame(raf); raf = 0; });
  }

  // ===== display ===========================================================================

  // The CRT overlay is a static layer of scanlines and a soft vignette. The choice is kept in a
  // plain cookie (not the session): it is a display preference, not a credential.
  function setCrt(on, remember) {
    document.body.classList.toggle('crt', on);
    for (const b of $$('[data-crt]')) b.setAttribute('aria-pressed', String(on));
    if (remember) document.cookie = `wz_crt=${on ? 1 : 0}; Path=/; Max-Age=31536000; SameSite=Strict`;
  }

  // ===== login, boot =======================================================================

  function showLogin(message) {
    const dlg = $('#login');
    if (!dlg.open) dlg.showModal();
    const err = $('#login-error');
    err.hidden = !message;
    err.textContent = message || '';
    $('#login-token').focus();
  }

  async function submitLogin(e) {
    e.preventDefault();
    const input = $('#login-token');
    const token = input.value;
    input.value = ''; // the page keeps no copy
    try {
      await api.login(token);
    } catch (err) {
      showLogin(err.status === 401 ? 'That token is not right.' : humanError(err));
      return;
    }
    $('#login').close();
    boot();
  }

  async function boot() {
    try {
      await Promise.all([store.loadDevices(), store.loadSystems(), store.loadCatalog()]);
    } catch (err) {
      if (err.code !== 'unauthorized') {
        $('#main').replaceChildren(h('div', { class: 'page' }, h('div', { class: 'empty' }, h('b', null, 'The library is not available'),
          h('p', null, humanError(err)), h('button', { class: 'btn', type: 'button', onclick: boot }, 'Try again'))));
        $('#main').setAttribute('aria-busy', 'false');
      }
      return;
    }
    renderLeds();
    renderRail();
    V.key = '';
    V.view = null;
    V.route = parseRoute();
    renderRoute();
    store.pollProgress();
    store.preloadLists();
    store.watchCatalog(S.catalog && S.catalog.running ? 1500 : 60000);
  }

  function init() {
    Object.assign(store.hooks, {
      refresh() { if (V.view && V.view.refresh) V.view.refresh(); renderRail(); },
      devicesChanged() { renderLeds(); },
      listsChanged() { if (V.view && V.view.listsChanged) V.view.listsChanged(); },
      progressChanged() { ui.paintProgress(); },
      detailChanged(id) { if (V.view && V.view.kind === 'game' && V.view.id === id && V.view.reload) V.view.reload(); },
      confirmSystem,
      openHistory,
      reset() { views.resetFeatured(); },
    });
    api.onUnauthorized(() => showLogin(''));
    $('#login').addEventListener('cancel', (e) => e.preventDefault());
    $('#login-form').addEventListener('submit', submitLogin);
    window.addEventListener('popstate', () => { V.pop = true; });
    window.addEventListener('hashchange', renderRoute);
    document.addEventListener('keydown', onKey);
    document.addEventListener('visibilitychange', () => { if (!document.hidden && S.devices.length) store.schedulePoll(200); });
    for (const b of $$('[data-crt]')) b.addEventListener('click', () => setCrt(!document.body.classList.contains('crt'), true));
    setCrt(!/(?:^|;\s*)wz_crt=0/.test(document.cookie), false);
    renderBnav();
    $('.s-ic').append(icon('search'));
    startGamepad();

    const search = $('#search');
    $('#search-form').addEventListener('submit', (e) => {
      e.preventDefault();
      const first = $('.gt, .tl', $('#main'));
      if (first) first.focus();
    });
    search.addEventListener('input', onSearchInput);
    search.addEventListener('focus', () => { if (V.route && V.route.name !== 'search' && getComputedStyle(search.closest('form')).display !== 'none') search.select(); });

    const confirmDlg = $('#confirm');
    confirmDlg.addEventListener('click', (e) => { if (e.target === confirmDlg) confirmDlg.close(); });
    const historyDlg = $('#history');
    historyDlg.addEventListener('click', (e) => { if (e.target === historyDlg) historyDlg.close(); });
    document.addEventListener('click', (e) => {
      // The brand link also clears a search that is showing while the route is already home.
      if (e.target.closest && e.target.closest('.brand') && V.q) { search.value = ''; V.q = ''; }
    });
    boot();
  }

  init();
})();
