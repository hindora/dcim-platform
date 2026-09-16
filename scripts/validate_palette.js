#!/usr/bin/env node
/* Checks the multi-series line ramps the charts draw with.
 *
 * The chart rulebook has always said these ramps are "checked with
 * scripts/validate_palette.js, never by eye". The file did not exist, and the
 * ramp it claimed to guarantee shipped `#3b82f6` blue in slot 1 and `#a855f7`
 * purple in slot 3 - which a deuteranope sees as #7373f6 and #7575f6, a
 * CIEDE2000 distance of 0.6. That is not two colours. Every three-series chart
 * in the product drew those two lines side by side.
 *
 * So: the check exists now, and CI runs it. It reads the --series-N tokens
 * straight out of index.css, so it cannot drift from what the browser paints.
 *
 * What it enforces, per theme:
 *   - every pair among the first FOUR slots stays apart under normal vision
 *     and under each of the three dichromacies. Four, because that is the most
 *     series any chart here draws; beyond it a looser bar applies, since slots
 *     5 and 6 rarely appear together with 1 and 2.
 *   - every colour keeps 3:1 against the panel it is drawn on, the WCAG
 *     non-text contrast minimum.
 *
 * Colour alone is never the whole answer, which is why the charts also dash
 * the third series onward - see DASHES in components/seriesColors.ts. A ramp
 * that passes this file is a ramp whose hues are a real second channel, not
 * the only one.
 *
 * Usage: node scripts/validate_palette.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const CSS = path.join(__dirname, '..', 'frontend', 'src', 'index.css');

/** How far apart, in CIEDE2000, two lines must stay. ~2 is "a trained eye can
 *  see it side by side"; 10 is about where two strokes crossing a chart stop
 *  reading as one colour at a glance. The dichromat bars are lower than the
 *  normal-vision one because the whole gamut collapses under dichromacy - what
 *  matters is that the pair does not collapse WITH it. */
const LIMITS = {
  lead: { normal: 15, deutan: 12, protan: 12, tritan: 8 },
  rest: { normal: 8, deutan: 6, protan: 6, tritan: 4 },
};
/** How many slots a chart in this product actually draws at once. */
const LEAD = 4;
const MIN_CONTRAST = 3.0;

// ----------------------------------------------------------------- colour
const hexRgb = (h) => {
  const s = h.replace('#', '');
  return [0, 2, 4].map((i) => parseInt(s.slice(i, i + 2), 16));
};
const lin = (c) => {
  const x = c / 255;
  return x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4;
};
const relLum = (h) => {
  const [r, g, b] = hexRgb(h).map(lin);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const contrast = (a, b) => {
  const [hi, lo] = [relLum(a), relLum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};

function lab(hex) {
  const [r, g, b] = hexRgb(hex).map(lin);
  const x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047;
  const y = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  const z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883;
  const f = (t) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
  const [fx, fy, fz] = [f(x), f(y), f(z)];
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}

/** CIEDE2000. Long, but it is the only distance that agrees with the eye about
 *  blues, which is exactly the region this ramp lives in. */
function de00(c1, c2) {
  const [L1, a1, b1] = c1;
  const [L2, a2, b2] = c2;
  const rad = Math.PI / 180;
  const C1 = Math.hypot(a1, b1);
  const C2 = Math.hypot(a2, b2);
  const Cb = (C1 + C2) / 2;
  const G = 0.5 * (1 - Math.sqrt(Cb ** 7 / (Cb ** 7 + 25 ** 7 || 1)));
  const a1p = (1 + G) * a1;
  const a2p = (1 + G) * a2;
  const C1p = Math.hypot(a1p, b1);
  const C2p = Math.hypot(a2p, b2);
  const h1p = (Math.atan2(b1, a1p) / rad + 360) % 360;
  const h2p = (Math.atan2(b2, a2p) / rad + 360) % 360;
  const dLp = L2 - L1;
  const dCp = C2p - C1p;
  let dhp = 0;
  if (C1p * C2p !== 0) {
    dhp = h2p - h1p;
    if (dhp > 180) dhp -= 360;
    else if (dhp < -180) dhp += 360;
  }
  const dHp = 2 * Math.sqrt(C1p * C2p) * Math.sin((dhp / 2) * rad);
  const Lbp = (L1 + L2) / 2;
  const Cbp = (C1p + C2p) / 2;
  let hbp;
  if (C1p * C2p === 0) hbp = h1p + h2p;
  else if (Math.abs(h1p - h2p) > 180) hbp = (h1p + h2p + (h1p + h2p < 360 ? 360 : -360)) / 2;
  else hbp = (h1p + h2p) / 2;
  const T = 1 - 0.17 * Math.cos((hbp - 30) * rad) + 0.24 * Math.cos(2 * hbp * rad)
    + 0.32 * Math.cos((3 * hbp + 6) * rad) - 0.2 * Math.cos((4 * hbp - 63) * rad);
  const dTh = 30 * Math.exp(-(((hbp - 275) / 25) ** 2));
  const Rc = 2 * Math.sqrt(Cbp ** 7 / (Cbp ** 7 + 25 ** 7 || 1));
  const Sl = 1 + (0.015 * (Lbp - 50) ** 2) / Math.sqrt(20 + (Lbp - 50) ** 2);
  const Sc = 1 + 0.045 * Cbp;
  const Sh = 1 + 0.015 * Cbp * T;
  const Rt = -Math.sin(2 * dTh * rad) * Rc;
  return Math.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2
    + Rt * (dCp / Sc) * (dHp / Sh));
}

/** Viénot-Brettel-Mollon dichromat simulation. Not a claim about what anybody
 *  sees - it is the standard model, and it is enough to catch a pair that
 *  lands on the same confusion line. */
function cvd(hex, kind) {
  const [r, g, b] = hexRgb(hex).map(lin);
  let L = 17.8824 * r + 43.5161 * g + 4.1194 * b;
  let M = 3.4557 * r + 27.1554 * g + 3.8671 * b;
  let S = 0.02996 * r + 0.18431 * g + 1.46709 * b;
  if (kind === 'protan') L = 2.02344 * M - 2.52581 * S;
  else if (kind === 'deutan') M = 0.49421 * L + 1.24827 * S;
  else S = -0.395913 * L + 0.801109 * M;
  const out = [
    0.080944 * L - 0.130504 * M + 0.116721 * S,
    -0.0102485 * L + 0.0540194 * M - 0.113615 * S,
    -0.000365 * L - 0.00412 * M + 0.693513 * S,
  ].map((c) => {
    const v = Math.min(1, Math.max(0, c));
    const enc = v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055;
    return Math.round(enc * 255).toString(16).padStart(2, '0');
  });
  return `#${out.join('')}`;
}

const seen = (hex, kind) => (kind === 'normal' ? hex : cvd(hex, kind));

// ------------------------------------------------------------------ input
/** The tokens, read out of the stylesheet itself. A validator with its own
 *  copy of the values checks a palette nobody ships. */
function readThemes(css) {
  const blocks = [
    { name: 'dark', re: /:root\s*\{([\s\S]*?)\}/ },
    { name: 'light', re: /:root\[data-theme="light"\]\s*\{([\s\S]*?)\}/ },
  ];
  return blocks.map(({ name, re }) => {
    const body = (css.match(re) || [])[1];
    if (!body) throw new Error(`no ${name} token block in index.css`);
    const ramp = [];
    for (let i = 1; i <= 8; i += 1) {
      const m = body.match(new RegExp(`--series-${i}:\\s*(#[0-9a-fA-F]{6})`));
      if (m) ramp.push({ slot: i, hex: m[1].toLowerCase() });
    }
    const bg = (body.match(/--bg-raised:\s*(#[0-9a-fA-F]{6})/) || [])[1];
    return { name, ramp, bg };
  });
}

// ------------------------------------------------------------------ check
function main() {
  const css = fs.readFileSync(CSS, 'utf8');
  const themes = readThemes(css);
  const failures = [];
  let pairs = 0;

  for (const { name, ramp, bg } of themes) {
    if (ramp.length < 4) {
      failures.push(`${name}: only ${ramp.length} --series-N tokens; want at least 4`);
      continue;
    }
    console.log(`\n${name} theme, on ${bg}`);
    for (const { slot, hex } of ramp) {
      const c = contrast(hex, bg);
      const flag = c < MIN_CONTRAST ? '  FAIL' : '';
      console.log(`  --series-${slot}  ${hex}  luminance ${relLum(hex).toFixed(3)}`
        + `  contrast ${c.toFixed(1)}:1${flag}`);
      if (c < MIN_CONTRAST) {
        failures.push(`${name} --series-${slot} ${hex}: contrast ${c.toFixed(2)}:1 `
          + `on ${bg}, want ${MIN_CONTRAST}:1`);
      }
    }
    for (const kind of ['normal', 'deutan', 'protan', 'tritan']) {
      let worst = null;
      for (let i = 0; i < ramp.length; i += 1) {
        for (let j = i + 1; j < ramp.length; j += 1) {
          const leading = ramp[i].slot <= LEAD && ramp[j].slot <= LEAD;
          const limit = LIMITS[leading ? 'lead' : 'rest'][kind];
          const d = de00(lab(seen(ramp[i].hex, kind)), lab(seen(ramp[j].hex, kind)));
          pairs += 1;
          if (!worst || d < worst.d) worst = { d, i: ramp[i].slot, j: ramp[j].slot, leading };
          if (d < limit) {
            failures.push(`${name} ${kind}: --series-${ramp[i].slot} and `
              + `--series-${ramp[j].slot} are ${d.toFixed(1)} apart `
              + `(${seen(ramp[i].hex, kind)} vs ${seen(ramp[j].hex, kind)}), want ${limit}`);
          }
        }
      }
      console.log(`  ${kind.padEnd(7)} closest pair  ${worst.d.toFixed(1)}`
        + `  (--series-${worst.i} / --series-${worst.j})`);
    }
  }

  console.log(`\n${pairs} comparisons`);
  if (failures.length) {
    console.error('\nFAILED:');
    for (const f of failures) console.error(`  - ${f}`);
    process.exit(1);
  }
  console.log('palette ok');
}

main();
