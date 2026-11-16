/*
 * warpzone library UI: the small things every view shares.
 *
 * A DOM builder, the pixel icons, how systems and consoles are named, abbreviated and coloured,
 * and the title helpers. Nothing here talks to the server or keeps state.
 *
 * The page is served with Content-Security-Policy: default-src 'self', so there is no inline
 * style or script and no data: URL. Dynamic styling goes through custom properties and single
 * properties set with element.style, which the policy allows. Nodes are built, never parsed
 * from strings, so catalog and scrape text cannot inject markup.
 */
(function () {
  'use strict';

  const SVGNS = 'http://www.w3.org/2000/svg';

  // Build an element: h('div', {class: 'x', onclick: fn, vars: {c: '#f0f'}}, child, ...).
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

  // ---- pixel icons ---------------------------------------------------------------------------
  // ASCII bitmaps drawn as one SVG path of rectangles: crisp at any size, tinted by currentColor.

  const BITMAPS = {
    trophy: ['#########', '#.#####.#', '#.#####.#', '.#.###.#.', '..#####..', '...###...', '....#....', '...###...', '..#####..'],
    eject: ['....#....', '...###...', '..#####..', '.#######.', '#########', '.........', '#########', '#########'],
    insert: ['.#######.', '.#.....#.', '.#.###.#.', '.#.###.#.', '.#.###.#.', '.#.....#.', '.#######.', '.#######.', '.#.#.#.#.'],
    search: ['.#####...', '#.....#..', '#.....#..', '#.....#..', '#.....#..', '#.....#..', '.#####.#.', '.......##', '........#'],
    home: ['....#....', '...###...', '..#####..', '.#######.', '##.###.##', '##.###.##', '##.###.##', '##.###.##'],
    pad: ['.#######.', '#.......#', '#.#...#.#', '###.#.###', '#.#...#.#', '.#######.'],
    grid: ['##.##.##', '##.##.##', '........', '##.##.##', '##.##.##', '........', '##.##.##', '##.##.##'],
    left: ['...#', '..##', '.##.', '##..', '.##.', '..##', '...#'],
    right: ['#...', '##..', '.##.', '..##', '.##.', '##..', '#...'],
    play: ['#....', '##...', '###..', '####.', '#####', '####.', '###..', '##...', '#....'],
    pause: ['###.###', '###.###', '###.###', '###.###', '###.###', '###.###', '###.###'],
    star: ['....#....', '...###...', '...###...', '#########', '.#######.', '..#####..', '..#####..', '.###.###.', '.##...##.'],
    check: ['.......#', '......##', '#....##.', '##..##..', '.####...', '..##....'],
    disc: ['..#####..', '.#######.', '###...###', '###.#.###', '###...###', '.#######.', '..#####..'],
  };
  const pathCache = new Map();

  function bitmapPath(name) {
    if (pathCache.has(name)) return pathCache.get(name);
    let d = '';
    BITMAPS[name].forEach((row, y) => {
      let x = 0;
      while (x < row.length) {
        if (row[x] !== '#') { x++; continue; }
        let w = 0;
        while (row[x + w] === '#') w++;
        d += `M${x} ${y}h${w}v1h-${w}z`;
        x += w;
      }
    });
    pathCache.set(name, d);
    return d;
  }

  function icon(name, cls) {
    const rows = BITMAPS[name];
    const svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('class', 'px' + (cls ? ' ' + cls : ''));
    svg.setAttribute('viewBox', `0 0 ${rows[0].length} ${rows.length}`);
    svg.setAttribute('shape-rendering', 'crispEdges');
    svg.setAttribute('fill', 'currentColor');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(SVGNS, 'path');
    path.setAttribute('d', bitmapPath(name));
    svg.append(path);
    return svg;
  }

  // ---- systems -------------------------------------------------------------------------------

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
    atari7800: 'Atari 7800', lynx: 'Atari Lynx', atarilynx: 'Atari Lynx',
    pokemini: 'Pokémon Mini', c64: 'Commodore 64', ngp: 'Neo Geo Pocket', model2: 'Sega Model 2',
    atarijaguar: 'Atari Jaguar', wonderswan: 'WonderSwan', wonderswancolor: 'WonderSwan Color',
  };

  // The maker and first release year of each known system: the side rail and the Systems page
  // group the systems by maker in MAKERS order, and by year within a maker (then by name). A
  // system missing here goes to "Other", by name; arcade comes last.
  const MAKERS = ['Nintendo', 'Sony', 'Sega', 'Microsoft', 'Atari', 'NEC', 'SNK', 'Bandai', 'Commodore', 'Other', 'Arcade'];
  const SYSTEM_MAKERS = {
    gameandwatch: ['Nintendo', 1980], nes: ['Nintendo', 1983], famicom: ['Nintendo', 1983],
    fds: ['Nintendo', 1986], gb: ['Nintendo', 1989], snes: ['Nintendo', 1990], sfc: ['Nintendo', 1990],
    sgb: ['Nintendo', 1994], virtualboy: ['Nintendo', 1995], satellaview: ['Nintendo', 1995],
    n64: ['Nintendo', 1996], gbc: ['Nintendo', 1998], n64dd: ['Nintendo', 1999],
    pokemini: ['Nintendo', 2001], gba: ['Nintendo', 2001], gc: ['Nintendo', 2001],
    nds: ['Nintendo', 2004], wii: ['Nintendo', 2006], n3ds: ['Nintendo', 2011], '3ds': ['Nintendo', 2011],
    wiiu: ['Nintendo', 2012], switch: ['Nintendo', 2017],
    psx: ['Sony', 1994], ps2: ['Sony', 2000], psp: ['Sony', 2004], ps3: ['Sony', 2006],
    psvita: ['Sony', 2011], ps4: ['Sony', 2013],
    'sg-1000': ['Sega', 1983], mastersystem: ['Sega', 1985], genesis: ['Sega', 1988],
    megadrive: ['Sega', 1988], gamegear: ['Sega', 1990], segacd: ['Sega', 1991], megacd: ['Sega', 1991],
    model2: ['Sega', 1993], sega32x: ['Sega', 1994], saturn: ['Sega', 1994], model3: ['Sega', 1996],
    dreamcast: ['Sega', 1998], naomi: ['Sega', 1998],
    xbox: ['Microsoft', 2001], xbox360: ['Microsoft', 2005],
    atari2600: ['Atari', 1977], atari800: ['Atari', 1979], atari5200: ['Atari', 1982],
    atarist: ['Atari', 1985], atari7800: ['Atari', 1986], lynx: ['Atari', 1989],
    atarilynx: ['Atari', 1989], atarijaguar: ['Atari', 1993], atarijaguarcd: ['Atari', 1995],
    pcengine: ['NEC', 1987], tg16: ['NEC', 1987], pcenginecd: ['NEC', 1988], 'tg-cd': ['NEC', 1988],
    supergrafx: ['NEC', 1989], pcfx: ['NEC', 1994],
    neogeo: ['SNK', 1990], neogeocd: ['SNK', 1994], ngp: ['SNK', 1998], ngpc: ['SNK', 1999],
    wonderswan: ['Bandai', 1999], wonderswancolor: ['Bandai', 2000],
    vic20: ['Commodore', 1980], c64: ['Commodore', 1982], c16: ['Commodore', 1984],
    amiga: ['Commodore', 1985], cdtv: ['Commodore', 1991], amiga600: ['Commodore', 1992],
    amiga1200: ['Commodore', 1992], amigacd32: ['Commodore', 1993],
    arcade: ['Arcade'], mame: ['Arcade'], fbneo: ['Arcade'], fba: ['Arcade'], cps1: ['Arcade'],
    cps2: ['Arcade'], cps3: ['Arcade'], daphne: ['Arcade'],
  };

  function systemMaker(id) {
    return (SYSTEM_MAKERS[id] || ['Other'])[0];
  }

  // Sort order for system folder names: maker, then release year (unknown last), then name.
  function compareSystems(a, b) {
    const [ma, ya] = SYSTEM_MAKERS[a] || ['Other'];
    const [mb, yb] = SYSTEM_MAKERS[b] || ['Other'];
    return MAKERS.indexOf(ma) - MAKERS.indexOf(mb)
      || (ya || 9999) - (yb || 9999)
      || systemName(a).localeCompare(systemName(b));
  }

  function systemName(id) {
    if (SYSTEM_NAMES[id]) return SYSTEM_NAMES[id];
    const pretty = id.replace(/[-_]+/g, ' ');
    return pretty.length <= 4 ? pretty.toUpperCase() : pretty.replace(/\b\w/g, (c) => c.toUpperCase());
  }

  // The short badge of a system, used where the full name does not fit (the full name goes in a
  // title attribute or beside it): its folder name (`roms/<system>`), as the
  // library, ES-DE and the whitelists name it, never cut.
  function systemCode(id) {
    return id.toUpperCase();
  }

  // One neon accent per system: fixed for the common ones, stable by name for the rest.
  const PALETTE = ['#38f9ff', '#ff4fa3', '#ffd23f', '#7dff6a', '#b366ff', '#4d8dff', '#ff9f43',
    '#ff5470', '#5dff8a', '#ff7bd5', '#9cff3d', '#47e0c8'];
  const SYSTEM_COLORS = {
    snes: '#ff4fa3', gba: '#4d8dff', psx: '#ffd23f', gc: '#b366ff', gb: '#5dff8a',
    nes: '#ff5470', n64: '#7dff6a', ps2: '#4d8dff', switch: '#ff5470', psvita: '#38f9ff',
  };

  function systemColor(id) {
    if (SYSTEM_COLORS[id]) return SYSTEM_COLORS[id];
    let n = 7;
    for (let i = 0; i < id.length; i++) n = (n * 31 + id.charCodeAt(i)) >>> 0;
    return PALETTE[n % PALETTE.length];
  }

  // The custom properties that tint an element: the accent and its glow (a colour with alpha).
  function tint(color) { return { c: color, glow: color + '80' }; }

  // ---- consoles ------------------------------------------------------------------------------

  // The name of a device for the places a whole name does not fit, up to eight characters: a
  // short name stays whole ("machine", "deck") and a long hyphenated one gives its last part
  // whole ("anbernic-rg351mp" is "RG351MP"). The full name goes in a title where there is one.
  function consoleCode(name) {
    if (name.length <= 8) return name.toUpperCase();
    const parts = name.split(/[-_ ]+/).filter(Boolean);
    return (parts[parts.length - 1] || name).slice(0, 8).toUpperCase();
  }

  // ---- titles --------------------------------------------------------------------------------

  const REGION_CODES = {
    USA: 'USA', Europe: 'EUR', Japan: 'JPN', World: 'WORLD', Spain: 'ESP', France: 'FRA', Germany: 'GER',
    Italy: 'ITA', Australia: 'AUS', Brazil: 'BRA', Korea: 'KOR', China: 'CHN', Asia: 'ASA',
    Canada: 'CAN', Netherlands: 'NED', Sweden: 'SWE', Russia: 'RUS', Taiwan: 'TWN',
  };

  // The short codes of the regions the API lists (`region`: ["USA", "Europe"]), joined for a chip.
  // A region this file does not know keeps its first three letters.
  function regionCodes(list, max) {
    const codes = (Array.isArray(list) ? list : []).filter((r) => typeof r === 'string' && r)
      .map((r) => REGION_CODES[r] || r.slice(0, 3).toUpperCase());
    if (!codes.length) return '';
    const cap = max || 2;
    return codes.length > cap ? codes.slice(0, cap).join('/') + '+' : codes.join('/');
  }

  // The name of a language code (`en`, `zh-Hant`) in English, or the code itself where the
  // browser cannot say.
  let languageNames = null;
  function languageName(code) {
    try {
      if (!languageNames) languageNames = new Intl.DisplayNames(['en'], { type: 'language' });
      return languageNames.of(code) || code;
    } catch (_) { return code; }
  }

  // "Legend of Zelda, The - A Link to the Past (USA) (Rev 1)" becomes the name shown, the
  // region and the other tags: the title keeps the tags apart so two discs never look alike.
  function parseTitle(raw) {
    const text = String(raw || '');
    const tags = [];
    let region = '';
    let base = text.replace(/\s*[(\[]([^)\]]*)[)\]]/g, (_, tag) => {
      const first = tag.split(',')[0].trim();
      if (!region && REGION_CODES[first]) region = REGION_CODES[first];
      else tags.push(tag);
      return '';
    }).trim();
    base = base.replace(/^(.*?), (The|A|An)( - .*)?$/, (_, a, art, rest) => `${art} ${a}${rest || ''}`);
    return { name: base || text, region, tags, full: text };
  }

  window.WZ = Object.assign(window.WZ || {}, {
    h, $, $$, plural, debounce, throttle, icon, tint,
    systemName, systemMaker, compareSystems, systemCode, systemColor, consoleCode, parseTitle, regionCodes, languageName,
  });
})();
