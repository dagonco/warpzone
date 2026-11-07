/*
 * warpzone library UI: the pieces the views are made of.
 *
 * Art boxes, game tiles with their per-console slots, the NONE / SOME / ALL lever of a system,
 * system cards, and the console status (a LED, a bar, a line of text) that the progress poll
 * repaints in place. Every function returns nodes built with h(); none of them fetches.
 */
(function () {
  'use strict';

  const W = window.WZ;
  const { api, formatBytes, h, $, $$, plural, icon, tint, systemName, systemCode, systemColor, consoleCode, parseTitle, store } = W;
  const { S } = store;

  const gameHref = (system, id) => `#/game/${encodeURIComponent(system)}/${encodeURIComponent(id)}`;
  const systemHref = (system) => `#/system/${encodeURIComponent(system)}`;
  const systemTint = (system) => tint(systemColor(system));

  // ===== art ===============================================================================

  // An image over a placeholder. With no image to ask for (the list says there is none) or one
  // that fails to load, the box shows the tinted glyph of data/ui/placeholder.svg and the
  // system code, so a missing cover is never a hole.
  function artBox(o) {
    const code = o.system ? systemCode(o.system) : '';
    const box = h('span', { class: 'art' + (o.cls ? ' ' + o.cls : '') + (o.has === false ? ' ph' : '') },
      h('span', { class: 'phn', 'aria-hidden': 'true' }, code));
    if (o.has !== false) {
      const img = h('img', {
        src: api.mediaUrl(o.id, o.kind || 'covers'), alt: o.alt || '', loading: o.eager ? 'eager' : 'lazy', decoding: 'async',
      });
      img.addEventListener('error', () => { img.remove(); box.classList.add('ph'); });
      img.addEventListener('load', () => box.classList.add('loaded'));
      box.append(img);
    }
    return box;
  }

  function regionChip(region) {
    return region ? h('span', { class: 'rg', title: 'Region: ' + region }, region) : null;
  }

  // The RetroAchievements badge. Text only, by design (no RA images are fetched or shipped).
  const RA_TEXT = {
    supported: 'Achievements',
    linked: 'Known to RetroAchievements, no achievements yet',
    'not-linked': 'Not found in RetroAchievements',
    'no-hash': 'Not hashed yet',
    'no-list': 'No RetroAchievements list for this system',
  };

  function raBadge(ra) {
    const info = typeof ra === 'string' ? { state: ra } : (ra || {});
    if (!info.state) return null;
    let text = RA_TEXT[info.state] || info.state;
    if (info.state === 'supported' && info.achievements != null) {
      text = `${plural(info.achievements, 'achievement')}${info.points != null ? ', ' + plural(info.points, 'point') : ''}`;
    }
    const cls = 'badge ra' + (info.state === 'supported' ? '' : ' off');
    const body = [icon('trophy'), 'RA ', text];
    if (info.state === 'supported' && info.url) {
      return h('a', { class: cls, href: info.url, target: '_blank', rel: 'noopener noreferrer' }, body);
    }
    return h('span', { class: cls, title: RA_TEXT[info.state] }, body);
  }

  // ===== console status ====================================================================

  // One status per console, from the progress poll and the state of its whitelist. `key` drives
  // the colour, `pct` the bar. The names are the API's: unknown (no answer yet or it failed),
  // needs-migration, missing (no whitelist yet), pending, downloading, done, offline, paused and
  // not-shared.
  function statusOf(d) {
    const wl = d.whitelist && d.whitelist.state;
    if (wl === 'needs-migration') {
      return { key: 'needs-migration', text: 'Needs migration', long: 'The whitelist needs "warpzone whitelist migrate" on the hub', pct: null };
    }
    const p = S.progress[d.name];
    if (!p) {
      return S.progressError
        ? { key: 'unknown', text: 'Unknown', long: 'Progress is unknown for now', pct: null }
        : { key: 'checking', text: 'Checking', long: 'Checking', pct: null };
    }
    const left = p.need_bytes > 0 ? formatBytes(p.need_bytes) : '';
    switch (p.state) {
      case 'downloading':
        return { key: 'downloading', text: `Syncing${p.percent != null ? ' ' + p.percent + '%' : ''}`, long: `Downloading${p.percent != null ? ' ' + p.percent + '%' : ''}`, pct: p.percent == null ? 0 : p.percent, left };
      case 'pending': return { key: 'pending', text: 'Pending', long: 'Waiting for the console to apply the change', pct: 0 };
      case 'done': return { key: 'done', text: 'Up to date', long: 'Up to date', pct: 100 };
      case 'offline': return { key: 'offline', text: 'Offline', long: 'Offline', pct: 0 };
      case 'paused': return { key: 'paused', text: 'Paused', long: 'Paused', pct: 0 };
      case 'not-shared': return { key: 'not-shared', text: 'Not shared', long: 'The library folder is not shared with it', pct: 0 };
      default: return { key: 'unknown', text: p.state, long: p.state, pct: null };
    }
  }

  // Progress is drawn into nodes tagged data-pdev (the device) and data-pk (what to draw);
  // polling repaints them in place so the page is not rebuilt every few seconds.
  const PAINT = {
    state(el, st) { el.textContent = el.dataset.long ? st.long : st.text; },
    host(el, st) { el.dataset.st = st.key; },
    segs(el, st) {
      const lit = st.pct == null ? 0 : Math.round((st.pct / 100) * el.children.length);
      Array.from(el.children).forEach((c, i) => c.classList.toggle('on', i < lit));
    },
    bar(el, st) { el.style.setProperty('--w', String((st.pct || 0) / 100)); },
    left(el, st) { el.textContent = st.left ? ` · ${st.left} left` : ''; },
  };

  function paintProgress() {
    const seen = new Map();
    for (const el of $$('[data-pdev]')) {
      const name = el.dataset.pdev;
      if (!seen.has(name)) { const d = store.device(name); seen.set(name, d ? statusOf(d) : null); }
      const st = seen.get(name);
      if (st && PAINT[el.dataset.pk]) PAINT[el.dataset.pk](el, st);
    }
    const banner = $('#banner');
    if (!banner) return;
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

  const pnode = (tag, name, kind, cls, long) =>
    h(tag, { class: cls, dataset: { pdev: name, pk: kind, long: long ? '1' : '' } });

  function segs(name, n) {
    const el = pnode('span', name, 'segs', 'segs');
    for (let i = 0; i < (n || 20); i++) el.append(h('i'));
    return el;
  }

  // The header LED of a console: a lamp, its code and, where there is room, its state.
  function led(d) {
    const st = statusOf(d);
    return h('li', null, h('a', {
      class: 'led', href: '#/consoles', dataset: { pdev: d.name, pk: 'host', st: st.key, fk: 'led:' + d.name },
      title: `${d.name}: ${st.long}`,
    },
    h('span', { class: 'dot' }),
    h('span', { class: 'lt' },
      h('span', { class: 'nm' }, consoleCode(d.name)),
      pnode('span', d.name, 'state', 'st')),
    h('span', { class: 'ledbar', 'aria-hidden': 'true' }, pnode('i', d.name, 'bar', 'ledbar-fill'))));
  }

  // ===== slots: one game on one console ====================================================

  // The state of a game on every console, from whichever copy of it is at hand.
  function devicesOf(id) {
    const known = S.byId.get(id);
    if (known) return known.game.devices;
    if (S.detail && S.detail.id === id && S.detail.data) return S.detail.data.devices || {};
    if (S.searchResult) {
      for (const group of S.searchResult.groups) {
        for (const g of group.games) if (g.id === id) return g.devices || {};
      }
    }
    return {};
  }

  const SLOT_VERB = { on: 'inserted', partial: 'partly inserted', off: 'not inserted', unsupported: 'not offered', unknown: 'unknown' };

  function paintSlot(b) {
    const { gid, dev, sys } = b.dataset;
    const st = store.gameState(devicesOf(gid), dev, sys);
    const managed = store.isManaged(dev);
    const locked = b.dataset.locked === '1';
    const usable = (st === 'on' || st === 'off' || st === 'partial') && managed && !locked;
    b.className = 'slot st-' + st;
    b.disabled = !usable;
    b.setAttribute('aria-pressed', st === 'on' ? 'true' : st === 'partial' ? 'mixed' : 'false');
    const action = st === 'on' ? 'eject' : 'insert';
    b.title = usable
      ? `${dev}: ${SLOT_VERB[st]}. Press to ${action}.`
      : !managed ? `${dev}: its whitelist needs "warpzone whitelist migrate".`
        : locked ? `${dev}: this game cannot be changed from the UI.`
          : `${dev}: ${SLOT_VERB[st]}.`;
    b.setAttribute('aria-label', `${b.dataset.gtitle} on ${dev}: ${SLOT_VERB[st]}`);
  }

  function slotBtn(ref, d) {
    const b = h('button', {
      type: 'button',
      dataset: { gid: ref.id, dev: d.name, sys: ref.system, gtitle: ref.title, locked: ref.locked ? '1' : '', fk: `slot:${ref.id}:${d.name}` },
      onclick: () => {
        const st = store.gameState(devicesOf(ref.id), d.name, ref.system);
        store.changeGame({ id: ref.id, title: ref.title }, ref.system, d.name, st === 'off' || st === 'partial');
      },
    }, h('i'), h('span', null, consoleCode(d.name)));
    paintSlot(b);
    return b;
  }

  const slots = (ref) => h('div', { class: 'slots' }, S.devices.map((d) => slotBtn(ref, d)));
  const paintSlots = (root) => { for (const b of $$('.slot', root)) paintSlot(b); };

  // ===== tiles =============================================================================

  // `g` is a compact game of a system list. The title shows the name without its tags and the
  // tags (a disc number, a revision) stay on the line below, so two discs never look alike.
  function tile(g, system, opts) {
    const o = opts || {};
    const t = parseTitle(g.name || g.title);
    const sub = [...t.tags, g.size ? formatBytes(g.size) : ''].filter(Boolean).join(' · ');
    const ref = { id: g.id, system, title: t.name, locked: (g.flags || []).includes('unlistable') };
    return h('article', { class: 'tile', vars: o.inherit ? null : systemTint(system) },
      h('a', { class: 'tl', href: gameHref(system, g.id), dataset: { fk: 'g:' + g.id }, title: t.full },
        h('span', { class: 'artw' },
          artBox({ id: g.id, system, has: g.cover }),
          g.ra === 'supported' ? h('span', { class: 'b-ra', title: RA_TEXT.supported }, icon('trophy'), 'RA') : null,
          regionChip(t.region),
          o.sys ? h('span', { class: 'b-sys', title: systemName(system) }, systemCode(system)) : null),
        h('span', { class: 't' }, t.name),
        h('span', { class: 's' }, sub || ' ')),
      slots(ref));
  }

  function skeletonTiles(n) {
    return Array.from({ length: n }, () => h('div', { class: 'tile skeleton', 'aria-hidden': 'true' },
      h('span', { class: 'artw' }), h('span', { class: 't' }, ' '), h('span', { class: 's' }, ' ')));
  }

  // ===== the lever of a system =============================================================

  // What one lever shows: a console's share of a system, or all consoles' together.
  function leverModel(sys, deviceKey) {
    if (deviceKey === '*') {
      const usable = S.devices.filter((d) => !store.excludes(d.name, sys));
      const covs = usable.map((d) => store.coverage(sys, d.name));
      let state = 'some';
      if (covs.length && covs.every((c) => c.state === 'all')) state = 'all';
      else if (!covs.length || covs.every((c) => c.state === 'none')) state = 'none';
      return {
        sys, key: '*', label: 'All consoles', title: 'Every console that takes this system', state,
        whole: covs.length > 0 && covs.every((c) => c.whole), n: null, total: 0, size: null, of: 0,
        disabled: !covs.length || store.allBlocked(sys),
        note: covs.length ? 'Waiting for "warpzone whitelist migrate"' : 'No console offers it',
        pdev: null,
      };
    }
    const cov = store.coverage(sys, deviceKey);
    const unsup = cov.state === 'unsupported';
    const unmanaged = !store.isManaged(deviceKey);
    const unknown = cov.state === 'unknown';
    return {
      sys, key: deviceKey, label: deviceKey, title: deviceKey, state: unsup || unknown ? 'none' : cov.state, whole: cov.whole,
      n: cov.n, total: cov.total, size: cov.size, of: cov.of,
      disabled: unsup || unmanaged || unknown,
      note: unsup ? 'Not offered to this console' : unmanaged ? 'Needs "warpzone whitelist migrate"' : unknown ? 'State unknown' : '',
      pdev: deviceKey,
    };
  }

  function stateLine(m) {
    if (m.disabled) return m.note;
    if (m.state === 'all') return m.whole ? 'Whole system' : 'Every game, one by one';
    if (m.state === 'some') return 'Game by game';
    return 'Nothing';
  }

  // opts.compact: the system's name leads (a console's page lists every system), otherwise the
  // console's name does (the system's page lists every console).
  function lever(sys, deviceKey, opts) {
    const o = opts || {};
    const m = leverModel(sys, deviceKey);
    const name = o.compact ? systemName(sys) : m.label;
    const here = m.disabled ? '' : m.state;
    const sizeText = m.size != null && m.of ? `${formatBytes(m.size)} of ${formatBytes(m.of)}` : m.of ? formatBytes(m.of) : '';
    const count = m.n != null && m.total ? h('span', { class: 'lv-c' }, h('b', null, m.n.toLocaleString('en-US')), ` / ${m.total.toLocaleString('en-US')}`) : null;
    const pct = m.n != null && m.total ? m.n / m.total : here === 'all' ? 1 : here === 'some' ? 0.5 : 0;
    const press = (to) => () => {
      const on = to === 'all';
      store.changeSystem(sys, deviceKey, on, here === (on ? 'all' : 'none') && (on ? m.whole : true));
    };
    const notchBtn = (to, label, ic, verb) => h('button', {
      type: 'button', class: 'notch n-' + to + (here === to ? ' on' : ''),
      disabled: m.disabled || null,
      'aria-pressed': String(here === to),
      'aria-label': `${verb} ${systemName(sys)} ${to === 'all' ? 'into' : 'from'} ${deviceKey === '*' ? 'all consoles' : deviceKey}`,
      title: here === 'all' && to === 'all' && !m.whole ? 'Every game is on one by one. Press to make it the whole system.' : null,
      dataset: { fk: `lv:${sys}:${deviceKey}:${to}`, write: '1' },
      onclick: press(to),
    }, icon(ic), h('span', null, label));
    const el = h('div', {
      class: `lever${o.compact ? ' compact' : ''}${deviceKey === '*' ? ' all' : ''} st-${here || 'off'}${m.disabled ? ' is-disabled' : ''}`,
      role: 'group', 'aria-label': `${name}, ${systemName(sys)}: ${stateLine(m)}`,
      dataset: m.pdev ? { pdev: m.pdev, pk: 'host' } : null,
    },
    h('div', { class: 'lv-h' },
      h('span', { class: 'dot' }),
      h('span', { class: 'lv-n', title: o.compact ? systemName(sys) : m.title }, name),
      count),
    h('div', { class: 'lv-s' },
      h('span', { class: 'lv-st' }, stateLine(m)),
      sizeText && !o.compact ? h('span', { class: 'lv-sz' }, sizeText) : null),
    h('div', { class: 'lv-cov', 'aria-hidden': 'true' }, h('i', { vars: { w: String(pct) } })),
    h('div', { class: 'notches' },
      notchBtn('none', 'NONE', 'eject', 'Eject all of'),
      h('span', { class: 'notch n-some' + (here === 'some' ? ' on' : ''), 'aria-hidden': 'true' }, h('span', null, 'SOME')),
      notchBtn('all', 'ALL', 'insert', 'Insert all of')));
    return el;
  }

  // ===== system card =======================================================================

  function sysCard(s) {
    const list = store.S.lists.get(s.system);
    const picks = list ? list.games.filter((g) => g.cover).slice(0, 3) : [];
    // Up to three covers fanned out; a system with none shows one placeholder in the middle.
    const slotsOf = { 0: ['f1'], 1: ['f1'], 2: ['f0', 'f1'], 3: ['f0', 'f1', 'f2'] }[picks.length];
    const fan = slotsOf.map((cls, i) => h('span', { class: 'f ' + cls },
      artBox({ id: picks[i] ? picks[i].id : '', system: s.system, has: !!picks[i] })));
    const covs = S.devices.map((d) => {
      const c = store.coverage(s.system, d.name);
      const pct = c.n != null && c.total ? c.n / c.total : c.state === 'all' ? 1 : c.state === 'some' ? 0.5 : 0;
      const label = c.state === 'unsupported' ? '-' : c.n != null ? c.n.toLocaleString('en-US') : c.state === 'some' ? '~' : '?';
      return h('span', { class: 'cv ' + c.state, title: `${d.name}: ${c.state === 'unsupported' ? 'not offered' : c.state}` },
        h('b', null, consoleCode(d.name)), h('span', { class: 'cb' }, h('i', { vars: { w: String(pct) } })), h('em', null, label));
    });
    return h('a', { class: 'syscard', href: systemHref(s.system), vars: systemTint(s.system), dataset: { fk: 's:' + s.system }, title: systemName(s.system) },
      h('span', { class: 'plate' }, h('b', null, systemCode(s.system)), h('span', null, s.games.toLocaleString('en-US'))),
      h('span', { class: 'fan' }, fan),
      h('span', { class: 'sn' }, systemName(s.system)),
      h('span', { class: 'sz' }, `${plural(s.games, 'game')} · ${formatBytes(s.size)}`),
      h('span', { class: 'covs' }, covs));
  }

  W.ui = {
    gameHref, systemHref, systemTint, artBox, regionChip, raBadge, RA_TEXT,
    statusOf, paintProgress, pnode, segs, led,
    devicesOf, paintSlot, paintSlots, slotBtn, slots, tile, skeletonTiles,
    leverModel, lever, sysCard,
  };
})();
