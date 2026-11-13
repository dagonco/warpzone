/*
 * warpzone library UI: the pages.
 *
 * Home, systems, one system, the consoles, search and one game. Each view function builds its
 * nodes, returns {kind, refresh(), ...} and loads what it needs in the background; `dead` is
 * set by the router when the page is left, and async work checks it before drawing. A view's
 * refresh() is called when the data it shows changed (a write, a progress answer, a catalog
 * run) and updates in place where it can, so focus and scroll stay where they are.
 */
(function () {
  'use strict';

  const W = window.WZ;
  const { api, formatBytes, formatDate, formatDay, formatMoment, formatPlaytime, timeAgo, normalizeWords, titleKey, h, $, $$, plural,
    debounce, icon, tint, systemName, systemCode, systemColor, consoleCode, parseTitle, languageName, store, ui } = W;
  const { S } = store;
  const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ===== helpers ===========================================================================

  function setMain(node, title) {
    const main = $('#main');
    main.replaceChildren(node);
    main.setAttribute('aria-busy', 'false');
    document.title = title ? `${title} - warpzone library` : 'warpzone library';
  }

  const captureFocus = () => {
    const el = document.activeElement;
    return el && el.dataset ? el.dataset.fk || null : null;
  };

  function restoreFocus(fk) {
    if (!fk) return;
    const el = $(`[data-fk="${CSS.escape(fk)}"]`);
    if (el) el.focus({ preventScroll: true });
  }

  const setTint = (el, color) => { for (const [k, v] of Object.entries(tint(color))) el.style.setProperty('--' + k, v); };

  function sechead(title, note) {
    return h('div', { class: 'sechead' }, h('h2', null, h('i'), title), h('span', { class: 'rule' }), note ? h('span', { class: 'n' }, note) : null);
  }

  function emptyPanel(title, text, ...extra) {
    return h('div', { class: 'empty' }, h('b', null, title), text ? h('p', null, text) : null, ...extra);
  }

  // After the browser's back button the page returns to where it was, once the page is built
  // tall enough to scroll there.
  let pendingScroll = null;
  let pendingFocus = null;
  const scrollMemory = new Map();
  const setPendingScroll = (y, fk) => { pendingScroll = y == null ? null : y; pendingFocus = fk || null; };
  function settled() {
    if (pendingScroll != null) { window.scrollTo(0, pendingScroll); pendingScroll = null; }
    if (pendingFocus) { restoreFocus(pendingFocus); pendingFocus = null; }
  }

  const allGames = () => {
    const out = [];
    for (const [system, list] of S.lists) for (const g of list.games) out.push({ game: g, system });
    return out;
  };

  // ===== home ==============================================================================

  // The games of "Now showing": a few covers per system, picked by the day so the shelf changes
  // from one day to the next and not from one load to the next. Details come after, one by one.
  let featured = null;
  const featuredData = new Map();
  let featuredToken = 0;

  function dayHash(text) {
    const day = new Date().toISOString().slice(0, 10);
    let n = 17;
    for (const ch of text + day) n = (n * 31 + ch.charCodeAt(0)) >>> 0;
    return n;
  }

  function pickFeatured() {
    if (featured || !S.systems.length) return false;
    if (S.lists.size < Math.min(4, S.systems.length)) return false;
    const perSystem = [];
    for (const [system, list] of S.lists) {
      const covers = list.games.filter((g) => g.cover);
      if (!covers.length) continue;
      const start = dayHash(system) % covers.length;
      const picks = [covers[start]];
      if (covers.length > 3) picks.push(covers[(start + Math.floor(covers.length / 2)) % covers.length]);
      perSystem.push(picks.map((g) => ({ id: g.id, system, name: g.name || g.title })));
    }
    perSystem.sort((a, b) => a[0].system.localeCompare(b[0].system));
    const out = [];
    for (let round = 0; round < 2; round++) for (const picks of perSystem) if (picks[round]) out.push(picks[round]);
    featured = out.slice(0, 8);
    return featured.length > 0;
  }

  async function loadFeaturedDetails(onEach) {
    const token = ++featuredToken;
    for (const f of featured || []) {
      if (token !== featuredToken) return;
      if (featuredData.has(f.id)) continue;
      try {
        featuredData.set(f.id, await api.game(f.id));
        onEach(f.id);
      } catch (err) { if (err.code === 'unauthorized') return; }
    }
  }

  const HERO_KINDS = ['miximages', 'screenshots', 'titlescreens', 'covers'];

  function createHero(view) {
    let idx = 0;
    let paused = REDUCED;
    let timer = null;
    const host = h('section', { class: 'hero', 'aria-label': 'Now showing', hidden: true });

    function paint(swap) {
      if (!featured || !featured.length) { host.hidden = true; return; }
      host.hidden = false;
      idx = ((idx % featured.length) + featured.length) % featured.length;
      const f = featured[idx];
      const det = featuredData.get(f.id);
      const meta = (det && det.meta) || {};
      const t = parseTitle(meta.name || f.name);
      const media = (det && det.media) || ['covers'];
      const kind = HERO_KINDS.find((k) => media.includes(k)) || 'covers';
      const year = meta.releasedate ? formatDate(meta.releasedate).slice(0, 4) : '';
      const info = [year, (meta.genre || '').split(' / ')[0], meta.players ? `${meta.players} ${meta.players === '1' ? 'player' : 'players'}` : '']
        .filter(Boolean);
      setTint(host, systemColor(f.system));
      const hadFocus = host.contains(document.activeElement) ? document.activeElement.dataset.fk : null;
      host.replaceChildren(h('div', { class: 'hero-in' },
        h('div', { class: 'hero-text' },
          h('div', { class: 'eyebrow' }, h('span', { class: 'play', 'aria-hidden': 'true' }), 'Now showing',
            h('span', { class: 'idx' }, `${String(idx + 1).padStart(2, '0')} / ${String(featured.length).padStart(2, '0')}`)),
          h('h2', { class: 'hero-title' }, t.name),
          h('div', { class: 'sysline' },
            h('a', { class: 'badge', href: ui.systemHref(f.system), title: systemName(f.system) }, systemCode(f.system)),
            ui.regionChip(t.region),
            info.map((x) => h('span', null, x)),
            det && det.ra && det.ra.state === 'supported' ? h('span', { class: 'badge ra' }, icon('trophy'), 'RA') : null),
          meta.desc ? h('p', { class: 'blurb' }, meta.desc) : null,
          h('div', { class: 'hero-actions' },
            h('a', { class: 'btn primary', href: ui.gameHref(f.system, f.id), dataset: { fk: 'hero-open' } }, 'View game', icon('right')),
            h('a', { class: 'btn', href: ui.systemHref(f.system), title: systemName(f.system) }, `Browse ${systemCode(f.system)}`)),
          h('div', { class: 'hero-dots', role: 'group', 'aria-label': 'Featured games' },
            featured.map((_, k) => h('button', {
              type: 'button', 'aria-label': `Show featured game ${k + 1} of ${featured.length}`, 'aria-current': k === idx ? 'true' : null,
              dataset: { fk: 'hero-dot:' + k }, onclick: () => set(k),
            })),
            h('button', {
              type: 'button', class: 'hero-pause', 'aria-pressed': String(paused), dataset: { fk: 'hero-pause' },
              'aria-label': paused ? 'Resume the slideshow' : 'Pause the slideshow', title: paused ? 'Resume the slideshow' : 'Pause the slideshow',
              onclick: () => { paused = !paused; paint(false); },
            }, icon(paused ? 'play' : 'pause')))),
        h('div', { class: 'cab' },
          h('div', { class: 'monitor' },
            h('a', { class: 'screen', href: ui.gameHref(f.system, f.id), tabindex: '-1', 'aria-hidden': 'true' },
              ui.artBox({ id: f.id, system: f.system, kind, has: true, cls: kind === 'screenshots' ? 'shot' : '', eager: true })),
            h('div', { class: 'led-strip', 'aria-hidden': 'true' }, h('i'), h('i'), h('i')))))
      );
      if (swap && !REDUCED) { host.classList.remove('swap'); void host.offsetWidth; host.classList.add('swap'); }
      restoreFocus(hadFocus);
    }

    function set(i) { idx = i; paint(true); }

    timer = setInterval(() => {
      if (view.dead || paused || document.hidden || !featured || featured.length < 2) return;
      if (host.matches(':hover') || host.contains(document.activeElement)) return;
      set(idx + 1);
    }, 9000);

    return {
      node: host, paint,
      detailArrived(fid) { if (featured && featured[idx] && featured[idx].id === fid) paint(false); },
      dispose() { clearInterval(timer); },
    };
  }

  function consoleCard(d) {
    const hold = store.holdings(d.name);
    const none = d.whitelist && d.whitelist.state === 'missing';
    return h('a', {
      class: 'stat', href: '#/consoles', title: d.name,
      dataset: { pdev: d.name, pk: 'host', fk: 'cc:' + d.name },
    },
    h('span', { class: 'bigled' }),
    h('span', { class: 'sc-main' }, h('span', { class: 'nm' }, d.name), ui.pnode('span', d.name, 'state', 'stt')),
    ui.segs(d.name),
    h('span', { class: 'sc-f' },
      hold.complete
        ? [h('b', null, hold.games.toLocaleString('en-US')), ` ${hold.games === 1 ? 'game' : 'games'} · `, h('b', null, formatBytes(hold.size))]
        : 'Counting games...',
      ui.pnode('span', d.name, 'left', 'left'),
      none ? h('span', { class: 'tag', title: 'The console has no whitelist yet. The first change made here creates it.' }, 'No whitelist yet') : null),
    ui.diskMeter(d));
  }

  // ----- the records at the end of the home page -------------------------------------------

  // A small block of the records panel: a title and rows. Null when there is nothing to list.
  function recBlock(title, ...rows) {
    const kids = rows.flat(Infinity).filter(Boolean);
    return kids.length ? h('div', { class: 'rec-b' }, h('h3', null, title), ...kids) : null;
  }

  const fmt = (n) => Number(n).toLocaleString('en-US');

  // RetroAchievements: the points and the games beaten and mastered, from /ra/summary.
  function raRecords(ra) {
    const t = ra.total;
    if (!t || !(t.games > 0) || t.unlocked == null) return null;
    const points = t.points_unlocked == null ? null : fmt(t.points_unlocked) + (t.points_complete === false ? '+' : '');
    const share = t.achievements > 0 ? Math.min(1, t.unlocked / t.achievements) : 0;
    return recBlock('RetroAchievements',
      h('p', { class: 'rec-user' }, ra.user),
      points ? h('p', { class: 'rec-big', title: t.points_complete === false ? 'At least this many: the unlock dates of some played games are not downloaded yet.' : null },
        h('b', null, points), ' points') : null,
      h('dl', { class: 'rec-stats' },
        h('div', null, h('dt', null, 'Played'), h('dd', null, `${fmt(t.played)} / ${fmt(t.games)}`)),
        h('div', null, h('dt', null, 'Beaten'), h('dd', null, fmt(t.beaten))),
        h('div', null, h('dt', null, 'Mastered'), h('dd', null, fmt(t.mastered)))),
      h('div', { class: 'rec-line' }, h('span', { class: 'rec-bar', 'aria-hidden': 'true' }, h('i', { vars: { w: String(share) } })),
        h('span', null, `${fmt(t.unlocked)} / ${fmt(t.achievements)} achievements`)));
  }

  // The share of each system's achievements the user has unlocked, systems with a game played.
  function raSystems(ra) {
    const rows = (ra.systems || []).filter((x) => x.played > 0 && x.achievements > 0 && x.unlocked != null)
      .map((x) => ({ system: x.system, pct: Math.min(1, x.unlocked / x.achievements), unlocked: x.unlocked, of: x.achievements }))
      .sort((a, b) => b.pct - a.pct || a.system.localeCompare(b.system)).slice(0, 6);
    return recBlock('Achievements by system', rows.map((r) => h('div', { class: 'rec-row', vars: ui.systemTint(r.system), title: `${systemName(r.system)}: ${fmt(r.unlocked)} of ${fmt(r.of)}` },
      h('b', null, systemCode(r.system)),
      h('span', { class: 'rec-bar', 'aria-hidden': 'true' }, h('i', { vars: { w: String(r.pct) } })),
      h('em', null, r.pct > 0 && r.pct < 0.01 ? '<1%' : `${Math.round(r.pct * 100)}%`))));
  }

  // The games played most, from the totals the system lists carry (the lists load in the
  // background, so this grows while they arrive).
  function playRecords() {
    const played = allGames().filter((x) => x.game.play && x.game.play.plays > 0);
    if (!played.length) return null;
    const top = played.sort((a, b) => b.game.play.plays - a.game.play.plays || (b.game.play.playtime || 0) - (a.game.play.playtime || 0)
      || (a.game.name || a.game.title).localeCompare(b.game.name || b.game.title)).slice(0, 5);
    const total = played.reduce((n, x) => n + (x.game.play.playtime || 0), 0);
    const plays = played.reduce((n, x) => n + x.game.play.plays, 0);
    return recBlock('Most played',
      top.map((x, i) => {
        const t = parseTitle(x.game.name || x.game.title);
        const time = formatPlaytime(x.game.play.playtime);
        return h('a', { class: 'rec-row game', href: ui.gameHref(x.system, x.game.id), vars: ui.systemTint(x.system), title: `${t.full} (${systemName(x.system)})`, dataset: { fk: 'mp:' + x.game.id } },
          h('b', null, String(i + 1)),
          h('span', { class: 'rn' }, t.name),
          h('em', null, [plural(x.game.play.plays, 'play'), time].filter(Boolean).join(' · ')));
      }),
      h('p', { class: 'rec-foot' }, `${plural(played.length, 'game')} played, ${plural(plays, 'play')}${total ? ' · ' + formatPlaytime(total) + ' in all' : ''}`));
  }

  function viewHome() {
    const view = { kind: 'home', dead: false };
    const hero = createHero(view);
    const introHost = h('div', { class: 'intro-host' });
    const stripHost = h('div', { class: 'strip' });
    const stripSec = h('section', { class: 'sec', 'aria-label': 'Consoles' }, sechead('Consoles'), stripHost);
    const sysHost = h('div', { class: 'syscards' });
    const sysNote = h('span', { class: 'n' });
    const sysSec = h('section', { class: 'sec', 'aria-label': 'Systems' },
      h('div', { class: 'sechead' }, h('h2', null, h('i'), 'Systems'), h('span', { class: 'rule' }), sysNote), sysHost);
    const shelves = h('div', { class: 'shelves' });
    const queueHome = W.queueUi.homeSection();
    const resumeHost = h('div', { class: 'hrow' });
    const resumeSec = h('section', { class: 'sec', 'aria-label': 'Continue playing', hidden: true }, sechead('Continue playing'), resumeHost);
    const unfinishedHost = h('div', { class: 'hrow' });
    const unfinishedSec = h('section', { class: 'sec', 'aria-label': 'Unfinished', hidden: true }, sechead('Unfinished'), unfinishedHost);
    const similarHost = h('div', { class: 'hrow' });
    const similarHead = h('h2', null, h('i'), '');
    const similarSec = h('section', { class: 'sec', 'aria-label': 'Because you play', hidden: true },
      h('div', { class: 'sechead' }, similarHead, h('span', { class: 'rule' })), similarHost);
    const recordsHost = h('div', { class: 'rec-grid' });
    const recordsSec = h('section', { class: 'sec records', 'aria-label': 'Records', hidden: true },
      sechead('Records'), h('div', { class: 'panel rec' }, recordsHost));
    const rescan = h('button', { class: 'btn', type: 'button', onclick: store.refreshCatalog, dataset: { fk: 'rescan' } }, 'Rescan the library');
    const root = h('div', { class: 'page home' }, introHost, hero.node, stripSec, queueHome.node, resumeSec, unfinishedSec, similarSec, sysSec, shelves, recordsSec,
      h('div', { class: 'footer-actions' }, rescan,
        h('span', { class: 'mute' }, 'Runs the catalog scan on the hub now. It also runs by itself every few hours.')));

    // The top of the page follows the catalog: why there is nothing to show, or a scan running.
    function paintIntro() {
      const running = !!(S.catalog && S.catalog.running);
      const failed = S.catalog && S.catalog.last_run && S.catalog.last_run.ok === false;
      rescan.disabled = running;
      rescan.textContent = running ? 'Scanning the library...' : 'Rescan the library';
      let panel = null;
      if (!S.systems.length) {
        panel = running
          ? emptyPanel('Scanning', 'The catalog is being built on the hub. This page updates when it finishes.')
          : emptyPanel('The catalog has not been built yet.',
            failed
              ? `The last scan failed: ${S.catalog.last_run.message || 'unknown error'}. Run "warpzone catalog" on the hub, or press the button below.`
              : 'Nothing is listed until the hub scans the library. Run "warpzone catalog" on the hub, or press the button below.');
      } else if (running) {
        panel = h('p', { class: 'note', role: 'status' }, 'A scan of the library is running; this page updates when it finishes.');
      }
      introHost.replaceChildren(...(panel ? [panel] : []));
      const totalGames = S.systems.reduce((n, s) => n + (s.games || 0), 0);
      const totalSize = S.systems.reduce((n, s) => n + (s.size || 0), 0);
      sysNote.textContent = S.systems.length ? `${plural(totalGames, 'game')} · ${plural(S.systems.length, 'system')} · ${formatBytes(totalSize)}` : '';
      stripSec.hidden = !S.devices.length;
      sysSec.hidden = !S.systems.length;
    }

    const paintStrip = () => stripHost.replaceChildren(...S.devices.map(consoleCard));

    // "Continue playing" and the records come from their own endpoints and are optional: with
    // no play history or no RetroAchievements user the section is simply not there.
    function paintResume() {
      const items = (S.recent || []).slice(0, 12);
      resumeSec.hidden = !items.length;
      resumeHost.replaceChildren(...items.map(ui.resumeCard));
    }

    // The two recommendation shelves come from /recommendations; a shelf with no games is not there.
    function paintRecs() {
      const unfinished = (S.recs && S.recs.unfinished) || [];
      unfinishedSec.hidden = !unfinished.length;
      unfinishedHost.replaceChildren(...unfinished.map((r) => ui.recCard(r, 'unfinished')));
      const similar = S.recs && S.recs.similar;
      similarSec.hidden = !similar;
      if (similar) {
        const t = parseTitle(similar.to.name || similar.to.title);
        similarSec.setAttribute('aria-label', `Because you play ${t.name}`);
        similarHead.replaceChildren(h('i'), `Because you play ${t.name}`);
        similarHost.replaceChildren(...similar.games.map((r) => ui.recCard(r, 'similar')));
      }
    }

    function paintRecords() {
      const blocks = [S.ra ? raRecords(S.ra) : null, S.ra ? raSystems(S.ra) : null, playRecords()].filter(Boolean);
      recordsSec.hidden = !blocks.length;
      recordsHost.replaceChildren(...blocks);
    }

    function loadExtras() {
      store.loadHomeExtras().then((asked) => {
        if (!asked || view.dead) return;
        const fk = captureFocus();
        paintResume(); paintRecs(); paintRecords();
        restoreFocus(fk);
      });
    }
    const paintSystems = () => sysHost.replaceChildren(...S.systems.map(ui.sysCard));

    function paintShelves() {
      const all = allGames();
      const parts = [];
      const nameOf = (x) => x.game.name || x.game.title;
      const recent = all.filter((x) => x.game.added)
        .sort((a, b) => b.game.added.localeCompare(a.game.added) || nameOf(a).localeCompare(nameOf(b))).slice(0, 24);
      if (recent.length) parts.push(shelf('Recently added', recent));
      const onSome = all.filter((x) => Object.values(x.game.devices).some((v) => v === 'on' || v === 'partial'))
        .sort((a, b) => (b.game.added || '').localeCompare(a.game.added || '') || nameOf(a).localeCompare(nameOf(b))).slice(0, 24);
      if (onSome.length) parts.push(shelf('On your consoles', onSome));
      if (!parts.length && S.systems.length && S.lists.size < S.systems.length) {
        parts.push(h('div', { class: 'hrow' }, ui.skeletonTiles(6)));
      }
      shelves.replaceChildren(...parts);
    }

    function shelf(title, items) {
      return h('section', { class: 'sec', 'aria-label': title }, sechead(title),
        h('div', { class: 'hrow' }, items.map((x) => ui.tile(x.game, x.system, { sys: true }))));
    }

    function paintAll() {
      const fk = captureFocus();
      paintIntro(); paintStrip(); queueHome.paint(); paintResume(); paintRecs(); paintSystems(); paintShelves(); paintRecords();
      loadExtras();
      store.ensureQueue(15000);
      if (pickFeatured() || (featured && !view.heroStarted)) {
        view.heroStarted = true;
        hero.paint(false);
        loadFeaturedDetails((fid) => { if (!view.dead) hero.detailArrived(fid); });
      }
      ui.paintProgress();
      restoreFocus(fk);
    }

    setMain(root, '');
    paintAll();
    settled();
    Object.assign(view, {
      refresh: paintAll,
      listsChanged: paintAll,
      queueChanged() { queueHome.paint(); },
      dispose() { hero.dispose(); },
    });
    return view;
  }

  // ===== systems ===========================================================================

  function viewSystems() {
    const view = { kind: 'systems', dead: false };
    const grid = h('div', { class: 'syscards' });
    const note = h('span', { class: 'n' });
    const root = h('div', { class: 'page' },
      h('section', { class: 'sec', 'aria-label': 'Systems' },
        h('div', { class: 'sechead' }, h('h2', null, h('i'), 'Systems'), h('span', { class: 'rule' }), note), grid));
    const paint = () => {
      const fk = captureFocus();
      note.textContent = plural(S.systems.length, 'system');
      grid.replaceChildren(...(S.systems.length ? S.systems.map(ui.sysCard) : [emptyPanel('The catalog has not been built yet.', 'Nothing is listed until the hub scans the library.')]));
      restoreFocus(fk);
    };
    setMain(root, 'Systems');
    paint();
    settled();
    return Object.assign(view, { refresh: paint, listsChanged: paint });
  }

  // ===== one system ========================================================================

  // Filter, sort and how far the grid was scrolled, kept per system for the way back.
  const filterMemory = new Map();

  function viewSystem(name) {
    const view = { kind: 'system', dead: false };
    const entry = store.system(name);
    if (!entry) {
      setMain(h('div', { class: 'page' }, emptyPanel('Unknown system', `There is no system called "${name}" in the catalog.`,
        h('a', { class: 'btn', href: '#/' }, 'Back to the library'))), 'Unknown system');
      return Object.assign(view, { refresh() {} });
    }
    const vs = filterMemory.get(name) || { q: '', show: 'all', sort: 'name', limit: 120 };
    filterMemory.set(name, vs);
    let observer = null;
    const full = systemName(name);

    const stats = h('p', { class: 'stats' });
    const levers = h('div', { class: 'levers', role: 'group', 'aria-label': `${full} on each console` });
    const leverAll = h('div', { class: 'lever-all' });
    const unclaimedHost = h('div', { class: 'unclaimed-host' });
    const gridHost = h('div', { class: 'grid-host' });
    const result = h('span', { class: 'result', role: 'status' });

    const input = h('input', {
      type: 'search', value: vs.q, placeholder: `Filter ${full}`, spellcheck: 'false', autocapitalize: 'off', autocomplete: 'off',
      'aria-label': `Filter the games of ${full}`, dataset: { fk: 'filter' },
      oninput: debounce((e) => { vs.q = e.target.value; vs.limit = 120; paintGrid(); }, 90),
    });
    const showOptions = () => [
      ['all', 'All games'], ['any', 'On a console'], ['none', 'On no console'],
      ...S.devices.map((d) => ['dev:' + d.name, 'On ' + d.name]), ['ra', 'With achievements'],
    ];
    const showSel = h('select', {
      'aria-label': 'Show', dataset: { fk: 'show' },
      onchange: (e) => { vs.show = e.target.value; vs.limit = 120; paintGrid(); },
    });
    const paintShowOptions = () => {
      const opts = showOptions();
      if (!opts.some(([v]) => v === vs.show)) vs.show = 'all';
      showSel.replaceChildren(...opts.map(([v, t]) => h('option', { value: v, selected: v === vs.show || null }, t)));
    };
    const sortSel = h('select', {
      'aria-label': 'Sort', dataset: { fk: 'sort' },
      onchange: (e) => { vs.sort = e.target.value; vs.limit = 120; paintGrid(); },
    }, [['name', 'A to Z'], ['added', 'Recently added'], ['size', 'Largest']].map(([v, t]) => h('option', { value: v, selected: v === vs.sort || null }, t)));

    const root = h('div', { class: 'page sysview', vars: ui.systemTint(name) },
      h('nav', { class: 'crumbs', 'aria-label': 'Breadcrumb' }, h('a', { href: '#/', dataset: { fk: 'crumb-home' } }, 'Library'), h('span', { class: 'sep' }), h('span', { 'aria-current': 'page' }, full)),
      h('header', { class: 'syshead' },
        h('div', { class: 'sh-t' }, h('span', { class: 'code', title: full }, systemCode(name)), h('h1', null, full), stats),
        h('div', { class: 'sh-l' }, levers, leverAll)),
      unclaimedHost,
      h('div', { class: 'filters', role: 'group', 'aria-label': 'Filter the games' },
        h('label', { class: 'fld grow' }, h('span', { class: 'fi', 'aria-hidden': 'true' }, '>'), input),
        h('label', { class: 'fld' }, h('span', { class: 'fi' }, 'show'), showSel),
        h('label', { class: 'fld' }, h('span', { class: 'fi' }, 'sort'), sortSel),
        result),
      gridHost);

    function paintHeader() {
      const e = store.system(name) || entry;
      const list = S.lists.get(name);
      const ra = list ? list.games.filter((g) => g.ra === 'supported').length : null;
      stats.replaceChildren(h('b', null, e.games.toLocaleString('en-US')), ' games', h('span', { class: 'sep' }), h('b', null, formatBytes(e.size)),
        ...(ra ? [h('span', { class: 'sep' }), h('b', null, ra.toLocaleString('en-US')), ' with RetroAchievements'] : []));
    }

    function paintLevers() {
      levers.replaceChildren(...S.devices.map((d) => ui.lever(name, d.name)));
      leverAll.replaceChildren(...(S.devices.length > 1 ? [ui.lever(name, '*')] : []));
      ui.paintProgress();
    }

    function visibleGames() {
      const list = S.lists.get(name);
      if (!list) return null;
      const words = normalizeWords(vs.q).split(' ').filter(Boolean);
      const states = (g) => Object.values(g.devices);
      let games = list.games.filter((g) => {
        if (words.length) {
          const hay = normalizeWords((g.name || '') + ' ' + (g.title || ''));
          if (!words.every((w) => hay.includes(w))) return false;
        }
        const any = states(g).some((v) => v === 'on' || v === 'partial');
        if (vs.show === 'any') return any;
        if (vs.show === 'none') return !any;
        if (vs.show === 'ra') return g.ra === 'supported';
        if (vs.show.startsWith('dev:')) return g.devices[vs.show.slice(4)] === 'on';
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
      if (!games) { gridHost.replaceChildren(h('div', { class: 'grid' }, ui.skeletonTiles(12))); return; }
      const total = S.lists.get(name).games.length;
      result.textContent = games.length === total ? plural(total, 'game') : `${games.length.toLocaleString('en-US')} of ${total.toLocaleString('en-US')}`;
      if (!games.length) {
        gridHost.replaceChildren(emptyPanel(total ? 'No game found' : 'Empty system', total ? 'Try another word or clear the filters.' : 'This system has no games.'));
        return;
      }
      const grid = h('div', { class: 'grid' });
      let shown = 0;
      const sentinel = h('div', { class: 'sentinel', 'aria-hidden': 'true' });
      const more = () => {
        const upto = Math.min(games.length, vs.limit);
        for (; shown < upto; shown++) grid.append(ui.tile(games[shown], name, { inherit: true }));
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

    // Switch updates and DLC that no base game claims are in the library but belong to no tile.
    function paintUnclaimed() {
      const folders = S.unclaimed.get(name) || [];
      unclaimedHost.replaceChildren(...(folders.length ? [h('details', { class: 'parts unclaimed' },
        h('summary', null, `${plural(folders.length, 'update or DLC folder')} belong to no game in the library`),
        h('p', { class: 'mute small' }, 'Their base game is not on the hub, so they have no tile here and cannot be turned on or off from the UI.'),
        h('ul', null, folders.map((f) => h('li', null, h('code', null, f)))))] : []));
    }

    setMain(root, full);
    paintShowOptions();
    paintHeader();
    paintLevers();
    paintGrid();
    store.ensureList(name).then(() => {
      if (view.dead) return;
      paintHeader(); paintGrid(); paintUnclaimed(); paintLevers(); settled();
    }, (err) => {
      if (view.dead) return;
      store.handleError(err);
      gridHost.replaceChildren(emptyPanel('Could not load', 'The games of this system could not be loaded.'));
    });

    return Object.assign(view, {
      refresh() {
        if (!store.system(name)) { location.hash = '#/'; return; }
        const fk = captureFocus();
        paintShowOptions(); paintHeader(); paintLevers();
        if (vs.show !== 'all') paintGrid(); else ui.paintSlots(gridHost);
        restoreFocus(fk);
      },
      listsChanged() { paintLevers(); },
      dispose() { if (observer) observer.disconnect(); },
    });
  }

  // ===== the consoles ======================================================================

  function viewConsoles() {
    const view = { kind: 'consoles', dead: false };
    const host = h('div', { class: 'devs' });
    const root = h('div', { class: 'page' },
      h('div', { class: 'pagehead' }, h('h1', null, 'Consoles'), h('span', { class: 'sub' }, 'Every system, every console, one lever')), host);

    function panel(d) {
      const hold = store.holdings(d.name);
      const wl = d.whitelist ? d.whitelist.state : '';
      const excluded = d.exclude_systems;
      return h('section', { class: 'dev panel', dataset: { pdev: d.name, pk: 'host' }, 'aria-label': d.name },
        h('h2', null, h('i'), h('span', { class: 'dn', title: d.name }, d.name), ui.pnode('span', d.name, 'state', 'dstate', true)),
        h('div', { class: 'dev-top' },
          ui.segs(d.name, 24),
          h('span', { class: 'sc-f' },
            hold.complete
              ? [h('b', null, hold.games.toLocaleString('en-US')), ` ${hold.games === 1 ? 'game' : 'games'} selected`, h('span', { class: 'sep' }), h('b', null, formatBytes(hold.size))]
              : 'Counting games...',
            ui.pnode('span', d.name, 'left', 'left')),
          ui.diskMeter(d),
          wl === 'missing' ? h('p', { class: 'note warn' }, 'This console has no whitelist yet. The first change made here creates it.') : null,
          wl === 'needs-migration' ? h('p', { class: 'note warn' }, `Its whitelist is not in the format the UI manages. On the hub run "warpzone whitelist migrate ${d.name}", then reload.`) : null,
          excluded.length ? h('p', { class: 'mute small' }, 'Does not run: ', excluded.map(systemName).join(', ')) : null,
          h('button', {
            type: 'button', class: 'btn sm', dataset: { fk: 'hist:' + d.name },
            title: 'The earlier versions of this whitelist, with a restore',
            onclick: () => store.hooks.openHistory(d.name),
          }, 'History')),
        h('div', { class: 'dev-levers' }, S.systems.map((s) => ui.lever(s.system, d.name, { compact: true }))));
    }

    function paint() {
      const fk = captureFocus();
      host.replaceChildren(...(S.devices.length ? S.devices.map(panel) : [emptyPanel('No consoles', 'No device in the hub config lists the library folder.')]));
      ui.paintProgress();
      restoreFocus(fk);
    }

    setMain(root, 'Consoles');
    paint();
    settled();
    return Object.assign(view, { refresh: paint, listsChanged: paint });
  }

  // ===== the unattended inbox ==============================================================

  const WHY = {
    settling: 'still being copied or changed',
    space: "waiting for free space on the library's disk",
  };

  const runCounts = (c) => [
    c.placed ? plural(c.placed, 'game') + ' placed' : null,
    c.already ? `${c.already.toLocaleString('en-US')} already in the library` : null,
    c.review ? `${c.review.toLocaleString('en-US')} for review` : null,
    c.held ? `${c.held.toLocaleString('en-US')} held` : null,
  ].filter(Boolean).join(', ') || 'nothing placed';

  function ixGame(g) {
    const name = g.id
      ? h('a', { class: 'ix-name', href: ui.gameHref(g.system, g.id), dataset: { fk: 'ig:' + g.id }, title: 'Open the game, where the console switches are' }, g.entry)
      : h('span', { class: 'ix-name' }, g.entry);
    const extra = [...(g.ra || []), ...(g.notes || [])];
    return h('li', { class: 'ix-game', vars: ui.systemTint(g.system) },
      h('span', { class: 'sysb', title: systemName(g.system) }, systemCode(g.system)),
      name,
      g.id ? null : h('span', { class: 'mute small' }, 'not in the catalog yet'),
      extra.length ? h('small', { class: 'ix-extra mute' }, extra.join(' / ')) : null);
  }

  function ixItem(item, note) {
    return h('li', { class: 'ix-item' },
      h('b', { class: 'ix-name' }, item.item),
      (item.reasons || []).map((r) => h('p', { class: 'ix-reason' }, r)),
      (item.notes || []).map((r) => h('p', { class: 'mute small' }, r)),
      note ? h('p', { class: 'mute small' }, note) : null);
  }

  // One run record: what it placed (linked to the game pages), the whitelist lines, what went to
  // review or stayed held, and the notes. Used for the newest run and for each older one opened.
  function runBody(rec) {
    const items = rec.items || [];
    const games = (outcomes) => items.flatMap((i) => (i.games || []).filter((g) => outcomes.includes(g.outcome)));
    const placed = games(['placed', 'planned']);
    const already = games(['already', 'exists']);
    const review = items.filter((i) => i.outcome === 'review');
    const held = items.filter((i) => i.outcome === 'held');
    const again = items.filter((i) => i.outcome === 'waiting');
    const gone = items.filter((i) => i.outcome === 'gone');
    const n = (list) => list.length.toLocaleString('en-US');
    const part = (title, ...body) => h('section', { class: 'ix-part' }, h('h3', null, title), ...body);
    const wl = rec.whitelist || [];
    const scrape = rec.scrape;
    return h('div', { class: 'ix-run' },
      placed.length ? part(`Placed (${n(placed)})`, h('ul', { class: 'ix-list' }, placed.map(ixGame))) : null,
      already.length ? part(`Already in the library (${n(already)})`,
        h('p', { class: 'mute small' }, already.map((g) => g.entry).join(', '))) : null,
      wl.length ? part('Whitelist lines', h('pre', { class: 'diff ix-pre', tabindex: '0', 'aria-label': 'Whitelist lines of this run' }, wl.join('\n'))) : null,
      review.length ? part(`For review (${n(review)})`,
        h('ul', { class: 'ix-list' }, review.map((i) => ixItem(i, i.moved_to ? `The original is in ${i.moved_to}.` : null)))) : null,
      held.length ? part(`Held (${n(held)})`,
        h('ul', { class: 'ix-list' }, held.map((i) => ixItem(i, 'The original is still in the inbox.')))) : null,
      again.length ? part(`Waiting again (${n(again)})`,
        h('ul', { class: 'ix-list' }, again.map((i) => ixItem(i, 'The original is still in the inbox and is looked at again.')))) : null,
      gone.length ? part(`Gone (${n(gone)})`,
        h('ul', { class: 'ix-list' }, gone.map((i) => ixItem(i, 'The original was no longer in the inbox when it was filed away.')))) : null,
      scrape ? part('Scraping', h('p', { class: 'mute small' },
        scrape.state === 'done' ? `Scraped: ${(scrape.systems || []).map(systemName).join(', ') || 'nothing to do'}.`
          : scrape.state === 'skipped' ? 'Skipped.' : 'Not finished.',
        scrape.message ? ' ' + scrape.message : '',
        scrape.state === 'not-finished' && (scrape.systems || []).length ? ` Run "warpzone scrape --system" for: ${scrape.systems.join(', ')}.` : '')) : null,
      (rec.warnings || []).length ? part('Warnings', h('ul', { class: 'ix-list' }, rec.warnings.map((w) => h('li', { class: 'ix-item mute small' }, w)))) : null,
      !items.length ? h('p', { class: 'mute small' }, 'This run processed nothing.') : null);
  }

  function viewInbox() {
    const view = { kind: 'inbox', dead: false };
    const host = h('div', { class: 'ix' });
    const root = h('div', { class: 'page' },
      h('div', { class: 'pagehead' }, h('h1', null, 'Inbox'), h('span', { class: 'sub' }, 'Games the hub filed by itself')), host);
    const details = new Map(); // run id -> {state: 'loading' | 'ok' | 'gone' | 'error', data}
    const opened = new Set();

    function statusPanel(d) {
      const err = d.error;
      return h('section', { class: 'panel ix-status', 'aria-label': 'State of the inbox' },
        h('h2', null, h('i'), 'State'),
        h('div', { class: 'ix-body' },
          h('p', null, d.checked
            ? ['Last check ', h('b', { title: formatMoment(d.checked) }, timeAgo(d.checked)), '.']
            : 'No unattended run has recorded a check yet.'),
          d.busy ? h('p', null, `A run (${d.busy.mode}) has been working since `,
            h('b', { title: formatMoment(d.busy.since) }, timeAgo(d.busy.since)), '.') : null,
          err ? h('div', { class: 'note warn', role: 'alert' },
            h('b', null, err.message),
            h('p', { class: 'mute small' }, `Since ${formatMoment(err.since)}; ${plural(err.count, 'run')} met it.` +
              (err.config ? ' It needs a change to the configuration.' : ''))) : null));
    }

    function waitingPanel(d) {
      const held = d.held || [];
      const waiting = d.waiting || [];
      if (!held.length && !waiting.length) return null;
      return h('section', { class: 'panel ix-wait', 'aria-label': 'Held and waiting items' },
        h('h2', null, h('i'), 'In the inbox', h('span', null, `${held.length} held, ${waiting.length} waiting`)),
        h('ul', { class: 'ix-list ix-body' },
          held.map((x) => h('li', { class: 'ix-item' },
            h('b', { class: 'ix-name' }, x.item), h('span', { class: 'ix-tag held' }, 'held'),
            h('p', { class: 'ix-reason' }, x.reason),
            h('p', { class: 'mute small', title: formatMoment(x.since) }, `Held ${timeAgo(x.since)}. Look at it, then run "warpzone inbox --apply" by hand, or replace the item to try again.`))),
          waiting.map((x) => h('li', { class: 'ix-item' },
            h('b', { class: 'ix-name' }, x.item), h('span', { class: 'ix-tag' }, 'waiting'),
            h('p', { class: 'mute small', title: formatMoment(x.since) },
              `${formatBytes(x.bytes) || '0 B'}, here since ${timeAgo(x.since)}: ${WHY[x.why] || x.why}.`)))));
    }

    function newestPanel(rec) {
      return h('section', { class: 'panel ix-last', 'aria-label': 'The newest run' },
        h('h2', null, h('i'), 'Newest run', h('span', { title: formatMoment(rec.finished) }, `${timeAgo(rec.finished)} / ${rec.mode}`)),
        h('div', { class: 'ix-body' }, h('p', { class: 'ix-counts' }, runCounts(rec.counts || {})), runBody(rec)));
    }

    function olderPanel(rows) {
      if (!rows.length) return null;
      return h('section', { class: 'panel ix-older', 'aria-label': 'Older runs' },
        h('h2', null, h('i'), 'Older runs'),
        h('ul', { class: 'ix-runs ix-body' }, rows.map((r) => {
          const on = opened.has(r.id);
          const d = details.get(r.id);
          return h('li', { class: 'ix-runrow' },
            h('button', {
              type: 'button', class: 'hist-row' + (on ? ' on' : ''), 'aria-expanded': String(on), dataset: { fk: 'run:' + r.id },
              onclick: () => toggle(r.id),
            }, h('b', { title: formatMoment(r.finished) }, formatMoment(r.finished) || r.id), h('small', null, `${timeAgo(r.finished)} / ${r.mode}`),
            h('span', null, runCounts(r))),
            on ? (d && d.state === 'ok' ? runBody(d.data)
              : d && d.state === 'gone' ? h('p', { class: 'mute small' }, 'This record is gone: the hub keeps the newest 50.')
                : d && d.state === 'error' ? h('p', { class: 'error-text', role: 'alert' }, 'It could not be loaded.')
                  : h('p', { class: 'mute small' }, 'Loading...')) : null);
        })));
    }

    async function toggle(id) {
      if (opened.has(id)) { opened.delete(id); paint(); return; }
      opened.add(id);
      const held = details.get(id);
      if (held && held.state === 'ok') { paint(); return; }
      details.set(id, { state: 'loading' });
      paint();
      try {
        details.set(id, { state: 'ok', data: await api.inboxRun(id) });
      } catch (err) {
        if (view.dead) return;
        details.set(id, { state: err.code === 'not-found' ? 'gone' : 'error' });
      }
      if (!view.dead) paint();
    }

    function paint() {
      const fk = captureFocus();
      const d = S.inbox;
      if (!d) {
        host.replaceChildren(S.inboxError
          ? emptyPanel('The inbox is not available', 'The server could not read it.',
            h('button', { class: 'btn', type: 'button', onclick: () => store.loadInbox() }, 'Try again'))
          : h('p', { class: 'loading-note' }, 'Loading...'));
        return;
      }
      const runs = d.runs || [];
      host.replaceChildren(...[
        statusPanel(d),
        waitingPanel(d),
        d.last ? newestPanel(d.last) : emptyPanel('No run has filed anything yet', 'Each run of "warpzone inbox --apply" that processed an item leaves a record here.'),
        olderPanel(runs.slice(1)),
      ].filter(Boolean));
      restoreFocus(fk);
    }

    setMain(root, 'Inbox');
    paint();
    settled();
    store.loadInbox();
    return Object.assign(view, {
      refresh() {},
      inboxChanged() { paint(); if (store.inboxNews()) store.markInboxSeen(); },
    });
  }

  // ===== search ============================================================================

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

  function groupCard(group) {
    const known = (g) => S.byId.get(g.id);
    const lead = group.games.find((g) => (known(g) ? known(g).game.cover : g.cover !== false)) || group.games[0];
    // Ask for the image only when the game list says there is one (the search answer has no
    // `cover`): a request for a missing image is a 404 in the log for nothing.
    const hasCover = known(lead) ? !!known(lead).game.cover : lead.cover !== false && !!lead.cover;
    const t0 = parseTitle(group.title);
    const systems = new Set(group.games.map((g) => g.system));
    const rows = group.games.map((g) => {
      const t = parseTitle(g.title);
      const flags = known(g) ? known(g).game.flags || [] : [];
      const ref = { id: g.id, system: g.system, title: t.name, locked: flags.includes('unlistable') };
      const note = [t.region, ...t.tags].filter(Boolean).join(' / ') || systemName(g.system);
      return h('div', { class: 'vrow', vars: ui.systemTint(g.system) },
        h('a', { href: ui.gameHref(g.system, g.id), dataset: { fk: 'g:' + g.id }, title: t.full },
          h('span', { class: 'sysb', title: systemName(g.system) }, systemCode(g.system)),
          h('span', { class: 'vt' }, note), ui.queueMark(g.id)),
        ui.slots(ref));
    });
    return h('article', { class: 'group', vars: ui.systemTint(lead.system) },
      h('a', { class: 'gart', href: ui.gameHref(lead.system, lead.id), tabindex: '-1', 'aria-hidden': 'true' },
        ui.artBox({ id: lead.id, system: lead.system, has: hasCover })),
      h('div', { class: 'gbody' },
        h('div', { class: 'gh' },
          h('a', { class: 'gt', href: ui.gameHref(lead.system, lead.id), title: group.title }, t0.name),
          group.games.length > 1 ? h('span', { class: 'gcount' }, systems.size > 1 ? plural(systems.size, 'system') : plural(group.games.length, 'version')) : null),
        h('div', { class: 'versions' }, rows)));
  }

  let searchSystem = null;

  function viewSearch(getQuery) {
    const view = { kind: 'search', dead: false };
    const head = h('div', { class: 'shead', role: 'status' });
    const chips = h('div', { class: 'schips', role: 'group', 'aria-label': 'Filter by system' });
    const hits = h('div', { class: 'schips' });
    const results = h('div', { class: 'group-grid' });
    const root = h('div', { class: 'page search-page' }, head, hits, chips, results);

    function paint() {
      const q = getQuery();
      const api_ = S.searchResult && S.searchResult.q === q ? S.searchResult : null;
      let groups = q ? (api_ ? api_.groups : localSearch(q)) : [];
      const counts = new Map();
      for (const gr of groups) for (const g of gr.games) counts.set(g.system, (counts.get(g.system) || 0) + 1);
      if (searchSystem && !counts.has(searchSystem)) searchSystem = null;
      const versions = Array.from(counts.values()).reduce((a, b) => a + b, 0);
      if (searchSystem) groups = groups.map((gr) => ({ ...gr, games: gr.games.filter((g) => g.system === searchSystem) })).filter((gr) => gr.games.length);
      const pending = q && !api_ && S.lists.size < S.systems.length;

      head.replaceChildren(h('h1', null, 'Search ', q ? h('em', null, q) : null),
        h('span', { class: 'res' }, !q ? 'Type to search' : groups.length ? `${plural(groups.length, 'title')} · ${plural(groups.reduce((n, gr) => n + gr.games.length, 0), 'version')}`
          : pending ? 'Searching...' : 'Nothing found'));

      const sysHits = q.length > 1 ? S.systems.filter((s) => normalizeWords(systemName(s.system)).includes(normalizeWords(q))) : [];
      hits.replaceChildren(...sysHits.map((s) => h('a', { class: 'btn sm', href: ui.systemHref(s.system), vars: ui.systemTint(s.system) }, `Open ${systemName(s.system)}`)));

      chips.hidden = counts.size < 2;
      chips.replaceChildren(...(counts.size < 2 ? [] : [
        h('button', { type: 'button', class: 'tog', 'aria-pressed': String(!searchSystem), dataset: { fk: 'ss-all' }, onclick: () => { searchSystem = null; paint(); restoreFocus('ss-all'); } },
          h('span', { class: 'tick' }), `All ${versions}`),
        ...S.systems.filter((s) => counts.has(s.system)).map((s) => h('button', {
          type: 'button', class: 'tog', vars: ui.systemTint(s.system), title: systemName(s.system), 'aria-pressed': String(searchSystem === s.system), dataset: { fk: 'ss-' + s.system },
          onclick: () => { searchSystem = searchSystem === s.system ? null : s.system; paint(); restoreFocus('ss-' + s.system); },
        }, h('span', { class: 'tick' }), `${systemCode(s.system)} ${counts.get(s.system)}`)),
      ]));

      results.replaceChildren(...(groups.length ? groups.map(groupCard)
        : [emptyPanel(q ? (pending ? 'Searching' : 'No match') : 'Search the library',
          q ? (pending ? 'The library is still loading.' : 'Check the spelling or try a shorter word.') : 'Titles from every system, one group per game.')]));
    }

    setMain(root, 'Search');
    paint();
    return Object.assign(view, {
      refresh() { const fk = captureFocus(); paint(); restoreFocus(fk); },
      slotsChanged() { ui.paintSlots(results); },
      listsChanged() { const q = getQuery(); if (!S.searchResult || S.searchResult.q !== q) view.refresh(); },
    });
  }

  // ===== one game ==========================================================================

  const KIND_LABEL = {
    videos: 'Video', covers: 'Cover', miximages: 'Mix image', screenshots: 'Screenshot', titlescreens: 'Title screen',
    backcovers: 'Back cover', marquees: 'Marquee', '3dboxes': '3D box',
  };
  // The images and the video of the switcher, in the order of its buttons; the first one of
  // FIRST_KINDS that the game has is the one shown to begin with (the video, else a picture of
  // the game itself rather than its box).
  const GALLERY = ['videos', 'covers', 'miximages', 'screenshots', 'titlescreens', 'backcovers', 'marquees'];
  const FIRST_KINDS = ['videos', 'miximages', 'screenshots', 'covers', 'titlescreens', 'backcovers', 'marquees'];
  const POSTER_KINDS = ['screenshots', 'miximages', 'titlescreens', 'covers'];
  const DISC_SYSTEMS = new Set(['psx', 'ps2', 'ps3', 'ps4', 'psp', 'gc', 'wii', 'wiiu', 'dreamcast', 'saturn', 'segacd',
    'xbox', 'xbox360', '3do', 'cdi', 'pcenginecd', 'neogeocd', 'amigacd32', 'megacd']);
  const CART_SYSTEMS = new Set(['nes', 'famicom', 'snes', 'sfc', 'n64', 'gb', 'gbc', 'gba', 'nds', 'n3ds', '3ds', 'switch',
    'genesis', 'megadrive', 'mastersystem', 'gamegear', 'sega32x', 'atari2600', 'atari7800', 'lynx', 'ngp', 'pcengine',
    'tg16', 'psvita']);

  function mediaCard(d, kind, label, note, cls, system) {
    return h('figure', { class: 'mcard ' + cls },
      h('figcaption', null, h('b', null, label), h('span', null, note)),
      h('span', { class: 'mc-img' }, ui.artBox({ id: d.id, system, kind, has: true, alt: `${label} of ${d.name}` })),
      h('span', { class: 'floor', 'aria-hidden': 'true' }));
  }

  // The video of a game in the screen of the cabinet: muted, with controls, nothing downloaded
  // until it is played (the poster is a picture of the game when the scrape has one). A video
  // the browser cannot play calls `failed`, and the page shows a picture instead.
  function videoScreen(d, media, title, failed) {
    const posterKind = POSTER_KINDS.find((k) => media.includes(k));
    const video = h('video', {
      class: 'vid', controls: true, playsinline: true, preload: posterKind ? 'none' : 'metadata', loop: true,
      poster: posterKind ? api.mediaUrl(d.id, posterKind) : null, 'aria-label': `Video of ${title}`,
    });
    video.muted = true;
    video.addEventListener('error', failed);
    video.src = api.mediaUrl(d.id, 'videos');
    return h('div', { class: 'screen is-video' }, video);
  }

  // The label of an award and the mode that earned it.
  const AWARDS = {
    'beaten-softcore': ['Beaten', 'softcore'], 'beaten-hardcore': ['Beaten', 'hardcore'],
    completed: ['Completed', 'softcore mastery'], mastered: ['Mastered', 'hardcore'],
  };

  // How rare an achievement is, from the share of the players who have it.
  function rarityLabel(percent) {
    return percent < 2 ? 'Ultra rare' : percent < 8 ? 'Rare' : percent < 25 ? 'Uncommon' : 'Common';
  }

  // One achievement of the wall. `state` is `unlocked`, `locked`, or `plain` when it is not known
  // what the user has (no user, or the unlock dates are not downloaded yet). The badge image is
  // the one the hub cached; without it (a 404) the tile keeps its plain glyph.
  function achievementTile(a, state, players) {
    const badge = h('span', { class: 'ab' }, icon('trophy'));
    if (a.badge) {
      const img = h('img', { src: api.badgeUrl(a.badge, state === 'locked' ? 'locked' : 'unlocked'), alt: '', loading: 'lazy', decoding: 'async' });
      img.addEventListener('error', () => img.remove());
      badge.append(img);
    }
    const when = a.unlocked ? a.unlocked.softcore || a.unlocked.hardcore : null;
    const hard = !!(a.unlocked && a.unlocked.hardcore);
    const rarity = a.rarity && players > 0 && typeof a.rarity.percent === 'number' ? a.rarity : null;
    const status = state === 'unlocked' ? `Unlocked${hard ? ' in hardcore' : ''}${when ? ' ' + formatDay(when) : ''}` : state === 'locked' ? 'Locked' : '';
    return h('li', { class: 'ach ' + state + (hard ? ' hc' : ''), title: a.description || null },
      badge,
      h('div', { class: 'ai' },
        h('b', { class: 'at' }, a.title || 'Achievement'),
        a.description ? h('span', { class: 'ad' }, a.description) : null,
        h('span', { class: 'am' },
          Number.isFinite(a.points) ? h('span', { class: 'pts' }, h('b', null, String(a.points)), ' pts') : null,
          rarity ? h('span', { class: 'rar', title: `${rarity.percent}% of the players unlocked it; ${rarity.hardcore_percent}% of the hardcore players did in hardcore` },
            `${rarity.percent}%`, h('i', null, ' ' + rarityLabel(rarity.percent))) : null,
          hard ? h('span', { class: 'hcm', title: 'Unlocked in hardcore mode' }, 'HC') : null,
          a.type ? h('span', { class: 'aty' }, a.type.replace('_', ' ')) : null),
        status ? h('span', { class: 'astat', title: when ? formatMoment(when) : null }, status) : null));
  }

  // The achievements of a game: the progress bar and award, and the wall of badges. It says
  // what is not known (no list downloaded, no user, no unlock dates) instead of guessing. The
  // filter and how many are shown live in `st`, which the page keeps between repaints.
  function achievementsPanel(d, st, repaint) {
    const ach = d.achievements;
    if (!ach || typeof ach !== 'object') return null;
    const tot = ach.totals || {};
    const list = Array.isArray(ach.list) ? ach.list : [];
    const progress = ach.progress && typeof ach.progress === 'object' ? ach.progress : null;
    const dates = !!(ach.available && progress && progress.dates);
    const count = ach.available ? list.length : Number(tot.count) || 0;
    const unlocked = progress && typeof tot.unlocked === 'number' ? tot.unlocked : null;
    const hardcore = progress && typeof tot.unlocked_hardcore === 'number' ? tot.unlocked_hardcore : null;
    const award = progress && progress.award && AWARDS[progress.award.kind] ? progress.award : null;

    const head = [];
    if (unlocked != null && count) {
      head.push(h('div', { class: 'ach-sum' },
        h('span', null, h('b', null, fmt(unlocked)), ` / ${fmt(count)} unlocked`),
        typeof tot.points_unlocked === 'number' ? h('span', null, h('b', null, fmt(tot.points_unlocked)), ` / ${fmt(tot.points)} points`) : null,
        hardcore ? h('span', null, h('b', null, fmt(hardcore)), ' in hardcore') : null,
        progress.profile ? h('a', { class: 'ach-user', href: progress.profile, target: '_blank', rel: 'noopener noreferrer' }, progress.user) : null),
      h('div', { class: 'ach-bar', 'aria-hidden': 'true' }, h('i', { vars: { w: String(Math.min(1, unlocked / count)) } })));
    } else if (count) {
      head.push(h('div', { class: 'ach-sum' },
        h('span', null, h('b', null, fmt(count)), count === 1 ? ' achievement' : ' achievements'),
        Number(tot.points) > 0 ? h('span', null, h('b', null, fmt(tot.points)), ' points') : null));
    }
    if (award) {
      const [name, how] = AWARDS[progress.award.kind];
      head.push(h('div', { class: 'ach-award ' + progress.award.kind },
        icon('trophy'), h('span', null, h('b', null, name), h('small', null, [how, formatDay(award.date)].filter(Boolean).join(' · ')))));
    }

    const notes = [];
    if (!ach.available) {
      notes.push('The hub has not downloaded the achievements of this game yet. They arrive with its next RetroAchievements run.');
    } else if (!progress) {
      notes.push('No RetroAchievements user is set on the hub (the [ra] user setting), so what you unlocked is not shown.');
    } else if (!dates) {
      notes.push('The dates of your unlocks are not downloaded yet, so the badges below are not marked. The counts come from your RetroAchievements summary.');
    }

    let wall = null;
    if (list.length) {
      const state = (a) => (dates ? (a.unlocked ? 'unlocked' : 'locked') : 'plain');
      const nUnlocked = dates ? list.filter((a) => a.unlocked).length : 0;
      const filter = dates && st.filter !== 'all' ? st.filter : 'all';
      const shown = list.filter((a) => filter === 'all' || state(a) === filter);
      const visible = shown.slice(0, st.limit);
      const tab = (key, label, n) => h('button', {
        type: 'button', class: 'tog', 'aria-pressed': String(filter === key), dataset: { fk: 'af:' + key },
        onclick: () => { st.filter = key; st.limit = 48; repaint('af:' + key); },
      }, h('span', { class: 'tick' }), `${label} ${n}`);
      wall = [
        dates ? h('div', { class: 'ach-tabs seg', role: 'group', 'aria-label': 'Show achievements' },
          tab('all', 'All', list.length), tab('unlocked', 'Unlocked', nUnlocked), tab('locked', 'Locked', list.length - nUnlocked)) : null,
        h('ul', { class: 'ach-wall' }, visible.map((a) => achievementTile(a, state(a), ach.players))),
        shown.length > visible.length
          ? h('button', {
            type: 'button', class: 'btn sm ach-more', dataset: { fk: 'ach-more' },
            onclick: () => { st.limit = shown.length; repaint('ach-more'); },
          }, `Show all ${fmt(shown.length)}`)
          : null,
      ];
    }
    return h('section', { class: 'panel ach-panel', 'aria-label': 'Achievements' },
      h('h2', null, h('i'), 'Achievements', h('span', null, ach.available && count ? `${fmt(count)} to win` : '')),
      h('div', { class: 'pbody' },
        head,
        ...notes.map((n) => h('p', { class: 'mute small ach-note' }, n)),
        d.ra && d.ra.url ? h('a', { class: 'ach-link', href: d.ra.url, target: '_blank', rel: 'noopener noreferrer' }, 'Game page on RetroAchievements') : null,
        wall));
  }

  // The play statistics of a game: what the totals say, then each console.
  function playPanel(d) {
    const play = d.play;
    const tot = play && play.totals;
    if (!tot) return null;
    const devs = Object.entries(play.devices || {}).sort((a, b) => (b[1].last || 0) - (a[1].last || 0) || a[0].localeCompare(b[0]));
    const plays = Number(tot.plays) > 0 ? Math.floor(Number(tot.plays)) : 0;
    const time = formatPlaytime(tot.playtime);
    if (!plays && !devs.length && !time) return null;
    const stat = (label, value, title) => (value ? h('div', { class: 'ps', title: title || null }, h('dt', null, label), h('dd', null, value)) : null);
    return h('section', { class: 'panel playp', 'aria-label': 'Play history' },
      h('h2', null, h('i'), 'Play history', h('span', null, tot.fav ? 'Favourite' : '')),
      h('dl', { class: 'pstats pbody' },
        stat('Played', plays ? plural(plays, 'time') : ''),
        stat('Last played', timeAgo(tot.last), formatMoment(tot.last)),
        stat('Playtime', time)),
      devs.length ? h('ul', { class: 'pdev pbody' }, devs.map(([name, x]) => {
        const when = timeAgo(x.last);
        const t = formatPlaytime(x.playtime);
        return h('li', null,
          h('span', { class: 'pf' + (x.fav === true ? ' on' : ''), title: x.fav === true ? 'Favourite on ' + name : null },
            x.fav === true ? [icon('star'), h('span', { class: 'sr-only' }, 'Favourite')] : null),
          h('b', { class: 'pn', title: name }, name),
          h('span', { class: 'pp' }, Number(x.plays) > 0 ? plural(Math.floor(Number(x.plays)), 'play') : 'No plays'),
          h('span', { class: 'pw', title: formatMoment(x.last) || null }, [when ? 'Last ' + when : '', t].filter(Boolean).join(' · ')));
      })) : null);
  }

  // What the hub holds of the game's saves. Nothing when it found none: that says "not found",
  // not "the game has no save".
  // What one part of a game is, from where it lives: the base game, an update, a DLC, a disc
  // of a set, a playlist or cue sheet. A game of one part needs no label.
  function partRole(path) {
    const lower = path.toLowerCase();
    if (/^extras\/[^/]+\/updates\//.test(lower) || /-update\/?$/.test(lower)) return 'Update';
    if (/^extras\/[^/]+\/dlc\//.test(lower) || /^roms\/ps4\/dlc\//.test(lower)) return 'DLC';
    if (/^roms\/psvita\/patch\//.test(lower)) return 'Update';
    if (/^roms\/psvita\/license\//.test(lower)) return 'License';
    if (lower.endsWith('.m3u')) return 'Playlist';
    if (lower.endsWith('.cue') || lower.endsWith('.gdi')) return 'Index';
    const disc = /\((?:disc|disk|cd)\s*(\d+)/i.exec(path);
    if (disc) return 'Disc ' + disc[1];
    const track = /\(track\s*(\d+)\)/i.exec(path);
    if (track) return 'Track ' + track[1];
    return 'Game';
  }

  // The Files panel: one row per part, its folder small above its name, its size in a column of
  // its own, and the total in the heading.
  function filesPanel(parts) {
    if (!parts.length) return null;
    const many = parts.length > 1;
    const total = parts.reduce((n, p) => n + (p.size || 0), 0);
    const rows = parts.map((p) => {
      const role = many ? partRole(p.path) : '';
      const folderish = role === 'Update' || role === 'DLC';
      const clean = p.path.replace(/\/+$/, '');
      const cut = clean.lastIndexOf('/');
      const dir = cut >= 0 ? clean.slice(0, cut + 1) : '';
      const name = (cut >= 0 ? clean.slice(cut + 1) : clean) + (folderish ? '/' : '');
      return h('li', { class: 'part' + (many ? ' has-role' : '') },
        many ? h('span', { class: 'part-role' }, role) : null,
        h('span', { class: 'part-path', title: p.path },
          dir ? h('span', { class: 'part-dir' }, dir) : null,
          h('code', { class: 'part-name' }, name)),
        h('span', { class: 'part-size' }, formatBytes(p.size)));
    });
    const head = plural(parts.length, 'part') + (many ? ' · ' + formatBytes(total) : '');
    return h('section', { class: 'panel', 'aria-label': 'Files' }, h('h2', null, h('i'), 'Files', h('span', null, head)),
      h('ul', { class: 'parts-list pbody' }, rows));
  }

  function savesPanel(d) {
    const sv = d.saves;
    if (!sv || (!sv.data && !(sv.states > 0))) return null;
    const rows = [];
    const row = (k, v, title) => { if (v) rows.push(h('div', { class: 'fact', title: title || null }, h('dt', null, k), h('dd', null, v))); };
    row('Battery save', sv.data ? 'Yes' : 'None');
    row('Save states', sv.states > 0 ? String(sv.states) : 'None');
    row('Last save', timeAgo(sv.newest), formatMoment(sv.newest));
    row('Emulator', Array.isArray(sv.emulators) ? sv.emulators.join(', ') : '');
    return h('section', { class: 'panel', 'aria-label': 'Saves' },
      h('h2', null, h('i'), 'Saves', h('span', null, 'On the hub')),
      h('dl', { class: 'facts pbody' }, rows));
  }

  // The chips under the title: system, regions, languages, the verified seal, achievements.
  function gameBadges(d, sys, t, locked) {
    const regions = Array.isArray(d.region) ? d.region.filter(Boolean) : [];
    const langs = Array.isArray(d.languages) ? d.languages.filter(Boolean) : [];
    return h('div', { class: 'badges' },
      h('a', { class: 'badge', href: ui.systemHref(sys), title: systemName(sys) }, systemName(sys)),
      regions.length ? regions.map((r) => h('span', { class: 'badge reg', title: 'Region' }, r)) : ui.regionChip(t.region),
      langs.length ? h('span', { class: 'langs', role: 'group', 'aria-label': 'Languages' },
        langs.map((l) => h('span', { class: 'badge reg lang', title: languageName(l) }, l.toUpperCase()))) : null,
      d.verified === true ? h('span', { class: 'badge seal', title: 'Verified dump: matched to a DAT by its content when it was filed' + (d.dat ? ' (' + d.dat + ')' : '') },
        icon('check'), 'Verified') : null,
      d.play && d.play.totals && d.play.totals.fav === true ? h('span', { class: 'badge fav', title: 'Favourite' }, icon('star'), 'Favourite') : null,
      d.ra ? ui.raBadge(d.ra) : null,
      d.size ? h('span', { class: 'badge reg' }, formatBytes(d.size)) : null,
      locked ? h('span', { class: 'badge warn' }, 'Cannot be changed from the UI') : null);
  }

  function viewGame(system, id) {
    const view = { kind: 'game', dead: false, system, id };
    const root = h('div', { class: 'page detail', vars: ui.systemTint(system) });
    const frame = {
      crumbs: h('div', { class: 'crumbs-host' }),
      dleft: h('div', { class: 'dleft' }),
      dinfo: h('div', { class: 'dinfo' }),
      rest: h('div', { class: 'drest' }),
    };
    let built = false;
    let kindSel = null;
    let more = false;
    let videoFailed = false;
    let mediaKey = '';
    const wall = { filter: 'all', limit: 48 };

    // The media column is rebuilt only when what it shows changes, so a refresh of the page (a
    // console's progress, a catalog run) never stops a video that is playing.
    function paintMedia(d, sys, t, gallery, media) {
      const cards = [
        media.includes('physicalmedia') ? ['physicalmedia', 'phys'] : null,
        media.includes('3dboxes') ? ['3dboxes', 'box'] : null,
      ].filter(Boolean);
      const key = [d.id, kindSel, gallery.join(), cards.join()].join('|');
      if (key === mediaKey) return;
      mediaKey = key;
      const isVideo = kindSel === 'videos';
      const screen = isVideo
        ? videoScreen(d, media, t.name, () => { videoFailed = true; paint(false); })
        : h('div', { class: 'screen' },
          ui.artBox({ id: d.id, system: sys, kind: kindSel || 'covers', has: !!kindSel, alt: `${KIND_LABEL[kindSel] || 'Cover'} of ${t.name}`, eager: true, cls: kindSel === 'screenshots' ? 'shot' : '' }),
          h('span', { class: 'scan', 'aria-hidden': 'true' }));
      const cab = h('div', { class: 'cab' },
        h('div', { class: 'cab-h' }, h('span', null, kindSel ? KIND_LABEL[kindSel] : 'Art'), h('span', { class: 'ch' }, systemCode(sys))),
        screen,
        gallery.length > 1 ? h('div', { class: 'cab-f seg', role: 'group', 'aria-label': 'Images and video' }, gallery.map((k) => h('button', {
          type: 'button', class: 'tog', 'aria-pressed': String(k === kindSel), dataset: { fk: 'kind:' + k },
          onclick: () => { kindSel = k; paint(false); restoreFocus('kind:' + k); },
        }, h('span', { class: 'tick' }), KIND_LABEL[k]))) : null);
      const physLabel = DISC_SYSTEMS.has(sys) ? 'Disc' : CART_SYSTEMS.has(sys) ? 'Cartridge' : 'Physical media';
      const shelf = cards.map(([kind, cls]) => (kind === 'physicalmedia'
        ? mediaCard(d, kind, physLabel, systemName(sys), cls, sys) : mediaCard(d, kind, '3D box', 'Retail box', cls, sys)));
      frame.dleft.replaceChildren(cab, ...(shelf.length ? [h('section', { class: 'shelf', 'aria-label': 'Physical media and box' }, shelf)] : []));
    }

    function paint(first) {
      const d = S.detail && S.detail.id === id ? S.detail.data : null;
      if (!d) return;
      const sys = d.system || system;
      const meta = d.meta || {};
      const t = parseTitle(meta.name || d.name || d.title);
      const media = d.media || [];
      const gallery = GALLERY.filter((k) => media.includes(k) && !(k === 'videos' && videoFailed));
      if (!kindSel || !gallery.includes(kindSel)) kindSel = FIRST_KINDS.find((k) => gallery.includes(k)) || null;
      const fk = captureFocus();
      const locked = (d.flags || []).includes('unlistable');
      const ref = { id: d.id, system: sys, title: t.name, locked };
      if (!built) { built = true; root.replaceChildren(frame.crumbs, h('div', { class: 'dtop' }, frame.dleft, frame.dinfo), frame.rest); }

      paintMedia(d, sys, t, gallery, media);

      const rating = parseFloat(meta.rating);
      const facts = [];
      const fact = (k, v, cls) => { if (v) facts.push(h('div', { class: 'fact ' + (cls || '') }, h('dt', null, k), h('dd', null, v))); };
      fact('Developer', meta.developer);
      fact('Publisher', meta.publisher);
      fact('Year', meta.releasedate ? formatDate(meta.releasedate).slice(0, 4) : '', 'num');
      fact('Players', meta.players, 'num');
      fact('Genre', meta.genre, 'wide');
      if (!isNaN(rating)) {
        const r = Math.min(1, Math.max(0, rating));
        fact('Rating', h('span', { class: 'rating' },
          h('span', { class: 'bars', 'aria-hidden': 'true' }, Array.from({ length: 10 }, (_, i) => h('i', { class: i < Math.round(r * 10) ? 'on' : '' }))),
          h('b', null, `${(r * 10).toFixed(1)} / 10`)), 'wide');
      }
      fact('Size', formatBytes(d.size), 'num');
      fact('Added', d.added, 'num');
      if (d.dat) fact('DAT', d.dat, 'wide');

      const long = (meta.desc || '').length > 260;
      const desc = meta.desc
        ? [h('p', { class: 'lead' + (long && !more ? ' clamp' : '') }, meta.desc),
          long ? h('button', { type: 'button', class: 'more-btn', dataset: { fk: 'more' }, onclick: () => { more = !more; paint(false); restoreFocus('more'); } }, more ? 'Show less' : 'Read more') : null]
        : h('p', { class: 'lead mute' }, 'No description. This game has not been scraped.');

      const parts = d.parts || [];
      const others = d.versions || [];
      frame.crumbs.replaceChildren(h('nav', { class: 'crumbs', 'aria-label': 'Breadcrumb' },
        h('a', { href: '#/', dataset: { fk: 'crumb-home' } }, 'Library'), h('span', { class: 'sep' }),
        h('a', { href: ui.systemHref(sys), dataset: { fk: 'crumb-sys' }, title: systemName(sys) }, systemName(sys)), h('span', { class: 'sep' }),
        h('span', { 'aria-current': 'page' }, t.name)));
      frame.dinfo.replaceChildren(
        h('h1', null, t.name),
        ...(t.full !== t.name ? [h('p', { class: 'rom' }, t.full)] : []),
        gameBadges(d, sys, t, locked),
        consolesPanel(d, ref),
        W.queueUi.gamePanel(d, ref),
        ...[playPanel(d)].filter(Boolean));
      frame.rest.replaceChildren(
        h('div', { class: 'dgrid' },
          h('section', { class: 'panel brief', 'aria-label': 'Briefing' }, h('h2', null, h('i'), 'Briefing', h('span', null, systemCode(sys))), h('div', { class: 'pbody' }, desc)),
          facts.length ? h('section', { class: 'panel', 'aria-label': 'Game data' }, h('h2', null, h('i'), 'Game data'), h('dl', { class: 'facts pbody' }, facts)) : null,
          savesPanel(d),
          filesPanel(parts)),
        ...[achievementsPanel(d, wall, (focus) => { paint(false); restoreFocus(focus); })].filter(Boolean),
        ...(others.length ? [h('section', { class: 'panel', 'aria-label': 'Other versions' }, h('h2', null, h('i'), 'Other versions', h('span', null, String(others.length))),
          h('div', { class: 'vlist pbody' }, others.map((v) => {
            const known = S.byId.get(v.id);
            const vt = parseTitle(v.title);
            return h('a', { class: 'vitem', href: ui.gameHref(v.system, v.id), vars: ui.systemTint(v.system), dataset: { fk: 'v:' + v.id }, title: vt.full },
              h('span', { class: 'vart' }, ui.artBox({ id: v.id, system: v.system, has: known ? !!known.game.cover : undefined })),
              h('span', { class: 'vi' }, h('b', null, vt.name), h('span', null, [systemName(v.system), vt.region].filter(Boolean).join(' · '))));
          })))] : []));
      paintConsoleBar(d, ref);
      ui.paintProgress();
      document.title = `${t.name} - warpzone library`;
      restoreFocus(fk);
      if (first) { settled(); }
    }

    // What a console row offers for the state of the game on it.
    function actions(d, ref, dev, st) {
      const managed = store.isManaged(dev.name);
      const off = !managed || ref.locked;
      const label = (verb, prep) => `${verb} ${ref.title} ${prep} ${dev.name}`;
      const coin = (cls, ic, text, on, verb, prep) => h('button', {
        type: 'button', class: `coin ${cls}`, disabled: off || null, dataset: { fk: `${on ? 'add' : 'rm'}:${dev.name}`, write: '1' },
        'aria-label': label(verb, prep),
        onclick: () => store.changeGame({ id: d.id, title: ref.title }, ref.system, dev.name, on),
      }, icon(ic), text);
      if (st === 'off') return [coin('in', 'insert', 'Insert', true, 'Insert', 'into')];
      if (st === 'partial') return [coin('in', 'insert', 'Complete', true, 'Complete', 'on'), coin('out', 'eject', 'Eject', false, 'Eject', 'from')];
      if (st === 'on') return [coin('out', 'eject', 'Eject', false, 'Eject', 'from')];
      return [];
    }

    function consolesPanel(d, ref) {
      const dev = d.devices || {};
      const sys = ref.system;
      const rows = S.devices.map((c) => {
        const st = store.gameState(dev, c.name, sys);
        const managed = store.isManaged(c.name);
        const note = !managed ? 'Whitelist needs "warpzone whitelist migrate"'
          : st === 'unknown' ? 'The state of this game on the console is not known'
            : st === 'unsupported' ? 'This console does not take this system' : '';
        return h('div', { class: `crow st-${st}`, dataset: { pdev: c.name, pk: 'host' } },
          h('span', { class: 'dot' }),
          h('div', { class: 'cn' }, h('span', { class: 'cn-n', title: c.name }, c.name),
            note ? h('small', { class: 'note-s' }, note) : ui.pnode('small', c.name, 'state', 'cn-s', true)),
          h('span', { class: 'lamp', 'aria-hidden': 'true' }, st === 'on' ? 'LOADED' : st === 'partial' ? 'PARTIAL' : st === 'off' ? 'EMPTY' : '-'),
          h('div', { class: 'acts' }, actions(d, ref, c, st)));
      });
      const eligible = S.devices.filter((c) => !store.excludes(c.name, sys) && dev[c.name] !== 'unsupported');
      const canAdd = eligible.some((c) => dev[c.name] !== 'on');
      const canRemove = S.devices.some((c) => dev[c.name] === 'on' || dev[c.name] === 'partial');
      const blocked = store.allBlocked(sys);
      const on = S.devices.filter((c) => dev[c.name] === 'on').length;
      const bulk = (cls, text, add, enabled) => h('button', {
        type: 'button', class: `btn sm ${cls}`, disabled: !enabled || ref.locked || blocked || null, dataset: { fk: (add ? 'add' : 'rm') + ':*', write: '1' },
        onclick: () => store.changeGame({ id: d.id, title: ref.title }, sys, '*', add),
      }, text);
      return h('section', { class: 'panel cons', 'aria-label': 'Consoles' },
        h('h2', null, h('i'), 'Consoles', h('span', null, `${on} / ${S.devices.length} loaded`)),
        h('div', { class: 'crows pbody' }, rows),
        S.devices.length > 1 ? h('div', { class: 'cbulk pbody' }, bulk('primary', 'All consoles: insert', true, canAdd), bulk('danger', 'All consoles: eject', false, canRemove)) : null,
        S.devices.length > 1 && blocked ? h('p', { class: 'mute small pbody' }, 'A console still needs "warpzone whitelist migrate", so the all-consoles buttons wait. Use the buttons of each console.') : null,
        h('p', { class: 'mute small pbody' }, 'An ejected game leaves the console within about an hour: the console deletes it on its next cleanup.'));
    }

    // On a phone the console buttons sit at the bottom, where the thumb is.
    function paintConsoleBar(d, ref) {
      const bar = $('#consolebar');
      if (!bar) return;
      const dev = d.devices || {};
      bar.style.setProperty('--n', String(Math.max(1, S.devices.length)));
      setTint(bar, systemColor(ref.system));
      bar.replaceChildren(...S.devices.map((c) => {
        const st = store.gameState(dev, c.name, ref.system);
        const usable = (st === 'on' || st === 'off' || st === 'partial') && store.isManaged(c.name) && !ref.locked;
        const verb = st === 'on' ? 'Eject' : st === 'partial' ? 'Complete' : 'Insert';
        return h('button', {
          type: 'button', class: `cb-btn st-${st}`, disabled: !usable || null, 'aria-pressed': st === 'on' ? 'true' : st === 'partial' ? 'mixed' : 'false',
          title: `${c.name}: ${verb}`, dataset: { fk: 'mb:' + c.name, write: '1' },
          'aria-label': `${verb} ${ref.title} ${st === 'on' ? 'from' : 'into'} ${c.name}`,
          onclick: () => store.changeGame({ id: d.id, title: ref.title }, ref.system, c.name, st !== 'on'),
        }, h('span', { class: 'cb-n' }, consoleCode(c.name)),
        h('span', { class: 'cb-a' }, usable ? [icon(st === 'on' ? 'eject' : 'insert'), verb] : (st === 'unsupported' ? 'Not offered' : '-')));
      }));
    }

    async function load(silent) {
      try {
        const data = await api.game(id);
        if (view.dead) return;
        data.id = data.id || id;
        data.devices = data.devices || {};
        S.detail = { id, data };
        paint(!silent);
      } catch (err) {
        if (view.dead) return;
        await store.handleError(err);
        if (S.detail && S.detail.id === id && S.detail.data) return; // keep what is shown
        root.replaceChildren(emptyPanel('This game is not available', err.code === 'not-found' ? 'It no longer exists in the library.' : 'It could not be loaded.',
          h('a', { class: 'btn', href: ui.systemHref(system) }, `Back to ${systemName(system)}`)));
      }
    }

    S.detail = null;
    setMain(root, systemName(system));
    root.append(h('p', { class: 'loading-note' }, 'Loading...'));
    load(false);
    store.ensureQueue(15000);
    return Object.assign(view, {
      refresh() { if (S.detail && S.detail.id === id) paint(false); },
      queueChanged() { if (S.detail && S.detail.id === id) paint(false); },
      reload() { return load(true); },
      dispose() { const bar = $('#consolebar'); if (bar) bar.replaceChildren(); S.detail = null; },
    });
  }

  const resetFeatured = () => { featured = null; featuredData.clear(); featuredToken++; };

  W.views = { resetFeatured, viewHome, viewSystems, viewSystem, viewConsoles, viewInbox, viewSearch, viewGame, setPendingScroll, scrollMemory, setMain, localSearch, filterMemory };
})();
