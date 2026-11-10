/*
 * warpzone library UI: the play queue.
 *
 * The "Up next" section of the home page, the queue panel of a game page and the dialogs that
 * go with them (which consoles to queue a game for, what to do with the game on the consoles
 * when it is finished, dropped or removed, which game a relink points at). All decisions belong
 * to the server (development/design/play-queue.md): which state an entry shows, what each
 * console's claim looks like, what a release would do. This file draws those answers and sends
 * the operations, always with the etag it saw. It never turns a game off without the user
 * ticking it in a dialog.
 *
 * The data and the writes are in store.js (queueRun, queueDry); the "queued" mark of a tile is
 * in components.js.
 */
(function () {
  'use strict';

  const W = window.WZ;
  const { api, humanError, formatBytes, formatMoment, timeAgo, h, $, plural, tint, systemName, systemCode, systemColor, parseTitle,
    store, ui } = W;
  const { S, toast } = store;

  const HOME_ROWS = 10;      // active entries the home page lists before "Show all"
  const HISTORY_ROWS = 10;   // ended entries "Recently finished" lists before "Show all"
  const COOKIE = 'wz_queue_dev';

  const AWARD_TEXT = {
    'beaten-softcore': ['You beat it', 'softcore'], 'beaten-hardcore': ['You beat it', 'hardcore'],
    completed: ['You completed it', 'softcore mastery'], mastered: ['You mastered it', 'hardcore'],
  };

  const entryName = (e) => parseTitle(e.name || e.title).name;

  // ===== helpers ===========================================================================

  const fkOf = () => {
    const el = document.activeElement;
    return el && el.dataset ? el.dataset.fk || null : null;
  };

  // Focus the element with this key again after a repaint; a button that is now disabled (the
  // entry reached the end of the queue) hands the focus to its twin.
  function refocus(fk) {
    if (!fk) return;
    const find = (key) => $(`[data-fk="${CSS.escape(key)}"]`);
    let el = find(fk);
    if ((!el || el.disabled) && /^q-(up|down):/.test(fk)) el = find(fk.replace(/^q-(up|down):/, (_, d) => `q-${d === 'up' ? 'down' : 'up'}:`));
    if (el && !el.disabled) el.focus({ preventScroll: true });
  }

  // The consoles the user chose last (a display preference, kept in a plain cookie like the CRT
  // switch): null when never chosen, [] for "none".
  function lastConsoles() {
    const m = new RegExp('(?:^|;\\s*)' + COOKIE + '=([^;]*)').exec(document.cookie);
    if (!m) return null;
    const v = decodeURIComponent(m[1]);
    return v === '-' ? [] : v.split(',').filter(Boolean);
  }

  function rememberConsoles(names) {
    document.cookie = `${COOKIE}=${names.length ? encodeURIComponent(names.join(',')) : '-'}; Path=/; Max-Age=31536000; SameSite=Strict`;
  }

  async function runOps(ops, message) {
    const res = await store.queueRun(ops);
    if (res && message) toast(message + store.skippedNote(res), 'ok');
    return res;
  }

  // ===== the dialog ========================================================================

  // One dialog for everything here. Returns a controller: `body` and `acts` are containers to
  // fill, `close(value)` closes it, `done` resolves with that value (null on Escape or cancel).
  function openModal(kicker, heading, color) {
    const dlg = $('#queue-dialog');
    if (dlg.open) dlg.close();
    const body = h('div', { class: 'q-mbody' });
    const acts = h('div', { class: 'macts' });
    const ctl = { body, acts, closed: false, result: null };
    ctl.done = new Promise((resolve) => {
      dlg.addEventListener('close', () => { ctl.closed = true; resolve(ctl.result); }, { once: true });
    });
    ctl.close = (value) => { ctl.result = value === undefined ? null : value; dlg.close(); };
    for (const [k, v] of Object.entries(tint(color))) dlg.style.setProperty('--' + k, v);
    dlg.className = 'modal q-dialog';
    dlg.replaceChildren(h('div', { class: 'modal-inner' },
      h('div', { class: 'mh' }, h('i'), kicker),
      h('div', { class: 'mb' }, h('h2', { id: 'queue-dialog-title' }, heading), body),
      acts));
    dlg.showModal();
    return ctl;
  }

  const cancelBtn = (m, label) => h('button', { type: 'button', class: 'btn', dataset: { fk: 'q-cancel' }, onclick: () => m.close(null) }, label || 'Cancel');

  // ===== queue for consoles ================================================================

  // The state of the game on each console, from whichever copy of it the page holds, or the
  // game's own answer when the page holds none (a game opened from the history).
  async function gameDevices(id) {
    if (S.byId.has(id) || (S.detail && S.detail.id === id && S.detail.data)) return ui.devicesOf(id);
    try {
      const d = await api.game(id);
      return d.devices || {};
    } catch (err) {
      return {};
    }
  }

  // The picker: which consoles to turn the game on for (none is a choice). A dry run says what
  // each chosen console would do before anything is written. With an active `entry` it adds
  // consoles to it; otherwise it queues the game (again, when its entry had ended).
  async function openPicker(o) {
    if (!store.queueUsable()) { toast('The queue is not available, so it cannot be changed.', 'error'); return null; }
    const entry = o.entry && (o.entry.state === 'queued' || o.entry.state === 'playing') ? o.entry : null;
    const devs = await gameDevices(o.id);
    const have = entry ? entry.devices || {} : {};
    const rows = S.devices.map((d) => {
      const st = store.gameState(devs, d.name, o.system);
      let why = '';
      if (o.locked) why = 'This game cannot be changed from the UI.';
      else if (st === 'unsupported') why = 'This console does not take this system.';
      else if (!store.isManaged(d.name)) why = 'Its whitelist needs "warpzone whitelist migrate".';
      else if (st === 'unknown') why = 'The state of the game there is not known.';
      else if (have[d.name] && have[d.name].end == null) why = 'Already queued for it.';
      return { name: d.name, st, why };
    });
    const usable = rows.filter((r) => !r.why).map((r) => r.name);
    const last = lastConsoles();
    const sel = new Set(entry ? [] : last === null ? (usable.length === 1 ? usable : []) : last.filter((n) => usable.includes(n)));
    const name = parseTitle(o.title).name;
    const m = openModal(entry ? 'Add consoles' : 'Play queue', entry ? `Add consoles for ${name}` : `Queue ${name}`, systemColor(o.system));
    let dry = null;
    let error = '';
    let loading = false;
    let token = 0;

    const opsFor = () => {
      const chosen = rows.filter((r) => sel.has(r.name)).map((r) => r.name);
      return entry ? [{ op: 'devices', game: o.id, add: chosen }] : [{ op: 'add', game: o.id, devices: chosen, position: null }];
    };

    const noteOf = (r) => {
      if (!sel.has(r.name)) return '';
      if (loading) return 'Checking...';
      const d = dry && dry.devices.find((x) => x.device === r.name);
      const bytes = d && d.bytes_on ? ` (${formatBytes(d.bytes_on)} to download)` : '';
      if (r.st === 'on') return `Already on ${r.name}: it will stay there when you finish it.`;
      if (r.st === 'partial') return `Completes it on ${r.name}${bytes}. It will stay there when you finish it.`;
      return `Turns it on for ${r.name}${bytes}.`;
    };

    const boxes = new Map();
    const notes = new Map();
    const list = h('ul', { class: 'q-opts' }, rows.map((r) => {
      const cb = h('input', { type: 'checkbox', checked: sel.has(r.name) || null, disabled: !!r.why || null, dataset: { fk: 'qp:' + r.name } });
      cb.addEventListener('change', () => { if (cb.checked) sel.add(r.name); else sel.delete(r.name); check(); });
      const note = h('small', { class: 'q-note' }, r.why);
      boxes.set(r.name, cb);
      notes.set(r.name, note);
      return h('li', { class: 'q-opt' + (r.why ? ' off' : '') }, h('label', { class: 'q-check' }, cb, h('span', null, h('b', null, r.name), note)));
    }));
    const msg = h('p', { class: 'error-text', role: 'alert', hidden: true });
    const summary = h('p', { class: 'mute small' });
    const confirm = h('button', { type: 'button', class: 'btn primary', dataset: { fk: 'q-confirm' }, onclick: () => m.close([...sel]) }, entry ? 'Add consoles' : 'Add to queue');

    function paint() {
      for (const r of rows) if (!r.why) notes.get(r.name).textContent = noteOf(r);
      msg.hidden = !error;
      msg.textContent = error;
      summary.textContent = sel.size ? '' : entry
        ? 'Choose at least one console.'
        : 'No console chosen: the game is queued without turning it on anywhere.';
      confirm.disabled = loading || !!error || (entry && !sel.size);
    }

    async function check() {
      const mine = ++token;
      if (!sel.size) { dry = null; error = ''; loading = false; paint(); return; }
      loading = true;
      paint();
      const r = await store.queueDry(opsFor());
      if (mine !== token || m.closed) return;
      loading = false;
      dry = r.res || null;
      error = r.error ? humanError(r.error) : '';
      paint();
    }

    m.body.replaceChildren(
      h('p', { class: 'mute small' }, usable.length
        ? 'Queueing a game for a console turns it on in that console\'s whitelist, so it is downloaded before you pick the console up.'
        : 'No console can take this game, so it is queued without turning it on anywhere.'),
      rows.length ? list : null, msg, summary);
    m.acts.replaceChildren(cancelBtn(m), confirm);
    paint();
    if (sel.size) check();
    const first = usable.length ? boxes.get(usable[0]) : confirm;
    first.focus();

    const chosen = await m.done;
    if (!chosen) return null;
    rememberConsoles(chosen);
    const where = chosen.length ? ` for ${chosen.join(', ')}` : '';
    return runOps(opsFor(), entry ? `Added ${chosen.join(', ')} to "${name}".` : `Queued "${name}"${where}.`);
  }

  // ===== finish, drop, remove, take a console off ==========================================

  // What the queue owes the consoles of an active or ended entry. `releasable`: consoles where
  // the queue turned the game on (or a release that did not reach the whitelist is still pending)
  // and which can be turned off now. `keepOnly`: owed too, but not releasable (a console that left
  // the config, a game that left the library, a state that is not known): they can only be kept.
  // `stays`: open claims on a game the user already had; they end by themselves and are never turned off.
  function claimPlan(entry) {
    const plan = { releasable: [], keepOnly: [], stays: [] };
    for (const [name, c] of Object.entries(entry.devices || {})) {
      const held = c.before === 'off' && c.end == null;
      const pending = c.before === 'off' && c.end === 'released' && c.view === 'not-released';
      if (held || pending) {
        const can = entry.found === 'present' && !!store.device(name) && store.isManaged(name) && ['on', 'not-on', 'not-released'].includes(c.view);
        (can ? plan.releasable : plan.keepOnly).push(name);
      } else if (c.end == null) {
        plan.stays.push(name);
      }
    }
    return plan;
  }

  // The operation for an end flow with this list of consoles to release: every console the queue
  // owes is named in `release` or `keep`, as the server requires.
  function endOp(entry, how, plan, release) {
    const keep = [...plan.keepOnly, ...plan.releasable.filter((n) => !release.includes(n))];
    if (how.kind === 'remove') return { op: 'remove', game: entry.id, release, keep };
    if (how.kind === 'state') return { op: 'state', game: entry.id, state: how.state, release, keep };
    const mine = release.filter((n) => n === how.device);
    return { op: 'devices', game: entry.id, release: mine, keep: mine.length ? [] : [how.device] };
  }

  const END_TEXT = {
    state: { finished: ['Finished', 'Mark as finished'], dropped: ['Dropped', 'Mark as dropped'] },
  };

  // Ask which consoles to turn the game off on. Resolves the list to release, or null on cancel.
  async function askRelease(entry, how, plan) {
    const name = entryName(entry);
    const heading = how.kind === 'remove' ? `Remove ${name} from the queue?`
      : how.kind === 'device' ? `Take ${name} off ${how.device}?`
        : how.state === 'finished' ? `Finished ${name}?` : `Drop ${name}?`;
    const confirmText = how.kind === 'remove' ? 'Remove from queue' : how.kind === 'device' ? `Take off ${how.device}` : END_TEXT.state[how.state][1];
    const m = openModal('Play queue', heading, '#ff5470');
    m.body.replaceChildren(h('p', { class: 'mute' }, 'Checking...'));
    m.acts.replaceChildren(cancelBtn(m));
    $('[data-fk="q-cancel"]', m.acts).focus();
    const r = await store.queueDry([endOp(entry, how, plan, plan.releasable)]);
    if (m.closed) return null;
    if (r.error) {
      m.body.replaceChildren(h('p', { class: 'error-text', role: 'alert' }, humanError(r.error)));
      m.acts.replaceChildren(cancelBtn(m, 'Close'));
      await m.done;
      return null;
    }
    const res = r.res;
    const checks = new Map();
    const items = [];
    for (const n of plan.releasable) {
      const skip = (res.skipped || []).find((s) => s.device === n);
      if (skip) {
        items.push(h('li', { class: 'q-opt stay' }, skip.reason === 'covered'
          ? `Stays on ${n}: the whole system (or a folder of it) is turned on there.`
          : `Stays on ${n}: another queue entry holds it there.`));
        continue;
      }
      const d = (res.devices || []).find((x) => x.device === n);
      const frees = d && d.bytes_off ? `frees ${formatBytes(d.bytes_off)}; ` : '';
      const cb = h('input', { type: 'checkbox', checked: true, dataset: { fk: 'qr:' + n } });
      checks.set(n, cb);
      items.push(h('li', { class: 'q-opt' }, h('label', { class: 'q-check' }, cb,
        h('span', null, h('b', null, `Remove from ${n}`), h('small', { class: 'q-note' }, `${frees}gone within about an hour`)))));
    }
    for (const n of plan.keepOnly) {
      items.push(h('li', { class: 'q-opt stay' }, `Stays on ${n}: ${entry.found === 'present' ? 'its state is not known right now' : 'the game is no longer in the library'}.`));
    }
    if (how.kind !== 'device') {
      for (const n of plan.stays) items.push(h('li', { class: 'q-opt stay' }, `Stays on ${n}: it was there before you queued it.`));
    }
    const confirm = h('button', { type: 'button', class: 'btn primary', dataset: { fk: 'q-confirm' },
      onclick: () => m.close([...checks].filter(([, cb]) => cb.checked).map(([n]) => n)) }, confirmText);
    m.body.replaceChildren(
      h('p', { class: 'mute small' }, checks.size
        ? 'The queue turned this game on for these consoles. Tick the ones to turn it off on; the rest keep it.'
        : 'Nothing will be turned off.'),
      h('ul', { class: 'q-opts' }, items),
      h('p', { class: 'mute small' }, 'A console deletes the files within about an hour, when its cleanup runs. Its saves are never touched, and queueing the game again before then costs nothing.'));
    m.acts.replaceChildren(cancelBtn(m), confirm);
    const firstBox = [...checks.values()][0];
    (firstBox || $('[data-fk="q-cancel"]', m.acts)).focus();
    return m.done;
  }

  // Finish, drop or remove an entry, or take one console off it. With nothing the queue could
  // turn off there is no question to ask; otherwise the dialog decides.
  async function endEntry(entry, how) {
    if (!store.queueUsable()) { toast('The queue is not available, so it cannot be changed.', 'error'); return null; }
    let plan = claimPlan(entry);
    if (how.kind === 'device') {
      const n = how.device;
      plan = { releasable: plan.releasable.filter((x) => x === n), keepOnly: plan.keepOnly.filter((x) => x === n), stays: [] };
    }
    const name = entryName(entry);
    let release = [];
    if (plan.releasable.length) {
      release = await askRelease(entry, how, plan);
      if (release === null) return null;
    }
    const left = release.length ? ` It will leave ${release.join(', ')} within about an hour.` : '';
    const message = how.kind === 'remove' ? `Removed "${name}" from the queue.${left}`
      : how.kind === 'device' ? `"${name}" is no longer held on ${how.device}.${left}`
        : `Marked "${name}" as ${how.state}.${left}`;
    return runOps([endOp(entry, how, plan, release)], message);
  }

  const setState = (entry, state) => runOps([{ op: 'state', game: entry.id, state }], `"${entryName(entry)}" is now ${state}.`);
  const dismiss = (entry) => runOps([{ op: 'dismiss', game: entry.id }], 'Dismissed. It will ask again only for a higher award.');

  function move(entry, delta) {
    const at = S.queue.entries.findIndex((e) => e.id === entry.id);
    if (at < 0) return null;
    return runOps([{ op: 'move', game: entry.id, position: at + delta }]);
  }

  function sync(entry, name) {
    const turnOn = entry.devices[name].view === 'not-on';
    return runOps([{ op: 'sync', game: entry.id, device: name }],
      turnOn ? `Turned "${entryName(entry)}" on for ${name}.` : `Removing "${entryName(entry)}" from ${name}.`);
  }

  // The user confirms a relink: the entry keeps its place and dates, and the consoles the queue
  // had it on are left as they are.
  async function relink(entry, cand) {
    if (!store.queueUsable()) return null;
    const m = openModal('Play queue', `Use ${cand.name} instead?`, systemColor(cand.system));
    m.body.replaceChildren(
      h('p', null, `"${entryName(entry)}" is no longer in the library. Point this queue entry at "${cand.name}" (${systemName(cand.system)})?`),
      h('p', { class: 'mute small' }, 'It keeps its place, its state and its dates. The consoles the queue turned the old game on for are left as they are: the queue will not turn them off, and the new game starts with no console.'));
    const confirm = h('button', { type: 'button', class: 'btn primary', dataset: { fk: 'q-confirm' }, onclick: () => m.close(true) }, 'Use this game');
    m.acts.replaceChildren(cancelBtn(m), confirm);
    confirm.focus();
    if (!(await m.done)) return null;
    return runOps([{ op: 'relink', game: entry.id, to: cand.id }], `"${entryName(entry)}" now points at "${cand.name}".`);
  }

  // ===== pieces both places draw ===========================================================

  const VIEW_TEXT = {
    'not-on': 'not on the console yet',
    kept: 'left as it is',
    released: 'removed (gone within about an hour)',
    'not-released': 'still on the console',
    unknown: 'state not known',
  };

  // One line per console the entry was queued for: its view of the claim, and the repair or the
  // way out that applies. `opts.takeOff` adds a "Take off" button to an open claim.
  function claimLines(entry, opts) {
    const o = opts || {};
    return Object.entries(entry.devices || {}).map(([name, c]) => {
      const open = c.end == null;
      let text;
      if (c.view === 'on') text = ui.pnode('span', name, 'state', 'q-prog', true);
      else if (c.view === 'kept') text = c.end === 'kept' ? 'left as it is' : 'was there before: it stays';
      else text = VIEW_TEXT[c.view] || c.view;
      const fix = c.view === 'not-on' || c.view === 'not-released'
        ? h('button', {
          type: 'button', class: 'btn sm', dataset: { fk: `q-sync:${entry.id}:${name}`, write: '1' },
          'aria-label': `${c.view === 'not-on' ? 'Turn on' : 'Remove'} ${entryName(entry)} ${c.view === 'not-on' ? 'for' : 'from'} ${name}`,
          onclick: () => sync(entry, name),
        }, c.view === 'not-on' ? 'Turn on' : 'Remove') : null;
      const off = o.takeOff && open && entry.found === 'present'
        ? h('button', {
          type: 'button', class: 'btn sm', dataset: { fk: `q-off:${entry.id}:${name}`, write: '1' },
          'aria-label': `Take ${entryName(entry)} off ${name}`,
          onclick: () => endEntry(entry, { kind: 'device', device: name }),
        }, 'Take off') : null;
      return h('li', { class: `q-dev v-${c.view}`, dataset: { view: c.view } },
        h('b', { title: name }, name), h('span', { class: 'q-dv' }, text), fix, off);
    });
  }

  function stateMark(entry) {
    const playing = entry.shown === 'playing';
    return h('span', {
      class: 'q-state' + (playing ? ' playing' : ''),
      title: entry.inferred ? 'Shown as playing because you played it since you queued it' : null,
    }, playing ? 'Playing' : 'Queued');
  }

  // "You beat it (hardcore). Mark it finished?" with the two answers.
  function suggestionBox(entry) {
    const s = entry.suggestion;
    if (!s) return null;
    const [text, how] = AWARD_TEXT[s.award] || ['You finished it', ''];
    return h('div', { class: 'q-sugg', role: 'group', 'aria-label': 'RetroAchievements suggestion' },
      h('span', null, `${text}${how ? ` (${how})` : ''}. Mark it finished?`),
      h('button', { type: 'button', class: 'btn sm primary', dataset: { fk: 'q-yes:' + entry.id, write: '1' }, onclick: () => endEntry(entry, { kind: 'state', state: 'finished' }) }, 'Yes'),
      h('button', { type: 'button', class: 'btn sm', dataset: { fk: 'q-no:' + entry.id, write: '1' }, onclick: () => dismiss(entry) }, 'Not now'));
  }

  // ===== the home page: Up next ============================================================

  function homeSection() {
    let showAll = false;
    let histAll = false;
    let histOpen = false;
    const note = h('span', { class: 'n' });
    const body = h('div', { class: 'q-sbody' });
    const node = h('section', { class: 'sec', 'aria-label': 'Up next', hidden: true },
      h('div', { class: 'sechead' }, h('h2', null, h('i'), 'Up next'), h('span', { class: 'rule' }), note), body);

    function row(e, idx, count) {
      const present = e.found === 'present';
      const t = parseTitle(e.name || e.title);
      const art = ui.artBox({ id: e.id, system: e.system, has: present && !!e.cover });
      const head = present
        ? h('a', { class: 'q-title', href: ui.gameHref(e.system, e.id), dataset: { fk: 'q-g:' + e.id }, title: t.full }, t.name)
        : h('span', { class: 'q-title', title: t.full }, t.name);
      const meta = [systemCode(e.system), present && e.size ? formatBytes(e.size) : '', e.added ? `Added ${timeAgo(e.added)}` : ''].filter(Boolean).join(' · ');
      const cands = e.found === 'moved'
        ? h('div', { class: 'q-cands' },
          h('p', null, 'No longer in the library. Is it one of these?'),
          e.candidates.map((c) => h('button', {
            type: 'button', class: 'btn sm', dataset: { fk: `q-rl:${e.id}:${c.id}`, write: '1' },
            title: `${c.title} (${systemName(c.system)})`, onclick: () => relink(e, c),
          }, `${parseTitle(c.name || c.title).name} · ${systemCode(c.system)}`)))
        : e.found === 'missing' ? h('p', { class: 'q-gonetext' }, 'No longer in the library.') : null;
      const lines = present ? claimLines(e) : [];
      return h('li', { class: 'q-row' + (present ? '' : ' q-gone'), vars: ui.systemTint(e.system) },
        h('span', { class: 'q-art', 'aria-hidden': 'true' }, art),
        h('div', { class: 'q-body' },
          h('div', { class: 'q-head' }, head, present ? stateMark(e) : null),
          h('p', { class: 'q-meta' }, meta),
          present ? suggestionBox(e) : null,
          present ? (lines.length ? h('ul', { class: 'q-devs' }, lines) : h('p', { class: 'mute small' }, 'Not queued for a console.')) : null,
          cands),
        h('div', { class: 'q-actions' },
          present ? [
            h('button', {
              type: 'button', class: 'btn sm', disabled: idx === 0 || null, dataset: { fk: 'q-up:' + e.id, write: '1' },
              'aria-label': `Move ${t.name} up`, title: 'Move up', onclick: () => move(e, -1),
            }, 'Up'),
            h('button', {
              type: 'button', class: 'btn sm', disabled: idx === count - 1 || null, dataset: { fk: 'q-down:' + e.id, write: '1' },
              'aria-label': `Move ${t.name} down`, title: 'Move down', onclick: () => move(e, 1),
            }, 'Down'),
            h('button', { type: 'button', class: 'btn sm', dataset: { fk: 'q-fin:' + e.id, write: '1' }, 'aria-label': `Mark ${t.name} as finished`, onclick: () => endEntry(e, { kind: 'state', state: 'finished' }) }, 'Finished'),
            h('button', { type: 'button', class: 'btn sm', dataset: { fk: 'q-drop:' + e.id, write: '1' }, 'aria-label': `Drop ${t.name}`, onclick: () => endEntry(e, { kind: 'state', state: 'dropped' }) }, 'Dropped'),
          ] : null,
          h('button', { type: 'button', class: 'btn sm danger', dataset: { fk: 'q-rm:' + e.id, write: '1' }, 'aria-label': `Remove ${t.name} from the queue`, onclick: () => endEntry(e, { kind: 'remove' }) }, 'Remove')));
    }

    function histRow(e) {
      const present = e.found === 'present';
      const t = parseTitle(e.name || e.title);
      const title = present
        ? h('a', { class: 'q-title', href: ui.gameHref(e.system, e.id), dataset: { fk: 'q-hg:' + e.id }, title: t.full }, t.name)
        : h('span', { class: 'q-title', title: t.full }, t.name);
      return h('li', { class: 'q-hist-row', vars: ui.systemTint(e.system) },
        h('div', { class: 'q-hist-main' }, title,
          h('span', { class: 'q-state ' + e.state }, e.state === 'finished' ? 'Finished' : 'Dropped'),
          h('span', { class: 'q-meta', title: formatMoment(e.ended) }, `${systemCode(e.system)} · ${timeAgo(e.ended)}`)),
        h('div', { class: 'q-actions' },
          present ? h('button', {
            type: 'button', class: 'btn sm', dataset: { fk: 'q-again:' + e.id, write: '1' }, 'aria-label': `Queue ${t.name} again`,
            onclick: () => openPicker({ id: e.id, system: e.system, title: e.name || e.title, locked: false, entry: null }),
          }, 'Queue again') : null,
          h('button', { type: 'button', class: 'btn sm', dataset: { fk: 'q-hrm:' + e.id, write: '1' }, 'aria-label': `Remove ${t.name} from the history`, title: 'Forget it: removes it from the history', onclick: () => endEntry(e, { kind: 'remove' }) }, 'Forget')));
    }

    async function showEveryEnded() {
      histAll = true;
      S.queueHistory = Math.min(10000, S.queue.history_total);
      await store.loadQueue();
    }

    function paint() {
      const q = S.queue;
      const fk = fkOf();
      if (!q) { node.hidden = true; return; }
      if (q.state === 'damaged' || q.state === 'newer') {
        node.hidden = false;
        note.textContent = '';
        body.replaceChildren(h('p', { class: 'note warn', role: 'status' },
          `${q.message || 'queue.json cannot be read'}. The queue is not shown and cannot be changed until the file is fixed (restore one of the files in catalog/queue-backups/ on the hub).`));
        return;
      }
      const entries = q.entries || [];
      const history = q.history || [];
      if (q.state === 'absent' && !entries.length && !history.length) { node.hidden = true; return; }
      node.hidden = false;
      note.textContent = plural(entries.length, 'game');
      const shown = showAll ? entries : entries.slice(0, HOME_ROWS);
      const parts = [];
      if (q.limit > 0 && q.size > 0.9 * q.limit) {
        parts.push(h('p', { class: 'note warn', role: 'status' },
          `The queue file is nearly full (${formatBytes(q.size)} of ${formatBytes(q.limit)}). To make room, press Forget on entries of the history below.`));
      }
      parts.push(entries.length
        ? h('ul', { class: 'q-list' }, shown.map((e, i) => row(e, i, entries.length)))
        : h('p', { class: 'mute q-empty' }, 'Nothing queued. Use "Add to queue" on a game page.'));
      if (entries.length > HOME_ROWS) {
        parts.push(h('button', {
          type: 'button', class: 'btn sm q-more', dataset: { fk: 'q-showall' },
          onclick: () => { showAll = !showAll; paint(); refocus('q-showall'); },
        }, showAll ? 'Show fewer' : `Show all ${entries.length}`));
      }
      if (q.history_total > 0) {
        const list = histAll ? history : history.slice(0, HISTORY_ROWS);
        const more = q.history_total > list.length;
        const details = h('details', { class: 'q-hist', open: histOpen || null, ontoggle: (ev) => { histOpen = ev.target.open; } },
          h('summary', { dataset: { fk: 'q-hist' } }, `Recently finished (${q.history_total.toLocaleString('en-US')})`),
          h('ul', { class: 'q-hist-list' }, list.map(histRow)),
          more ? h('button', {
            type: 'button', class: 'btn sm q-more', dataset: { fk: 'q-histall' },
            onclick: async () => { await showEveryEnded(); },
          }, `Show all ${q.history_total.toLocaleString('en-US')}`) : null);
        parts.push(details);
      }
      body.replaceChildren(...parts);
      ui.paintProgress();
      refocus(fk);
    }

    return { node, paint };
  }

  // ===== the game page =====================================================================

  function gamePanel(d, ref) {
    const q = S.queue;
    const head = (extra, ...kids) => h('section', { class: 'panel q-panel', 'aria-label': 'Play queue' },
      h('h2', null, h('i'), 'Play queue', h('span', null, extra || '')), h('div', { class: 'pbody q-pbody' }, ...kids));
    if (!q) {
      return head('', S.queueError
        ? [h('p', { class: 'mute' }, 'The queue could not be loaded.'),
          h('button', { type: 'button', class: 'btn sm', dataset: { fk: 'q-retry' }, onclick: () => store.loadQueue() }, 'Try again')]
        : h('p', { class: 'mute' }, 'Loading...'));
    }
    if (q.state === 'damaged' || q.state === 'newer') {
      return head('Unavailable', h('p', { class: 'note warn' }, `${q.message || 'queue.json cannot be read'}. The queue cannot be changed until the file is fixed.`));
    }
    const entry = S.queueById.get(d.id);
    const ended = entry ? null : S.queueEnded.get(d.id);
    const pickerFor = (e) => () => openPicker({ id: d.id, system: ref.system, title: ref.title, locked: ref.locked, entry: e });

    if (entry) {
      const at = q.entries.findIndex((e) => e.id === entry.id);
      const lines = claimLines(entry, { takeOff: true });
      const free = S.devices.some((c) => !ref.locked && !store.excludes(c.name, ref.system) && store.isManaged(c.name) &&
        !(entry.devices[c.name] && entry.devices[c.name].end == null));
      const stored = entry.state;
      const act = (fk, cls, text, fn, label) => h('button', { type: 'button', class: `btn sm ${cls || ''}`, dataset: { fk, write: '1' }, 'aria-label': label || null, onclick: fn }, text);
      return head(`${at + 1} of ${q.entries.length}`,
        h('p', { class: 'q-line' }, stateMark(entry), entry.inferred ? h('small', { class: 'mute' }, ' You played it since you queued it.') : null),
        suggestionBox(entry),
        lines.length ? h('ul', { class: 'q-devs' }, lines) : h('p', { class: 'mute small' }, 'Not queued for a console.'),
        free ? h('p', null, act('q-addc', '', 'Add a console', pickerFor(entry))) : null,
        h('div', { class: 'q-actions q-pactions' },
          stored === 'playing' ? act('q-back', '', 'Back to queued', () => setState(entry, 'queued')) : act('q-play', '', 'Playing', () => setState(entry, 'playing'), `Mark ${ref.title} as playing`),
          act('q-fin', '', 'Finished', () => endEntry(entry, { kind: 'state', state: 'finished' }), `Mark ${ref.title} as finished`),
          act('q-drop', '', 'Dropped', () => endEntry(entry, { kind: 'state', state: 'dropped' }), `Drop ${ref.title}`),
          act('q-rm', 'danger', 'Remove from queue', () => endEntry(entry, { kind: 'remove' }))));
    }
    if (ended) {
      return head('',
        h('p', { class: 'q-line' }, h('span', { class: 'q-state ' + ended.state }, ended.state === 'finished' ? 'Finished' : 'Dropped'),
          h('small', { class: 'mute', title: formatMoment(ended.ended) }, ` ${timeAgo(ended.ended)}`)),
        h('p', { class: 'mute small' }, 'It stays in your history. Queue it again to play it once more.'),
        h('div', { class: 'q-actions q-pactions' },
          h('button', { type: 'button', class: 'btn sm primary', dataset: { fk: 'q-again', write: '1' }, onclick: pickerFor(null) }, 'Queue again')));
    }
    return head('',
      h('p', { class: 'mute small' }, 'Plan to play it next. Queueing it for a console turns it on there, so it is downloaded before you pick the console up.'),
      h('div', { class: 'q-actions q-pactions' },
        h('button', { type: 'button', class: 'btn sm primary', dataset: { fk: 'q-add', write: '1' }, onclick: pickerFor(null) }, 'Add to queue')));
  }

  // The dialog closes on a click on its backdrop, like the others.
  const dlg = $('#queue-dialog');
  if (dlg) dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });

  W.queueUi = { homeSection, gamePanel, openPicker, claimPlan, endOp };
})();
