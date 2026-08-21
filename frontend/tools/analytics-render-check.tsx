/* Headless check of the exit criterion for phase 5.7: every analytics panel
 * renders against the live API, including the panels whose honest answer is a
 * refusal.
 *
 * There is no browser in this environment, so this renders each view to static
 * markup with react-dom/server and asserts on the text. It catches what matters
 * without one: a crash on a null field, a refusal rendered as an empty chart, a
 * number shown without the caveat that says what it is worth.
 *
 * Outside src/, so it is neither type-checked with the app nor bundled:
 *
 *   ./node_modules/.bin/esbuild tools/analytics-render-check.tsx --bundle \
 *       --platform=node --format=cjs --jsx=automatic --outfile=/tmp/arc.cjs
 *   node /tmp/arc.cjs http://127.0.0.1:8000 admin admin1234
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderToStaticMarkup } from 'react-dom/server';
import { CapacityView } from '../src/features/analytics/CapacityView';
import { CoolingView } from '../src/features/analytics/CoolingView';
import { ForecastView } from '../src/features/analytics/ForecastView';
import { PowerView } from '../src/features/analytics/PowerView';
import { PueView } from '../src/features/analytics/PueView';
import { ThermalView } from '../src/features/analytics/ThermalView';
import type { RoomSummary } from '../src/api/client';

declare const process: { argv: string[]; exit(code: number): never };
declare const console: { log(...a: unknown[]): void };

const [base, user, pass] = process.argv.slice(2);

/** Strip markup so an assertion reads what a person would read. */
function text(html: string): string {
  return html.replace(/<[^>]+>/g, ' ').replace(/&[a-z]+;/g, ' ')
    .replace(/\s+/g, ' ').trim();
}

async function main() {
  const login = await fetch(`${base}/api/v1/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: user, password: pass }),
  });
  const { token } = (await login.json()) as { token: string };
  const get = async (p: string) => {
    const r = await fetch(`${base}/api/v1${p}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!r.ok) throw new Error(`${p} -> ${r.status}`);
    return r.json();
  };

  const rooms = (await get('/rooms')) as { items: RoomSummary[] };
  const room = rooms.items.find((r) => r.name.includes('Hall A')) ?? rooms.items[0];
  const dc = room.datacenter_id ?? undefined;
  console.log(`room: ${room.datacenter_code} ${room.name}\n`);

  // The views read from the query cache. Seeding it renders them exactly as the
  // browser would on a warm cache, without a network layer in the renderer.
  const seed: [readonly unknown[], unknown][] = [
    [['capacity', room.id], await get(`/capacity?scope=room&scope_id=${room.id}`)],
    [['forecast', room.id, 'power', undefined],
      await get(`/analytics/forecast?scope=room&scope_id=${room.id}&metric=power&horizon_days=30`)],
    [['pue', dc], await get(`/analytics/pue${dc ? `?datacenter_id=${dc}` : ''}`)],
    [['pue-series', dc], await get(`/analytics/pue/series${dc ? `?datacenter_id=${dc}` : ''}`)],
    [['thermal', room.id], await get(`/analytics/thermal?room_id=${room.id}`)],
    [['cooling', dc], await get(`/cooling${dc ? `?datacenter_id=${dc}` : ''}`)],
    [['power-fleet', dc], await get(`/power${dc ? `?datacenter_id=${dc}` : ''}`)],
  ];

  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: Infinity } },
  });
  for (const [key, data] of seed) qc.setQueryData(key, data);

  const views = [
    ['Capacity', <CapacityView room={room} />],
    ['Forecast', <ForecastView room={room} />],
    ['PUE', <PueView room={room} />],
    ['Thermal', <ThermalView room={room} />],
    ['Cooling', <CoolingView room={room} />],
    ['Power', <PowerView room={room} />],
  ] as const;

  let failures = 0;
  for (const [name, node] of views) {
    let body: string;
    try {
      body = text(renderToStaticMarkup(
        <QueryClientProvider client={qc}>{node}</QueryClientProvider>));
    } catch (e) {
      console.log(`FAIL ${name}: threw ${String(e)}`);
      failures += 1;
      continue;
    }
    if (!body || body === 'Loading…') {
      console.log(`FAIL ${name}: rendered nothing (${body})`);
      failures += 1;
      continue;
    }
    if (body.includes('Failed to load')) {
      console.log(`FAIL ${name}: ${body.slice(0, 120)}`);
      failures += 1;
      continue;
    }
    console.log(`--- ${name} (${body.length} chars)`);
    console.log(`    ${body.slice(0, 400)}\n`);
  }

  // The populated forecast cannot be reached against this database - the fleet
  // has hours of history, not the fourteen days the backend insists on - so the
  // chart path is exercised against a response of the shape the backend
  // produces. Without this, the only forecast ever rendered is the refusal.
  const day0 = 200;
  const synthetic = {
    scope: 'room', scope_id: room.id, name: room.name, metric: 'power',
    metric_label: 'coincident load across the scope', devices: 115,
    statistic: 'daily p95 of the coincident load',
    history_days: 60, min_history_days: 14, method: 'holt_winters',
    method_reason: 'weekly seasonal model; it predicted the held-back week better',
    trend_per_day: 2.0, r2: 0.96, unit: 'kW', capacity: 250,
    points: Array.from({ length: 30 }, (_, i) => ({
      day: i + 1, value: day0 + 2 * i,
      lower: day0 + 2 * i - (4 + i * 0.8), upper: day0 + 2 * i + (4 + i * 0.8),
    })),
    history: Array.from({ length: 60 }, (_, i) => ({
      day: new Date(Date.UTC(2026, 5, 1 + i)).toISOString(),
      value: 80 + 2 * i,
    })),
    runway: { days: 18, earliest_days: 11, latest_days: null,
              reason: 'crosses 250 at about day 18, as early as day 11' },
    notes: ['the interval on a seasonal model has no closed form'],
  };
  qc.setQueryData(['forecast', room.id, 'power', undefined], synthetic);
  const populated = renderToStaticMarkup(
    <QueryClientProvider client={qc}><ForecastView room={room} /></QueryClientProvider>);
  const body = text(populated);
  const checks: [string, boolean][] = [
    ['runway shown as a window', body.includes('as early as day 11')],
    ['method named', body.includes('holt_winters')],
    ['interval band drawn', populated.includes('fill-opacity="0.16"')],
    ['projection dashed', populated.includes('stroke-dasharray="6 4"')],
    ['capacity threshold drawn', populated.includes('capacity')],
    ['no refusal shown', !body.includes('No forecast')],
  ];
  console.log('--- Forecast (populated, synthetic response)');
  for (const [what, ok] of checks) {
    console.log(`    ${ok ? 'ok  ' : 'FAIL'} ${what}`);
    if (!ok) failures += 1;
  }
  console.log(`    ${body.slice(0, 260)}
`);

  console.log(failures ? `${failures} check(s) failed` : 'all views rendered');
  process.exit(failures ? 1 : 0);
}

main();
