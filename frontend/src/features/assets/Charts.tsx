import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type AssetCharts } from '../../api/client';
import { humanise } from '../../lib/format';

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

/** Bars beyond this are summed into one row. Twenty-five device types is a
 *  scrollbar, not a chart; the long tail is a single fact - "and 13 others". */
const TOP_N = 10;

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
          <BarList
            rows={collapse(data.by_type.map((t) => ({ label: t.label, n: t.n })))}
            href={(r) => `/assets/inventory?device_type=${encodeURIComponent(r.key ?? '')}`}
          />
        </Panel>

        <Panel title="By make" total={sum(data.by_vendor)}>
          <BarList rows={collapse(data.by_vendor.map((v) => ({ label: v.label, n: v.n })))} />
        </Panel>

        <Panel title="By lifecycle" total={sum(data.by_lifecycle)}>
          {/* States with nothing in them are kept, not dropped: "no assets are
              decommissioned" is a fact worth seeing, and a chart whose rows
              come and go cannot be compared with itself week to week. */}
          <BarList
            rows={data.by_lifecycle.map((l) => ({ label: humanise(l.key), n: l.n }))}
            keepZeros
            href={(r) => `/assets/inventory?lifecycle=${encodeURIComponent(r.key ?? '')}`}
            keys={data.by_lifecycle.map((l) => l.key)}
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

type Row = { label: string; n: number; key?: string };

function BarList({ rows, href, keepZeros, keys }: {
  rows: Row[];
  href?: (r: Row) => string;
  keepZeros?: boolean;
  keys?: string[];
}) {
  const withKeys = rows.map((r, i) => ({ ...r, key: keys?.[i] }));
  const shown = keepZeros ? withKeys : withKeys.filter((r) => r.n > 0);
  // Scaled to the largest bar, not to the total: at 310 of 664 the rest would
  // be slivers and the point of the chart is comparing them with each other.
  const max = Math.max(1, ...shown.map((r) => r.n));

  if (shown.length === 0) return <p className="muted">Nothing recorded.</p>;

  return (
    <div className="asset-barlist">
      {shown.map((r) => (
        <div className="asset-barlist-row" key={r.label}>
          {href && r.key ? (
            <Link to={href(r)}>{r.label}</Link>
          ) : (
            <span>{r.label}</span>
          )}
          <div className="asset-bar">
            <span style={{ width: `${(r.n / max) * 100}%` }} />
          </div>
          <div className="n">{r.n.toLocaleString()}</div>
        </div>
      ))}
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

/** Keep the top rows and fold the rest into one. A chart with a scrollbar has
 *  stopped being a chart. */
function collapse(rows: Row[]): Row[] {
  if (rows.length <= TOP_N + 1) return rows;
  const head = rows.slice(0, TOP_N);
  const tail = rows.slice(TOP_N);
  return [...head, { label: `and ${tail.length} others`, n: sum(tail) }];
}
