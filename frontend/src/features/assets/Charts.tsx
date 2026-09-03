import { useQuery } from '@tanstack/react-query';
import { api, type AssetCharts } from '../../api/client';
import { humanise } from '../../lib/format';
import { BarChart, type BarRow } from './components/BarChart';
import { Donut, Gauge, VColumns } from './components/Shapes';

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

      <h3 className="asset-charts-head">Where it is, and whose</h3>
      <div className="asset-cols">
        <Panel title="By room" total={sum(data.by_room)}>
          <BarChart rows={data.by_room.map((r): BarRow => ({
            label: r.label, n: r.n,
          }))} />
        </Panel>

        <Panel title="By owner" total={sum(data.by_owner)}>
          <BarChart rows={data.by_owner.map((o): BarRow => ({
            label: o.label,
            n: o.n,
            href: `/assets/inventory?owner_group=${encodeURIComponent(o.label)}`,
          }))} />
        </Panel>

        <Panel title="How it is placed" total={sum(data.placement)}>
          {/* Floor-standing is NOT a gap. A chiller in a plant room is placed;
              it simply has no rack, and colouring it as a problem would report
              the estate's own design as a data fault. "Not placed" is the only
              row here that means something is missing. */}
          <BarChart
            sorted={false}
            limitable={false}
            rows={data.placement.map((p): BarRow => ({
              label: p.label,
              n: p.n,
              colour: p.label === 'Not placed' ? 'var(--warn)'
                : p.label === 'Floor-standing' ? 'var(--border-strong)' : undefined,
            }))}
          />
        </Panel>
      </div>

      <h3 className="asset-charts-head">Cover and records</h3>
      <div className="asset-cols">
        <Panel title="When cover lapses" total={sum(data.warranty_runway)}>
          {/* A SEQUENCE, so it is not sorted by size: expired first, then each
              quarter, then the rest. Sorting would destroy the only thing a
              runway is for. Colour marks the band that is already a problem;
              the rest is ordering, which left-to-right already carries. */}
          <VColumns
            rows={data.warranty_runway.map((r) => ({
              label: r.bucket.replace('Beyond 2 years', 'Later'),
              n: r.n,
              colour: r.band === 0 ? 'var(--critical)'
                : r.band === 2 ? 'var(--border-strong)' : undefined,
            }))}
          />
        </Panel>

        <Panel title="Cover state" total={coverTotal(data)}>
          {/* Colour is the state here, matching the Cover column in the table:
              expired is a fault, expiring is a warning, no cover recorded is
              an absence and deliberately not painted as either. */}
          <BarChart
            sorted={false}
            limitable={false}
            rows={coverRows(data)}
          />
        </Panel>

        <Panel title="Contract spend">
          <BarChart
            limitable={false}
            format={money}
            rows={data.contract_spend.map((c): BarRow => ({
              label: `${c.label} (${c.contracts})`, n: c.total,
            }))}
          />
        </Panel>

        <Panel title="Record completeness">
          {/* Ratio bars against one denominator, so a full field and an empty
              one are compared down the same column. The value of this chart is
              the rows that are EMPTY - each is a filter or a chart somebody
              expects to work and finds blank. */}
          <BarChart
            sorted={false}
            limitable={false}
            rows={data.completeness.map((c): BarRow => ({
              label: c.label,
              n: c.filled,
              of: c.total,
              colour: c.filled === 0 ? 'var(--critical)'
                : c.filled === c.total ? 'var(--ok)' : 'var(--warn)',
            }))}
          />
        </Panel>
      </div>

      <h3 className="asset-charts-head">Capacity</h3>
      <div className="asset-cols">
        <Panel title="Cabinet space">
          {/* One part-to-whole, so a donut earns its keep here: the centre
              carries the figure everyone wants first, and every segment's own
              value is in the legend - nobody reads a number off an arc. Held
              is still not the free colour: those units are spoken for. */}
          <Donut
            centre={`${space.u_total.toLocaleString()}U`}
            centreLabel={`${space.racks} racks`}
            parts={[
              { label: 'Installed', n: space.u_used, colour: 'var(--accent)' },
              { label: 'Held for planned', n: space.u_held, colour: 'var(--warn)' },
              { label: 'Free', n: space.u_free, colour: 'var(--border-strong)' },
            ]}
          />
        </Panel>

        <Panel title="Floor space remaining">
          {/* A bounded ratio where LOW is the problem, which is the one
              question a gauge answers better than a bar: the bands say what
              being there means. Under 15% of positions left is red, under 40%
              amber. */}
          <Gauge
            value={floor.free}
            max={floor.designed}
            unit=""
            label={`rack positions · ${floor.rooms} rooms · ${Math.round(floor.area_m2).toLocaleString()} m²`}
            bands={[[0.15, 'var(--critical)'], [0.4, 'var(--warn)'], [1, 'var(--ok)']]}
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

/** The four cover states, in worsening-to-best order rather than by size, so
 *  the eye lands on the problem first and the shape stays comparable between
 *  visits. */
function coverRows(data: AssetCharts): BarRow[] {
  const w = data.cover_state;
  return [
    { label: 'Expired', n: w.expired, colour: 'var(--critical)',
      href: '/assets/inventory?warranty_state=expired' },
    { label: 'Expiring', n: w.expiring, colour: 'var(--warn)',
      href: '/assets/inventory?warranty_state=expiring' },
    { label: 'Covered', n: w.active, colour: 'var(--ok)',
      href: '/assets/inventory?warranty_state=active' },
    { label: 'No cover recorded', n: w.unknown, colour: 'var(--border-strong)',
      href: '/assets/inventory?warranty_state=unknown' },
  ];
}

function coverTotal(data: AssetCharts): number {
  const w = data.cover_state;
  return w.expired + w.expiring + w.active + w.unknown;
}

/** Money, rounded to whole units. Pennies on a five-figure contract are noise
 *  in a chart somebody reads to compare suppliers. */
function money(n: number): string {
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function sum(rows: { n: number }[]): number {
  return rows.reduce((t, r) => t + r.n, 0);
}

