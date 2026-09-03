import { useQuery } from '@tanstack/react-query';
import { api, type AssetCharts } from '../../api/client';
import { humanise } from '../../lib/format';
import { BarChart, type BarRow } from './components/BarChart';

/** Composition and capacity of the estate.
 *
 *  Every chart here is a COUNT, not a trend, and that is a statement about the
 *  data rather than a preference. Nothing in this schema records history yet:
 *  lifecycle events only accrue from the day they start being written, and
 *  there is no capacity snapshot. A line drawn through one point would say
 *  something it cannot know.
 *
 *  No pie charts. Every ratio here is one part against a whole, and a pie asks
 *  the reader to compare arc lengths to recover a number the bar states
 *  outright. Bars share a baseline, which is the only reason they are easier
 *  to read.
 */

/** The lifecycle hues, matching the chips in the inventory table so the chart
 *  and the rows speak one vocabulary. `installed` is deliberately not the same
 *  green as `in_service`: the whole reason that state exists is that it is
 *  racked and must NOT alarm, and painting it as live hides exactly that. */
const LIFECYCLE_HUE: Record<string, string> = {
  planned: 'var(--accent-dim)',
  in_stock: 'var(--text-faint)',
  installed: 'var(--accent)',
  in_service: 'var(--ok)',
  maintenance: 'var(--warn)',
  decommissioned: 'var(--unknown)',
  retired: 'var(--text-faint)',
};

export function Charts() {
  const { data, isLoading, error } = useQuery<AssetCharts>({
    queryKey: ['asset-charts'],
    queryFn: api.assetCharts,
    refetchInterval: 60_000,
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (isLoading || !data) {
    return (
      <div className="asset-cols" style={{ marginTop: 20 }}>
        {[0, 1, 2, 3].map((i) => (
          <div className="asset-panel" key={i}>
            <div className="asset-skeleton" style={{ width: '40%' }} />
            <div className="asset-skeleton" style={{ height: 90, marginTop: 12 }} />
          </div>
        ))}
      </div>
    );
  }

  const { rack_space: space, floor_space: floor } = data;

  return (
    <>
      <h3 className="asset-charts-head">Composition</h3>
      <div className="asset-cols">
        <Panel title="By type" total={sum(data.by_type)}>
          <BarChart
            rows={data.by_type.map((t): BarRow => ({
              label: t.label,
              n: t.n,
              href: `/assets/inventory?device_type=${encodeURIComponent(t.key)}`,
            }))}
          />
        </Panel>

        <Panel title="By make" total={sum(data.by_vendor)}>
          <BarChart rows={data.by_vendor.map((v): BarRow => ({
            label: v.label, n: v.n,
          }))} />
        </Panel>

        <Panel title="By lifecycle" total={sum(data.by_lifecycle)}>
          {/* The one chart where a colour per bar earns itself: here the hue IS
              the state, and it is the same hue the chips in the table use, so
              the two read as one vocabulary. Empty states are kept - "nothing
              is decommissioned" is worth seeing, and a chart whose rows come
              and go cannot be compared with itself week to week. */}
          <BarChart
            limitable={false}
            rows={data.by_lifecycle.map((l): BarRow => ({
              label: humanise(l.key),
              n: l.n,
              colour: LIFECYCLE_HUE[l.key],
              href: `/assets/inventory?lifecycle=${encodeURIComponent(l.key)}`,
            }))}
          />
        </Panel>
      </div>

      <h3 className="asset-charts-head">Capacity</h3>
      <div className="asset-cols">
        <Panel title="Cabinet space">
          <Fill
            parts={[
              { label: 'Installed', n: space.u_used, tone: 'used' },
              { label: 'Held for planned', n: space.u_held, tone: 'held' },
              { label: 'Free', n: space.u_free, tone: 'free' },
            ]}
            total={space.u_total}
            unit="U"
            note={`${space.racks} racks`}
          />
        </Panel>

        <Panel title="Floor space">
          <Fill
            parts={[
              { label: 'Racks installed', n: floor.installed, tone: 'used' },
              { label: 'Positions free', n: floor.free, tone: 'free' },
            ]}
            total={floor.designed}
            unit=""
            note={`${floor.rooms} rooms · ${Math.round(floor.area_m2).toLocaleString()} m²`}
          />
        </Panel>

        <Panel title="How full the racks are">
          {/* The distribution behind the single "free U" figure. A mean hides
              whether the space is one empty cabinet or forty part-used ones,
              and only the first of those takes a full-height install. */}
          <Histogram rows={data.rack_fill} />
        </Panel>
      </div>
    </>
  );
}

function Panel({ title, total, children }: {
  title: string; total?: number; children: React.ReactNode;
}) {
  return (
    <div className="asset-panel">
      <h3>
        {title}
        {total != null && <span className="asset-panel-total">{total.toLocaleString()}</span>}
      </h3>
      {children}
    </div>
  );
}

function Histogram({ rows }: { rows: { band: string; n: number }[] }) {
  const max = Math.max(1, ...rows.map((r) => r.n));
  return (
    <div className="asset-histogram">
      {rows.map((r) => (
        <div className="asset-histogram-col" key={r.band}>
          <div className="v">{r.n || ''}</div>
          <div
            className={`asset-histogram-bar${toneOf(r.band)}`}
            style={{ height: `${(r.n / max) * 100}%` }}
            title={`${r.n} racks ${r.band.toLowerCase()}`}
          />
          <div className="k">{r.band}</div>
        </div>
      ))}
    </div>
  );
}

/** The two ends carry meaning the middle does not: an empty cabinet is where
 *  the next install goes, and one over 90% cannot take another full-height
 *  machine whatever the total free U says. */
function toneOf(band: string): string {
  if (band === 'Empty') return ' is-empty';
  if (band === 'Over 90%') return ' is-full';
  return '';
}

function Fill({ parts, total, unit, note }: {
  parts: { label: string; n: number; tone: string }[];
  total: number;
  unit: string;
  note?: string;
}) {
  const denominator = Math.max(1, total);
  return (
    <>
      <div className="asset-stack">
        {parts.filter((p) => p.n > 0).map((p) => (
          <span
            key={p.label}
            className={`is-${p.tone}`}
            style={{ width: `${(p.n / denominator) * 100}%` }}
            title={`${p.label}: ${p.n}${unit}`}
          />
        ))}
      </div>
      <div className="asset-legend">
        {parts.map((p) => (
          <span key={p.label}>
            <span className={`sw is-${p.tone}`} />
            {p.label} {p.n.toLocaleString()}{unit}
          </span>
        ))}
        {note && <span className="muted">{note}</span>}
      </div>
    </>
  );
}

function sum(rows: { n: number }[]): number {
  return rows.reduce((t, r) => t + r.n, 0);
}

