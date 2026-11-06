/*
 * warpzone library UI.
 *
 * A static page with no build step. It shows the catalog and which console gets which game,
 * and turns games and systems on and off. All decisions (what a change does, what it would
 * download, which error applies) belong to the API in development/design/library-ui.md; this
 * file only draws the answers and sends the changes. The one thing duplicated on purpose is
 * the title key port in api.js, which lets the search answer while typing.
 *
 * Layout of this file:
 *   1. helpers (DOM builder, toasts)      5. views: home, system, search
 *   2. state and data loading             6. the game sheet
 *   3. progress polling                   7. changes: dry run, confirmation, writes
 *   4. small components                   8. keyboard navigation, router, login, boot
 *
 * The page is served with Content-Security-Policy: default-src 'self', so there is no inline
 * style or script and no data: URL. Dynamic styling goes through custom properties set with
 * element.style.setProperty, which the policy allows.
 */
(function () {
  'use strict';

  const { ApiError, api, humanError, formatBytes, formatDate, normalizeWords, titleKey } = window.WZ;

  // ===== 1. helpers ========================================================================

  // Build an element: h('div', {class: 'x', onclick: fn, vars: {hue: 200}}, child, ...).
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v == null || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'dataset') Object.assign(el.dataset, v);
        else if (k === 'vars') for (const [n, val] of Object.entries(v)) el.style.setProperty('--' + n, val);
        else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
        else if (v === true) el.setAttribute(k, '');
        else el.setAttribute(k, v);
      }
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const plural = (n, one, many) => `${n.toLocaleString('en-US')} ${n === 1 ? one : many || one + 's'}`;

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

  function debounce(fn, ms) {
    let t = null;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  function throttle(fn, ms) {
    let timer = null;
    return () => {
      if (timer) return;
      timer = setTimeout(() => { timer = null; fn(); }, ms);
    };
  }

  // A stable hue per system, used to tint its placeholders and tiles.
  function hueOf(name) {
    let n = 0;
    for (let i = 0; i < name.length; i++) n = (n * 31 + name.charCodeAt(i)) % 360;
    return n;
  }

  // Display names for the folder names of library/roms. Anything else is prettified.
  const SYSTEM_NAMES = {
    nes: 'Nintendo NES', famicom: 'Famicom', snes: 'Super Nintendo', sfc: 'Super Famicom',
    n64: 'Nintendo 64', gb: 'Game Boy', gbc: 'Game Boy Color', gba: 'Game Boy Advance',
    nds: 'Nintendo DS', n3ds: 'Nintendo 3DS', '3ds': 'Nintendo 3DS', gc: 'GameCube', wii: 'Wii',
    wiiu: 'Wii U', switch: 'Nintendo Switch', psx: 'PlayStation', ps2: 'PlayStation 2',
    ps3: 'PlayStation 3', ps4: 'PlayStation 4', psp: 'PlayStation Portable',
    psvita: 'PlayStation Vita', genesis: 'Sega Genesis', megadrive: 'Sega Mega Drive',
    mastersystem: 'Sega Master System', gamegear: 'Sega Game Gear', saturn: 'Sega Saturn',
    dreamcast: 'Sega Dreamcast', segacd: 'Sega CD', sega32x: 'Sega 32X', xbox: 'Xbox',
    xbox360: 'Xbox 360', arcade: 'Arcade', mame: 'MAME', fbneo: 'FinalBurn Neo',
    neogeo: 'Neo Geo', pcengine: 'PC Engine', tg16: 'TurboGrafx-16', atari2600: 'Atari 2600',
    atari7800: 'Atari 7800', lynx: 'Atari Lynx', ngp: 'Neo Geo Pocket', model2: 'Sega Model 2',
  };

  function systemName(id) {
    if (SYSTEM_NAMES[id]) return SYSTEM_NAMES[id];
    const pretty = id.replace(/[-_]+/g, ' ');
    return pretty.length <= 4 ? pretty.toUpperCase() : pretty.replace(/\b\w/g, (c) => c.toUpperCase());
  }

  const STATE_LABEL = {
    on: 'On', off: 'Off', partial: 'Partial', unsupported: 'Unsupported',
    all: 'All games', some: 'Some games', none: 'None',
  };

  const KIND_LABEL = {
    covers: 'Cover', miximages: 'Mix image', screenshots: 'Screenshot', titlescreens: 'Title screen',
    marquees: 'Marquee', '3dboxes': '3D box',
  };

  // ===== 2. state and data loading =========================================================

  const S = {
    devices: [],            // [{name, profile, exclude_systems, whitelist: {state, etag}}]
    systems: [],            // [{system, games, size, devices: {name: {state, whole}}}]
    etags: {},              // device -> the whitelist etag the page last saw
    lists: new Map(),       // system -> {games: [...], byId: Map}
    listLoads: new Map(),   // system -> promise (dedupes loads)
    byId: new Map(),        // game id -> {game, system}
    progress: {},           // device -> progress entry
    progressError: false,
    route: { path: '/', game: null },
    q: '',                  // the search box
    searchResult: null,     // {q, groups} from the API
    view: null,             // {kind, refresh()}
    viewSeq: 0,
    busy: false,
    detail: null,           // {system, id, data, kind}
    gameOpenedByNav: false,
  };

  function ensureArray(x, key) {
    if (Array.isArray(x)) return x;
    if (x && Array.isArray(x[key])) return x[key];
    return [];
  }

  async function loadDevices() {
    const list = ensureArray(await api.devices(), 'devices');
    S.devices = list.map((d) => ({ ...d, exclude_systems: d.exclude_systems || [] }));
    for (const d of S.devices) {
      if (d.whitelist && d.whitelist.etag) S.etags[d.name] = d.whitelist.etag;
    }
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

  function ensureList(system, force) {
    if (!force && S.lists.has(system)) return Promise.resolve(S.lists.get(system));
    if (!force && S.listLoads.has(system)) return S.listLoads.get(system);
    const p = api.games(system).then((r) => {
      indexList(system, ensureArray(r, 'games'));
      S.listLoads.delete(system);
      listsChanged();
      return S.lists.get(system);
    }, (err) => { S.listLoads.delete(system); throw err; });
    S.listLoads.set(system, p);
    return p;
  }

  // Load every system in the background (three at a time): the shelves and the instant search
  // need them. A failure just leaves that system out until the next reload.
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

  const listsChanged = throttle(() => {
    if (S.view && S.view.listsChanged) S.view.listsChanged();
  }, 350);

  async function reloadAll() {
    S.lists.clear();
    S.byId.clear();
    S.listLoads.clear();
    try { await Promise.all([loadDevices(), loadSystems()]); } catch (err) { await handleError(err, true); return; }
    renderConsoles();
    refreshView();
    if (S.detail) openDetail(S.detail.system, S.detail.id, true);
    preloadLists();
  }

  function isManaged(deviceName) {
    const d = S.devices.find((x) => x.name === deviceName);
    return !d || !d.whitelist || d.whitelist.state === 'managed';
  }

  function excludes(deviceName, system) {
    const d = S.devices.find((x) => x.name === deviceName);
    return !!d && d.exclude_systems.includes(system);
  }

  // ===== 3. progress =======================================================================

  let progressTimer = null;

  function progressSoon() { schedulePoll(300); }

  function schedulePoll(ms) {
    clearTimeout(progressTimer);
    progressTimer = setTimeout(pollProgress, ms);
  }

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
    paintProgress();
    // Fast while something is pending or downloading; a slow poll otherwise, so a console that
    // comes online or finishes is noticed without a reload.
    schedulePoll(busy ? 4000 : S.progressError ? 20000 : 45000);
  }

  function progressText(p, long) {
    if (!p) return '';
    switch (p.state) {
      case 'downloading': return `${long ? 'Downloading' : 'Syncing'} ${p.percent != null ? p.percent + '%' : ''}`.trim();
      case 'pending': return long ? 'Waiting for the console to apply the change' : 'Pending';
      case 'done': return long ? 'Up to date' : 'Up to date';
      case 'offline': return 'Offline';
      case 'paused': return 'Paused';
      case 'not-shared': return long ? 'The library folder is not shared with it' : 'Not shared';
      default: return p.state;
    }
  }

  // Progress is drawn into nodes tagged data-progress-dev; polling repaints them in place so
  // the page is not rebuilt every few seconds.
  function progressNode(device, long) {
    return h('span', { class: 'prog', dataset: { progressDev: device, long: long ? '1' : '' } });
  }

  function paintProgress() {
    for (const el of $$('[data-progress-dev]')) {
      const p = S.progress[el.dataset.progressDev];
      el.className = 'prog' + (p ? ' prog-' + p.state : '');
      el.textContent = progressText(p, !!el.dataset.long);
      if (p && p.state === 'downloading' && p.percent != null) {
        const host = el.closest('.console');
        if (host) host.style.setProperty('--pct', p.percent + '%');
      }
    }
    const banner = $('#banner');
    if (S.progressError === 'hub') {
      banner.hidden = false;
      banner.textContent = 'The hub could not reach its Syncthing, so the progress of the consoles is unknown. Retrying.';
    } else if (S.progressError) {
      banner.hidden = false;
      banner.textContent = 'The server is not answering. Retrying.';
    } else {
      banner.hidden = true;
    }
  }

  // ===== 4. components =====================================================================

  function renderConsoles() {
    const ul = $('#consoles');
    ul.replaceChildren(...S.devices.map((d) => {
      const needs = d.whitelist && d.whitelist.state !== 'managed';
      return h('li', { class: 'console' + (needs ? ' needs-migration' : '') },
        h('span', { class: 'console-name' }, d.name),
        needs
          ? h('span', { class: 'prog', title: 'Run warpzone whitelist migrate on the hub' },
            d.whitelist.state === 'missing' ? 'No whitelist' : 'Needs migration')
          : progressNode(d.name),
        h('span', { class: 'bar', 'aria-hidden': 'true' }));
    }));
    paintProgress();
  }

  function stateBadge(state, extra) {
    return h('span', { class: 'badge state-' + state }, STATE_LABEL[state] || state, extra);
  }

  // The RetroAchievements badge. Text only, by design (no RA images are fetched or shipped).
  const RA_TEXT = {
    supported: 'Achievements',
    linked: 'Known to RetroAchievements, no achievements yet',
    'not-linked': 'Not found in RetroAchievements',
    'no-hash': 'Not hashed yet',
    'no-list': 'No RetroAchievements list for this system',
  };

  function raBadge(ra, full) {
    const info = typeof ra === 'string' ? { state: ra } : (ra || {});
    if (!info.state) return null;
    let text = RA_TEXT[info.state] || info.state;
    if (info.state === 'supported' && full && info.achievements != null) {
      text = `${plural(info.achievements, 'achievement')}, ${info.points != null ? plural(info.points, 'point') : ''}`.replace(/, $/, '');
    }
    const cls = 'badge ra ra-' + info.state;
    if (info.state === 'supported' && info.url && full) {
      return h('a', { class: cls, href: info.url, target: '_blank', rel: 'noopener noreferrer' }, 'RA ', text);
    }
    return h('span', { class: cls, title: RA_TEXT[info.state] }, 'RA ', text);
  }

  // The cover: the image over a placeholder. The placeholder stays in the DOM, so a missing or
  // slow image shows it instead of a hole.
  function coverBox(system, id, hasCover, kind, label) {
    const box = h('div', { class: 'cover', vars: { hue: hueOf(system) } },
      h('span', { class: 'cover-ph' }, h('span', { class: 'cover-ph-label' }, system)));
    if (hasCover !== false) {
      const img = h('img', {
        src: api.mediaUrl(id, kind || 'covers'), alt: label || '', loading: 'lazy', decoding: 'async',
      });
      img.addEventListener('error', () => img.remove());
      img.addEventListener('load', () => box.classList.add('loaded'));
      box.append(img);
    }
    return box;
  }

  function currentBase() {
    return S.route.path === '/' ? '#/' : '#' + S.route.path;
  }

  function gameHref(system, id) {
    return `${currentBase()}?game=${encodeURIComponent(system)}/${encodeURIComponent(id)}`;
  }

  function consolePills(devices) {
    const on = S.devices.filter((d) => devices && (devices[d.name] === 'on' || devices[d.name] === 'partial'));
    return h('span', { class: 'pills' }, on.map((d) =>
      h('span', { class: 'pill pill-' + devices[d.name] },
        d.name, h('span', { class: 'sr-only' }, ` (${devices[d.name]})`))));
  }

  function gameCard(g, system) {
    return h('a', {
      class: 'card', href: gameHref(system, g.id), dataset: { fk: 'g:' + g.id },
    },
    coverBox(system, g.id, g.cover, 'covers'),
    g.ra === 'supported' ? h('span', { class: 'badge ra ra-supported card-ra', title: RA_TEXT.supported }, 'RA') : null,
    h('span', { class: 'card-body' },
      h('span', { class: 'card-title' }, g.name || g.title),
      h('span', { class: 'card-meta' }, systemName(system), g.size ? ' · ' + formatBytes(g.size) : ''),
      consolePills(g.devices)));
  }

  function skeletonCards(n) {
    return Array.from({ length: n }, () =>
      h('div', { class: 'card skeleton', 'aria-hidden': 'true' }, h('div', { class: 'cover' }), h('span', { class: 'card-body' }, h('span', { class: 'card-title' }, ' '))));
  }

  function shelf(title, games, opts) {
    const o = opts || {};
    return h('section', { class: 'shelf' },
      h('div', { class: 'section-head' },
        h('h2', null, title),
        o.href ? h('a', { class: 'more', href: o.href }, o.hrefLabel || 'See all') : null),
      h('div', { class: 'shelf-row' }, games.map((x) => gameCard(x.game, x.system))));
  }

  // ===== 5. views ==========================================================================

  function setMain(node, title) {
    const main = $('#main');
    main.replaceChildren(node);
    main.setAttribute('aria-busy', 'false');
    document.title = title ? `${title} - warpzone library` : 'warpzone library';
  }

  function captureFocus() {
    const el = document.activeElement;
    return el && el.dataset ? el.dataset.fk || null : null;
  }

  function restoreFocus(fk) {
    if (!fk) return;
    const el = $(`[data-fk="${CSS.escape(fk)}"]`);
    if (el) el.focus({ preventScroll: true });
  }

  function refreshView() {
    if (S.view && S.view.refresh) S.view.refresh();
  }

  function allGames() {
    const out = [];
    for (const [system, list] of S.lists) for (const g of list.games) out.push({ game: g, system });
    return out;
  }

  // ---- home ----

  function viewHome() {
    const root = h('div', { class: 'page home' });
    const totalGames = S.systems.reduce((n, s) => n + (s.games || 0), 0);
    const totalSize = S.systems.reduce((n, s) => n + (s.size || 0), 0);

    const intro = h('section', { class: 'intro' },
      h('h1', null, 'Library'),
      h('p', { class: 'muted' }, S.systems.length
        ? `${plural(totalGames, 'game')} in ${plural(S.systems.length, 'system')}, ${formatBytes(totalSize)} in all.`
        : 'The catalog is empty.'));
    const shelves = h('div', { class: 'shelves' });
    const status = h('p', { class: 'muted loading-note', role: 'status' });
    const tiles = h('div', { class: 'tiles' });
    const bySystem = h('div', { class: 'shelves' });
    const refresh = h('button', { class: 'btn', type: 'button', onclick: refreshCatalog }, 'Rescan the library');

    root.append(
      intro,
      shelves,
      h('section', null,
        h('div', { class: 'section-head' }, h('h2', null, 'Systems')),
        tiles),
      h('section', null,
        h('div', { class: 'section-head' }, h('h2', null, 'Browse by system')),
        status, bySystem),
      h('div', { class: 'footer-actions' }, refresh,
        h('span', { class: 'muted' }, 'Runs the catalog scan on the hub now. It also runs by itself every few hours.')));

    if (!S.systems.length) {
      intro.append(h('p', null, 'Nothing is listed yet. Run "warpzone catalog" on the hub, or press the button below.'));
    }

    function paintTiles() {
      tiles.replaceChildren(...S.systems.map(systemTile));
    }

    function paintShelves() {
      const all = allGames();
      const loaded = S.lists.size;
      const total = S.systems.length;
      status.textContent = loaded < total ? `Loading the catalog (${loaded} of ${total} systems)...` : '';
      const parts = [];
      const recent = all.filter((x) => x.game.added).sort((a, b) =>
        b.game.added.localeCompare(a.game.added) || (a.game.name || a.game.title).localeCompare(b.game.name || b.game.title)).slice(0, 24);
      if (recent.length) parts.push(shelf('Recently added', recent));
      const onSome = all.filter((x) => Object.values(x.game.devices).some((v) => v === 'on' || v === 'partial'))
        .sort((a, b) => (b.game.added || '').localeCompare(a.game.added || '')).slice(0, 24);
      if (onSome.length) parts.push(shelf('On your consoles', onSome));
      if (!parts.length && loaded < total) parts.push(h('div', { class: 'shelf-row' }, skeletonCards(8)));
      shelves.replaceChildren(...parts);

      const rows = [];
      for (const s of S.systems) {
        const list = S.lists.get(s.system);
        if (!list || !list.games.length) continue;
        const picks = list.games.filter((g) => g.cover).slice(0, 14);
        const items = (picks.length ? picks : list.games.slice(0, 14)).map((g) => ({ game: g, system: s.system }));
        rows.push(shelf(systemName(s.system), items, { href: `#/system/${encodeURIComponent(s.system)}`, hrefLabel: `See all ${s.games}` }));
      }
      bySystem.replaceChildren(...rows);
    }

    setMain(root, '');
    paintTiles();
    paintShelves();
    return {
      kind: 'home',
      refresh() { const fk = captureFocus(); paintTiles(); paintShelves(); restoreFocus(fk); },
      listsChanged() { const fk = captureFocus(); paintTiles(); paintShelves(); restoreFocus(fk); },
    };
  }

  function systemTile(s) {
    const list = S.lists.get(s.system);
    const covers = list ? list.games.filter((g) => g.cover).slice(0, 3) : [];
    const art = h('span', { class: 'tile-art', 'aria-hidden': 'true' },
      h('span', { class: 'tile-code' }, s.system),
      covers.map((g, i) => {
        const img = h('img', { src: api.mediaUrl(g.id, 'covers'), alt: '', loading: 'lazy', decoding: 'async', class: 'tile-cover c' + i });
        img.addEventListener('error', () => img.remove());
        return img;
      }));
    return h('a', {
      class: 'tile', href: `#/system/${encodeURIComponent(s.system)}`, vars: { hue: hueOf(s.system) },
      dataset: { fk: 's:' + s.system },
    },
    art,
    h('span', { class: 'tile-body' },
      h('span', { class: 'tile-title' }, systemName(s.system)),
      h('span', { class: 'tile-meta' }, `${plural(s.games, 'game')} · ${formatBytes(s.size)}`),
      h('span', { class: 'tile-states' }, S.devices.map((d) => {
        const st = excludes(d.name, s.system) ? 'unsupported' : ((s.devices || {})[d.name] || {}).state || 'none';
        return h('span', { class: 'chip state-' + st }, d.name, ' ', h('b', null, STATE_LABEL[st] || st));
      }))));
  }

  // ---- system ----

  async function viewSystem(name) {
    const entry = S.systems.find((s) => s.system === name);
    if (!entry) {
      setMain(h('div', { class: 'page empty' }, h('h1', null, 'Unknown system'),
        h('p', null, `There is no system called "${name}" in the catalog.`),
        h('a', { class: 'btn', href: '#/' }, 'Back to the library')), 'Unknown system');
      return { kind: 'system' };
    }
    const seq = S.viewSeq;
    const vs = { filter: '', sort: 'name', chip: 'all', limit: 120 };
    const panelHost = h('div', { class: 'panel-host' });
    const gridHost = h('div', { class: 'grid-host' });
    const countEl = h('span', { class: 'muted', role: 'status' });
    let observer = null;

    const filterInput = h('input', {
      type: 'search', id: 'filter', placeholder: 'Filter this system', spellcheck: 'false', autocapitalize: 'off',
      'aria-label': 'Filter games of this system', dataset: { fk: 'filter' },
      oninput: debounce((e) => { vs.filter = e.target.value; vs.limit = 120; paintGrid(); }, 90),
    });
    const sortSel = h('select', {
      'aria-label': 'Sort', dataset: { fk: 'sort' },
      onchange: (e) => { vs.sort = e.target.value; vs.limit = 120; paintGrid(); },
    }, [['name', 'Name'], ['added', 'Recently added'], ['size', 'Largest first']].map(([v, t]) => h('option', { value: v }, t)));
    const chips = h('div', { class: 'chips', role: 'group', 'aria-label': 'Show' });
    const CHIPS = [['all', 'All'], ['on', 'On a console'], ['off', 'On no console'], ['ra', 'With achievements']];
    function paintChips() {
      chips.replaceChildren(...CHIPS.map(([v, t]) => h('button', {
        type: 'button', class: 'chip-btn', 'aria-pressed': String(vs.chip === v), dataset: { fk: 'chip:' + v },
        onclick: () => { vs.chip = v; vs.limit = 120; paintChips(); paintGrid(); restoreFocus('chip:' + v); },
      }, t)));
    }

    const root = h('div', { class: 'page system', vars: { hue: hueOf(name) } },
      h('div', { class: 'crumbs' }, h('a', { href: '#/' }, 'Library'), ' / ', systemName(name)),
      h('div', { class: 'sys-head' },
        h('h1', null, systemName(name)),
        h('p', { class: 'muted' }, `${plural(entry.games, 'game')} · ${formatBytes(entry.size)}`)),
      panelHost,
      h('div', { class: 'toolbar' }, filterInput, sortSel, chips, countEl),
      gridHost);

    function visibleGames() {
      const list = S.lists.get(name);
      if (!list) return null;
      const words = normalizeWords(vs.filter).split(' ').filter(Boolean);
      let games = list.games.filter((g) => {
        if (words.length) {
          const hay = normalizeWords((g.name || '') + ' ' + (g.title || ''));
          if (!words.every((w) => hay.includes(w))) return false;
        }
        const any = Object.values(g.devices).some((v) => v === 'on' || v === 'partial');
        if (vs.chip === 'on') return any;
        if (vs.chip === 'off') return !any;
        if (vs.chip === 'ra') return g.ra === 'supported';
        return true;
      });
      const nm = (g) => g.name || g.title;
      if (vs.sort === 'added') games = games.sort((a, b) => (b.added || '').localeCompare(a.added || '') || nm(a).localeCompare(nm(b)));
      else if (vs.sort === 'size') games = games.sort((a, b) => (b.size || 0) - (a.size || 0));
      else games = games.sort((a, b) => nm(a).localeCompare(nm(b), undefined, { numeric: true, sensitivity: 'base' }));
      return games;
    }

    // The grid renders in chunks: a system of thousands of sets keeps the DOM small, and the
    // sentinel at the end asks for the next chunk when it nears the viewport.
    function paintGrid() {
      if (observer) observer.disconnect();
      const games = visibleGames();
      if (!games) { gridHost.replaceChildren(h('div', { class: 'grid' }, skeletonCards(12))); return; }
      countEl.textContent = games.length === S.lists.get(name).games.length
        ? '' : `${games.length.toLocaleString('en-US')} of ${S.lists.get(name).games.length.toLocaleString('en-US')}`;
      if (!games.length) {
        gridHost.replaceChildren(h('p', { class: 'empty-note' }, S.lists.get(name).games.length ? 'No game matches.' : 'This system has no games.'));
        return;
      }
      const grid = h('div', { class: 'grid' });
      let shown = 0;
      const sentinel = h('div', { class: 'sentinel', 'aria-hidden': 'true' });
      const more = () => {
        const upto = Math.min(games.length, vs.limit);
        for (; shown < upto; shown++) grid.append(gameCard(games[shown], name));
        sentinel.hidden = shown >= games.length;
      };
      more();
      gridHost.replaceChildren(grid, sentinel);
      if ('IntersectionObserver' in window) {
        observer = new IntersectionObserver((ents) => {
          if (ents.some((e) => e.isIntersecting) && shown < games.length) { vs.limit += 120; more(); }
        }, { rootMargin: '900px 0px' });
        observer.observe(sentinel);
      } else { vs.limit = games.length; more(); }
    }

    function paintPanel() {
      panelHost.replaceChildren(consolePanel(name));
      paintProgress();
    }

    setMain(root, systemName(name));
    paintChips();
    paintPanel();
    paintGrid();
    ensureList(name).then(() => { if (seq === S.viewSeq) paintGrid(); },
      (err) => { if (seq === S.viewSeq) { handleError(err); gridHost.replaceChildren(h('p', { class: 'empty-note' }, 'The games of this system could not be loaded.')); } });

    return {
      kind: 'system',
      refresh() {
        const e = S.systems.find((s) => s.system === name);
        if (!e) { go('#/'); return; }
        entry.devices = e.devices; entry.games = e.games; entry.size = e.size;
        const fk = captureFocus(); paintPanel(); paintGrid(); restoreFocus(fk);
      },
      listsChanged() { },
    };
  }

  // The whole-system controls: one row per console and one for all of them.
  function consolePanel(system) {
    const entry = S.systems.find((s) => s.system === system);
    const states = (d) => (entry.devices || {})[d.name] || { state: 'none', whole: false };

    function row(label, deviceKey, state, whole, note, disabled) {
      const text = state === 'all' ? (whole ? 'Whole system' : 'Every game, one by one')
        : state === 'some' ? 'Game by game' : '';
      return h('div', { class: 'cp-row' + (disabled ? ' is-disabled' : '') },
        h('div', { class: 'cp-info' },
          h('div', { class: 'cp-name' }, h('strong', null, label),
            deviceKey !== '*' ? progressNode(deviceKey, false) : null),
          h('div', { class: 'cp-state' }, disabled ? h('span', { class: 'badge state-unsupported' }, note) : stateBadge(state, null), disabled ? null : h('span', { class: 'muted' }, text))),
        h('div', { class: 'seg', role: 'group', 'aria-label': `${label}: whole ${systemName(system)}` },
          h('button', {
            type: 'button', class: 'seg-btn', 'aria-pressed': String(state === 'all'), disabled: disabled || null,
            dataset: { fk: `cp:${deviceKey}:on`, write: '1' },
            onclick: () => changeSystem(system, deviceKey, true, state === 'all' && whole),
          }, 'All on'),
          h('button', {
            type: 'button', class: 'seg-btn', 'aria-pressed': String(state === 'none'),
            disabled: disabled || null,
            dataset: { fk: `cp:${deviceKey}:off`, write: '1' },
            onclick: () => changeSystem(system, deviceKey, false, state === 'none'),
          }, 'All off')));
    }

    const rows = S.devices.map((d) => {
      const st = states(d);
      const unsup = excludes(d.name, system);
      const unmanaged = !isManaged(d.name);
      const note = unsup ? 'Not offered to this console' : unmanaged ? 'Needs warpzone whitelist migrate' : '';
      return row(d.name, d.name, unsup ? 'none' : st.state, st.whole, note, unsup || unmanaged);
    });
    const usable = S.devices.filter((d) => !excludes(d.name, system));
    let allState = 'some';
    const sts = usable.map((d) => states(d).state);
    if (sts.length && sts.every((x) => x === 'all')) allState = 'all';
    else if (!sts.length || sts.every((x) => x === 'none')) allState = 'none';
    const allRow = S.devices.length > 1
      ? row('All consoles', '*', allState, sts.length && usable.every((d) => states(d).whole), 'No console offers it', !usable.length)
      : null;
    return h('section', { class: 'cpanel', 'aria-label': 'Consoles for this whole system' },
      h('div', { class: 'section-head' }, h('h2', null, 'Consoles'),
        h('p', { class: 'muted' }, 'Turning a whole system on or off replaces what was chosen inside it: excluded and single games are dropped.')),
      h('div', { class: 'cp-rows' }, rows, allRow));
  }

  // ---- search ----

  function localSearch(q) {
    const norm = normalizeWords(q);
    const words = norm.split(' ').filter(Boolean);
    if (!words.length) return [];
    const groups = new Map();
    for (const { game, system } of allGames()) {
      if (!game._key) game._key = titleKey(game.title || game.name || '');
      const kw = game._key.split(' ');
      if (!words.every((w) => kw.some((k) => k.startsWith(w)))) continue;
      if (!groups.has(game._key)) groups.set(game._key, { key: game._key, title: game.title, games: [] });
      groups.get(game._key).games.push({ id: game.id, system, title: game.title, devices: game.devices, cover: game.cover });
    }
    const rank = (k) => (k === norm ? 0 : k.startsWith(norm) ? 1 : 2);
    return Array.from(groups.values())
      .sort((a, b) => rank(a.key) - rank(b.key) || a.key.localeCompare(b.key)).slice(0, 100);
  }

  function viewSearch() {
    const results = h('div', { class: 'groups' });
    const head = h('h1', { class: 'search-head', role: 'status' });
    const root = h('div', { class: 'page search-page' }, head, results);

    function paint() {
      const api_ = S.searchResult && S.searchResult.q === S.q ? S.searchResult : null;
      const groups = api_ ? api_.groups : localSearch(S.q);
      const loadedAll = S.lists.size >= S.systems.length;
      const pending = !api_ && !loadedAll;
      head.textContent = groups.length
        ? `${plural(groups.length, 'title')} for "${S.q}"`
        : pending ? `Searching for "${S.q}"...` : `Nothing matches "${S.q}"`;
      results.replaceChildren(...groups.map(groupCard));
    }
    setMain(root, 'Search');
    paint();
    return {
      kind: 'search',
      refresh() { const fk = captureFocus(); paint(); restoreFocus(fk); },
      listsChanged() { if (!S.searchResult || S.searchResult.q !== S.q) paint(); },
    };
  }

  function groupCard(group) {
    const withCover = group.games.find((g) => {
      const known = S.byId.get(g.id);
      return known ? known.game.cover : g.cover !== false;
    }) || group.games[0];
    const known = (id) => (S.byId.get(id) ? S.byId.get(id).game.devices : null);
    return h('article', { class: 'group' },
      coverBox(withCover.system, withCover.id, true, 'covers'),
      h('div', { class: 'group-body' },
        h('h2', null, group.title),
        h('ul', { class: 'versions' }, group.games.map((g) =>
          h('li', null, h('a', { class: 'version', href: gameHref(g.system, g.id), dataset: { fk: 'g:' + g.id } },
            h('span', { class: 'version-sys' }, systemName(g.system)),
            consolePills(known(g.id) || g.devices)))))));
  }

  const runApiSearch = debounce(async () => {
    const q = S.q;
    if (!q) return;
    if (runApiSearch.abort) runApiSearch.abort.abort();
    const ctl = new AbortController();
    runApiSearch.abort = ctl;
    try {
      const r = await api.search(q, ctl.signal);
      if (q !== S.q) return;
      S.searchResult = { q, groups: r.groups || [] };
      if (S.view && S.view.kind === 'search') S.view.refresh();
    } catch (err) {
      if (err.name === 'AbortError') return;
      if (err.code !== 'unauthorized') S.searchResult = null; // keep the local answer
    }
  }, 170);

  function onSearchInput(e) {
    const q = e.target.value.trim();
    if (q === S.q) return;
    const had = !!S.q;
    S.q = q;
    S.searchResult = null;
    if (!q) { renderRoute(); return; }
    if (!had || !S.view || S.view.kind !== 'search') { S.viewSeq++; S.view = viewSearch(); } else S.view.refresh();
    runApiSearch();
  }

  // ===== 6. the game sheet ================================================================

  const detailEl = () => $('#detail');

  async function openDetail(system, id, silent) {
    const dlg = detailEl();
    S.detail = { system, id, data: null, kind: null };
    S.lastDetailId = id;
    if (!dlg.open) dlg.showModal();
    if (!silent) dlg.replaceChildren(h('div', { class: 'sheet-inner' }, h('p', { class: 'loading-note' }, 'Loading...')));
    try {
      const data = await api.game(id);
      if (!S.detail || S.detail.id !== id) return;
      data.id = data.id || id;
      S.detail.data = data;
      S.detail.system = data.system || system;
      paintDetail(!silent);
    } catch (err) {
      if (!S.detail || S.detail.id !== id) return;
      await handleError(err);
      closeDetailUI();
    }
  }

  function closeDetailUI() {
    if (!S.route.game) { if (detailEl().open) detailEl().close(); return; }
    if (S.gameOpenedByNav) history.back();
    else { history.replaceState(null, '', currentBase()); renderRoute(); }
  }

  function metaRows(g, meta) {
    const rows = [];
    const add = (label, value) => { if (value) rows.push([label, value]); };
    const year = meta.releasedate ? formatDate(meta.releasedate).slice(0, 4) : '';
    add('Year', year);
    add('Developer', meta.developer);
    add('Publisher', meta.publisher);
    add('Genre', meta.genre);
    add('Players', meta.players);
    if (meta.rating != null && meta.rating !== '' && !isNaN(parseFloat(meta.rating))) {
      const r = parseFloat(meta.rating);
      rows.push(['Rating', h('span', { class: 'rating' },
        h('span', { class: 'rating-bar', 'aria-hidden': 'true' }, h('i', { vars: { pct: Math.round(Math.min(1, Math.max(0, r)) * 100) + '%' } })),
        `${(r * 10).toFixed(1)} / 10`)]);
    }
    add('Size', formatBytes(g.size));
    add('Added', g.added);
    return rows;
  }

  function paintDetail(focusFirst) {
    const dlg = detailEl();
    const d = S.detail.data;
    if (!d) return;
    const system = S.detail.system;
    const meta = d.meta || {};
    const title = meta.name || d.name || d.title;
    const media = (d.media || []);
    const kind = S.detail.kind && media.includes(S.detail.kind) ? S.detail.kind
      : ['covers', '3dboxes', 'miximages', 'titlescreens', 'screenshots', 'marquees'].find((k) => media.includes(k)) || null;
    S.detail.kind = kind;
    const fk = captureFocus();

    const hero = h('div', { class: 'hero' },
      kind ? h('img', { class: 'hero-bg', src: api.mediaUrl(d.id, kind), alt: '', 'aria-hidden': 'true' }) : null,
      h('div', { class: 'hero-art' }, coverBox(system, d.id, !!kind, kind || 'covers', `${kind ? KIND_LABEL[kind] : 'Cover'} of ${title}`)));
    const thumbs = media.length > 1 ? h('div', { class: 'thumbs', role: 'group', 'aria-label': 'Images' }, media.map((k) =>
      h('button', {
        type: 'button', class: 'thumb', 'aria-pressed': String(k === kind), title: KIND_LABEL[k] || k, dataset: { fk: 'thumb:' + k },
        onclick: () => { S.detail.kind = k; paintDetail(false); restoreFocus('thumb:' + k); },
      }, h('img', { src: api.mediaUrl(d.id, k), alt: KIND_LABEL[k] || k, loading: 'lazy', decoding: 'async' })))) : null;

    const facts = metaRows(d, meta);
    const ra = d.ra ? raBadge(d.ra, true) : null;

    const inner = h('div', { class: 'sheet-inner' },
      h('button', { class: 'btn close', type: 'button', onclick: closeDetailUI, 'aria-label': 'Close', dataset: { fk: 'close' } }, 'Close'),
      h('div', { class: 'sheet-grid' },
        h('div', { class: 'sheet-media' }, hero, thumbs),
        h('div', { class: 'sheet-info' },
          h('p', { class: 'eyebrow' }, h('a', { href: `#/system/${encodeURIComponent(system)}`, onclick: () => detailEl().close() }, systemName(system))),
          h('h2', { id: 'detail-title', tabindex: '-1' }, title),
          h('div', { class: 'badges' }, ra, d.size ? h('span', { class: 'badge' }, formatBytes(d.size)) : null,
            (d.flags || []).includes('unlistable') ? h('span', { class: 'badge state-unsupported' }, 'Cannot be changed from the UI') : null),
          meta.desc ? h('p', { class: 'desc' }, meta.desc) : h('p', { class: 'desc muted' }, 'No description. This game has not been scraped.'),
          facts.length ? h('dl', { class: 'facts' }, facts.map(([k, v]) => [h('dt', null, k), h('dd', null, v)])) : null,
          consoleRows(d, system),
          versionsBlock(d),
          partsBlock(d))));
    dlg.replaceChildren(inner);
    paintProgress();
    if (focusFirst) {
      const first = $('.console-row button:not([disabled])', dlg) || $('.close', dlg);
      if (first) first.focus({ preventScroll: true });
    } else restoreFocus(fk);
  }

  function consoleRows(game, system) {
    const dev = game.devices || {};
    const locked = (game.flags || []).includes('unlistable');
    const rows = S.devices.map((d) => {
      const unsup = excludes(d.name, system) || dev[d.name] === 'unsupported';
      const st = unsup ? 'unsupported' : dev[d.name] || 'off';
      const managed = isManaged(d.name);
      const off = !managed || locked;
      const addBtn = (label) => h('button', {
        type: 'button', class: 'btn small primary', disabled: off || null, dataset: { fk: `add:${d.name}`, write: '1' },
        'aria-label': `${label} ${gameTitle(game)} on ${d.name}`,
        onclick: () => changeGame(game, system, d.name, true),
      }, label);
      const rmBtn = h('button', {
        type: 'button', class: 'btn small', disabled: off || null, dataset: { fk: `rm:${d.name}`, write: '1' },
        'aria-label': `Remove ${gameTitle(game)} from ${d.name}`,
        onclick: () => changeGame(game, system, d.name, false),
      }, 'Remove');
      return h('li', { class: 'console-row state-row-' + st },
        h('div', { class: 'console-id' },
          h('strong', null, d.name),
          stateBadge(st),
          st === 'on' || st === 'partial' ? progressNode(d.name, true) : null,
          !managed ? h('span', { class: 'muted small-text' }, 'Whitelist needs "warpzone whitelist migrate"') : null,
          st === 'unsupported' ? h('span', { class: 'muted small-text' }, 'This console does not take this system') : null),
        h('div', { class: 'console-actions' },
          st === 'off' ? addBtn('Add') : null,
          st === 'partial' ? [addBtn('Complete'), rmBtn] : null,
          st === 'on' ? rmBtn : null));
    });
    const eligible = S.devices.filter((d) => !excludes(d.name, system) && dev[d.name] !== 'unsupported');
    const canAdd = eligible.some((d) => dev[d.name] !== 'on');
    const canRemove = S.devices.some((d) => dev[d.name] === 'on' || dev[d.name] === 'partial');
    return h('section', { class: 'consoles-block', 'aria-label': 'Consoles' },
      h('h3', null, 'Consoles'),
      h('ul', { class: 'console-list' }, rows),
      S.devices.length > 1 ? h('div', { class: 'all-actions' },
        h('button', {
          type: 'button', class: 'btn primary', disabled: !canAdd || locked || null, dataset: { fk: 'add:*', write: '1' },
          onclick: () => changeGame(game, system, '*', true),
        }, 'Add to all consoles'),
        h('button', {
          type: 'button', class: 'btn', disabled: !canRemove || locked || null, dataset: { fk: 'rm:*', write: '1' },
          onclick: () => changeGame(game, system, '*', false),
        }, 'Remove from all consoles')) : null,
      h('p', { class: 'muted small-text' }, 'A removed game leaves the console within about an hour: the console deletes it on its next cleanup.'));
  }

  function gameTitle(game) { return (game.meta && game.meta.name) || game.name || game.title; }

  function versionsBlock(game) {
    const others = game.versions || [];
    if (!others.length) return null;
    return h('section', { class: 'versions-block' },
      h('h3', null, 'Other versions of this title'),
      h('ul', { class: 'version-list' }, others.map((v) => {
        const st = S.byId.get(v.id);
        return h('li', null, h('a', {
          class: 'version', href: gameHref(v.system, v.id), dataset: { fk: 'v:' + v.id },
          onclick: (e) => {
            e.preventDefault();
            history.replaceState(null, '', gameHref(v.system, v.id));
            renderRoute();
          },
        }, h('span', { class: 'version-sys' }, systemName(v.system)), h('span', { class: 'muted' }, v.title),
        st ? consolePills(st.game.devices) : null));
      })));
  }

  function partsBlock(game) {
    const parts = game.parts || [];
    if (parts.length < 2) return null;
    return h('details', { class: 'parts' },
      h('summary', null, `${plural(parts.length, 'part')} on disk`),
      h('ul', null, parts.map((p) => h('li', null, h('code', null, p.path), ' ', h('span', { class: 'muted' }, formatBytes(p.size))))));
  }

  // ===== 7. changes ========================================================================

  function setBusy(on) {
    S.busy = on;
    document.body.classList.toggle('busy', on);
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

  // Serialise writes: a second click while one runs would carry an etag the first just changed.
  async function guarded(fn) {
    if (S.busy) { toast('Another change is still being applied.', 'info'); return null; }
    setBusy(true);
    try { return await fn(); } catch (err) { await handleError(err); return null; } finally { setBusy(false); }
  }

  // The consoles a change touches, and the etag the server must find for each of them.
  function targets(device, system) {
    if (device === '*') return S.devices.filter((d) => !excludes(d.name, system)).map((d) => d.name);
    return [device];
  }

  function expectFor(names) {
    const out = {};
    for (const n of names) if (S.etags[n]) out[n] = S.etags[n];
    return out;
  }

  // After a write: remember the new etags, patch the cached lists with what changed, then
  // reload the aggregates (counts and whole-system states) and start watching progress.
  async function afterWrite(res, system) {
    for (const d of (res && res.devices) || []) {
      if (d.etag) S.etags[d.device] = d.etag;
      for (const id of d.on || []) { const e = S.byId.get(id); if (e) e.game.devices[d.device] = 'on'; }
      for (const id of d.off || []) { const e = S.byId.get(id); if (e) e.game.devices[d.device] = 'off'; }
    }
    await Promise.all([loadDevices(), loadSystems()]);
    renderConsoles();
    refreshView();
    progressSoon();
  }

  function skippedNote(res) {
    const sk = (res && res.skipped) || [];
    if (!sk.length) return '';
    return ' Skipped: ' + sk.map((s) => `${s.device} (${s.reason})`).join(', ') + '.';
  }

  async function changeGame(game, system, device, on) {
    await guarded(async () => {
      const names = targets(device, system);
      const res = await api.changes({
        changes: [{ device, system, game: game.id, on }],
        expect: expectFor(names), dry_run: false,
      });
      await afterWrite(res, system);
      const where = device === '*' ? 'all consoles' : device;
      toast(`${on ? 'Added' : 'Removed'} "${gameTitle(game)}" ${on ? 'to' : 'from'} ${where}.` + skippedNote(res), 'ok');
      // The sheet shows the fresh state of this game.
      if (S.detail && S.detail.id === game.id) await openDetail(S.detail.system, game.id, true);
    });
  }

  async function changeSystem(system, device, on, already) {
    if (already) { toast(on ? 'Already on.' : 'Already off.', 'info'); return; }
    await guarded(async () => {
      const names = targets(device, system);
      const body = { changes: [{ device, system, on }], expect: expectFor(names) };
      const dry = await api.changes({ ...body, dry_run: true });
      const ok = await confirmSystem(system, device, on, dry);
      if (!ok) return;
      // The etags may have moved while the dialog was open: use the current ones.
      const res = await api.changes({ ...body, expect: expectFor(names), dry_run: false });
      await afterWrite(res, system);
      await ensureList(system, true);
      refreshView();
      toast(`${systemName(system)} is now ${on ? 'on' : 'off'} for ${device === '*' ? 'all consoles' : device}.` + skippedNote(res), 'ok');
    });
  }

  // The confirmation dialog, filled from the dry run: what would be downloaded and removed per
  // console. Resolves true on confirm, false on cancel or Escape.
  function confirmSystem(system, device, on, dry) {
    const dlg = $('#confirm');
    const devs = (dry && dry.devices) || [];
    const lines = devs.map((d) => {
      const got = (d.on || []).length;
      const lost = (d.off || []).length;
      const parts = [];
      if (d.bytes_on > 0 || got) parts.push(h('li', { class: 'dl' }, 'This will download ', h('strong', null, formatBytes(d.bytes_on)),
        ` on ${d.device} (${plural(got, 'game')}).`));
      if (d.bytes_off > 0 || lost) parts.push(h('li', { class: 'rm' }, 'This will remove ', h('strong', null, formatBytes(d.bytes_off)),
        ` from ${d.device} (${plural(lost, 'game')}).`));
      if (!parts.length) parts.push(h('li', null, `No game changes on ${d.device}.`));
      return h('li', null, h('strong', { class: 'dev' }, d.device), h('ul', null, parts));
    });
    const skipped = ((dry && dry.skipped) || []).map((s) => `${s.device} (${s.reason})`);
    return new Promise((resolve) => {
      let result = false;
      const confirmBtn = h('button', { type: 'button', class: 'btn ' + (on ? 'primary' : 'danger'), onclick: () => { result = true; dlg.close(); } },
        on ? 'Turn on' : 'Turn off');
      const cancelBtn = h('button', { type: 'button', class: 'btn', onclick: () => dlg.close() }, 'Cancel');
      dlg.replaceChildren(h('div', { class: 'modal-inner' },
        h('h2', { id: 'confirm-title' }, `Turn ${systemName(system)} ${on ? 'on' : 'off'} for ${device === '*' ? 'all consoles' : device}?`),
        h('p', null, on
          ? 'The whole system will be offered to the console, including games added to it later. Games you excluded come back and single-game choices inside it are dropped.'
          : 'Every game of this system will be taken off the console, including single games you added one by one.'),
        devs.length ? h('ul', { class: 'effects' }, lines) : null,
        skipped.length ? h('p', { class: 'muted' }, 'Skipped: ' + skipped.join(', ') + '.') : null,
        on ? null : h('p', { class: 'muted' }, 'The files are deleted from the console about an hour later; turning it on again before then costs nothing.'),
        h('div', { class: 'actions' }, cancelBtn, confirmBtn)));
      dlg.addEventListener('close', () => resolve(result), { once: true });
      dlg.showModal();
      (on ? confirmBtn : cancelBtn).focus();
    });
  }

  async function refreshCatalog() {
    try {
      await api.refreshCatalog();
      toast('A catalog scan started on the hub. Reload in a minute to see what it finds.', 'ok');
    } catch (err) {
      if (err.status === 409) toast('A catalog scan is already running.', 'info');
      else await handleError(err);
    }
  }

  // ===== 8. keyboard navigation, router, login, boot ======================================

  // Arrow keys move focus to the nearest focusable element in that direction, so a grid, a
  // shelf and the sheet can be crossed with a d-pad as well as Tab. Enter activates (links and
  // buttons do that natively); Escape closes the sheet or the dialog (<dialog> does that).
  const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), summary, [tabindex="0"]';

  function onArrow(e) {
    const dir = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
    if (!dir || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    const t = e.target;
    if (t.tagName === 'SELECT') return;
    if (t.tagName === 'INPUT' && dir[1] === 0 && t.type !== 'button') return; // editing text
    const open = $$('dialog[open]');
    const scope = open.length ? open[open.length - 1] : document;
    if (scope.id === 'login') return;
    const from = t.getBoundingClientRect();
    const cx = from.left + from.width / 2;
    const cy = from.top + from.height / 2;
    let best = null;
    let bestScore = Infinity;
    for (const el of $$(FOCUSABLE, scope)) {
      if (el === t || el.closest('[hidden]') || el.classList.contains('skip')) continue;
      const r = el.getBoundingClientRect();
      if (!r.width && !r.height) continue;
      const px = r.left + r.width / 2;
      const py = r.top + r.height / 2;
      const dx = px - cx;
      const dy = py - cy;
      const along = dir[0] ? dx * dir[0] : dy * dir[1];
      if (along <= 4) continue;
      const across = Math.abs(dir[0] ? dy : dx);
      const score = along + across * 2.5;
      if (score < bestScore) { bestScore = score; best = el; }
    }
    if (best) { e.preventDefault(); best.focus(); }
  }

  function parseHash() {
    const raw = location.hash.replace(/^#/, '') || '/';
    const [path, query] = raw.split('?');
    const params = new URLSearchParams(query || '');
    return { path: path || '/', game: params.get('game') };
  }

  function go(hash) { location.hash = hash; }

  function renderRoute() {
    const prev = S.route;
    const route = parseHash();
    if (!prev.game && route.game && !S.detailFromLoad) S.gameOpenedByNav = true;
    S.route = route;
    const pathChanged = prev.path !== route.path || !S.view;
    if (pathChanged && S.q && prev.path !== null) { S.q = ''; S.searchResult = null; $('#search').value = ''; }
    if (pathChanged || (!S.q && S.view && S.view.kind === 'search')) {
      if (pathChanged) $('#main').setAttribute('aria-busy', 'true');
      S.viewSeq++;
      if (S.q) S.view = viewSearch();
      else mountRouteView(route);
    }
    if (route.game) {
      const [system, id] = route.game.split('/');
      if (!S.detail || S.detail.id !== id || !detailEl().open) openDetail(system, id);
    } else {
      S.detail = null;
      if (detailEl().open) detailEl().close();
    }
    if (!route.game) S.gameOpenedByNav = false;
  }

  function mountRouteView(route) {
    const m = /^\/system\/(.+)$/.exec(route.path);
    if (m) {
      const name = decodeURIComponent(m[1]);
      viewSystem(name).then((v) => { S.view = v; });
      S.view = { kind: 'system', refresh() { } };
    } else {
      S.view = viewHome();
    }
    window.scrollTo(0, 0);
  }

  // ---- login ----

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

  // ---- boot ----

  async function boot() {
    try {
      await Promise.all([loadDevices(), loadSystems()]);
    } catch (err) {
      if (err.code !== 'unauthorized') {
        $('#main').replaceChildren(h('div', { class: 'page empty' }, h('h1', null, 'The library is not available'),
          h('p', null, humanError(err)), h('button', { class: 'btn', type: 'button', onclick: boot }, 'Try again')));
        $('#main').setAttribute('aria-busy', 'false');
      }
      return;
    }
    renderConsoles();
    S.view = null;
    S.detailFromLoad = !!parseHash().game;
    S.route = { path: null, game: null };
    renderRoute();
    S.detailFromLoad = false;
    pollProgress();
    preloadLists();
  }

  function init() {
    api.onUnauthorized(() => showLogin(''));
    $('#login').addEventListener('cancel', (e) => e.preventDefault());
    $('#login-form').addEventListener('submit', submitLogin);
    window.addEventListener('hashchange', renderRoute);
    document.addEventListener('keydown', onArrow);
    document.addEventListener('visibilitychange', () => { if (!document.hidden && S.devices.length) schedulePoll(200); });

    const search = $('#search');
    $('#search-form').addEventListener('submit', (e) => { e.preventDefault(); const first = $('.version, .card', $('#main')); if (first) first.focus(); });
    search.addEventListener('input', onSearchInput);
    search.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { search.value = ''; onSearchInput({ target: search }); search.blur(); }
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) && !$('dialog[open]')) { e.preventDefault(); search.focus(); }
    });

    const dlg = detailEl();
    dlg.addEventListener('click', (e) => { if (e.target === dlg) closeDetailUI(); });
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); closeDetailUI(); });
    $('.brand').addEventListener('click', () => {
      // The brand link also clears a search that is showing while the route is already home.
      if (S.q) { $('#search').value = ''; onSearchInput({ target: $('#search') }); }
    });
    dlg.addEventListener('close', () => {
      // A write redraws the grid behind the sheet, so the browser's own "return focus to the
      // opener" can find nothing; put it back on the game's card.
      const id = S.lastDetailId;
      setTimeout(() => { if (id && (!document.activeElement || document.activeElement === document.body)) restoreFocus('g:' + id); }, 0);
    });
    $('#confirm').addEventListener('click', (e) => { if (e.target === $('#confirm')) $('#confirm').close(); });

    boot();
  }

  init();
})();
