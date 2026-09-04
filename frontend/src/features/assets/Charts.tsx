import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type AssetCharts } from '../../api/client';
import { humanise } from '../../lib/format';
import { BarChart, type BarRow } from './components/BarChart';
import { useHoverTip } from './components/HoverTip';
import { Donut, Gauge, VColumns } from './components/Shapes';
import { Trends } from './Trends';

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

  return (
    <>
      <h3 className="asset-charts-head">Capacity</h3>
      <div className="asset-cols">
        <CabinetSpacePanel rows={data.rack_space} />

        <FloorSpacePanel rows={data.floor_space} />

        <StillFitsPanel rows={data.fragmentation} />

        <RackFillPanel rows={data.rack_fill} />
      </div>

      <h3 className="asset-charts-head">Cover and records</h3>
      <div className="asset-cols">
        <CoverLapsesPanel rows={data.warranty_runway} />

        <CoverStatePanel rows={data.cover_state} />

        <ContractSpendPanel rows={data.contract_spend} />

        <CompletenessPanel rows={data.completeness} />
      </div>

      <h3 className="asset-charts-head">Composition</h3>
      <div className="asset-cols">
        <ByTypePanel rows={data.composition} />

        <ByMakePanel rows={data.composition} />

        <ByLifecyclePanel rows={data.by_lifecycle} />
      </div>

      <h3 className="asset-charts-head">Where it is, and whose</h3>
      <div className="asset-cols">
        <ByRoomPanel rows={data.by_room} />

        <ByOwnerPanel rows={data.composition} />

        <PlacementPanel rows={data.placement} />
      </div>

      <Trends />
    </>
  );
}

type CompositionRow = AssetCharts['composition'][number];

function uniqSorted(values: string[]): string[] {
  return [...new Set(values)].sort();
}

/** The site-plus-one-dimension filter pair every cube panel shares: "By
 *  type" filters by make, "By make" and "By owner" by type, the cover
 *  panels by make, completeness by type - the useful cross in each case,
 *  without collapsing two always-visible charts into one hidden behind a
 *  dropdown. The second dropdown's options follow the site, and a selection
 *  the narrowed cube no longer contains is cleared rather than silently
 *  shown empty. */
function useCubeFilters<R extends { dc: string | null }>(rows: R[], other: (r: R) => string) {
  const [dc, setDc] = useState('');
  const [pick, setPick] = useState('');

  const dcs = uniqSorted(rows.map((r) => r.dc).filter((d): d is string => d !== null));
  // The empty string is a null dimension (a rack outside any room): such
  // rows count under "All", but an empty option would be indistinguishable
  // from it, so none is offered.
  const options = uniqSorted(
    rows.filter((r) => !dc || r.dc === dc).map(other).filter((v) => v !== ''));

  const pickDc = (next: string) => {
    setDc(next);
    if (pick && !rows.some((r) => (!next || r.dc === next) && other(r) === pick)) {
      setPick('');
    }
  };

  const shown = rows.filter((r) => (!dc || r.dc === dc) && (!pick || other(r) === pick));
  return { dc, pickDc, pick, setPick, dcs, options, shown };
}

function ByTypePanel({ rows }: { rows: CompositionRow[] }) {
  const f = useCubeFilters(rows, (r) => r.vendor);

  // Rolled up by type_key so the deep link into the inventory survives the
  // aggregation; the label rides along.
  const byType = new Map<string, { label: string; n: number }>();
  for (const r of f.shown) {
    const g = byType.get(r.type_key);
    if (g) g.n += r.n;
    else byType.set(r.type_key, { label: r.type_label, n: r.n });
  }
  const bars = [...byType.entries()].map(([key, g]): BarRow => ({
    label: g.label,
    n: g.n,
    href: `/assets/inventory?device_type=${encodeURIComponent(key)}`,
  }));

  return (
    <Panel title="By type" total={bars.reduce((t, r) => t + r.n, 0)}>
      <div className="asset-panel-filters">
        <select value={f.dc} onChange={(e) => f.pickDc(e.target.value)} aria-label="Site">
          <option value="">All sites</option>
          {f.dcs.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <select value={f.pick} onChange={(e) => f.setPick(e.target.value)} aria-label="Make">
          <option value="">All makes</option>
          {f.options.map((v) => <option key={v} value={v}>{v}</option>)}
        </select>
      </div>
      <BarChart rows={bars} />
    </Panel>
  );
}

function ByMakePanel({ rows }: { rows: CompositionRow[] }) {
  const f = useCubeFilters(rows, (r) => r.type_label);

  const byVendor = new Map<string, number>();
  for (const r of f.shown) byVendor.set(r.vendor, (byVendor.get(r.vendor) ?? 0) + r.n);
  const bars = [...byVendor.entries()].map(([label, n]): BarRow => ({ label, n }));

  return (
    <Panel title="By make" total={bars.reduce((t, r) => t + r.n, 0)}>
      <div className="asset-panel-filters">
        <select value={f.dc} onChange={(e) => f.pickDc(e.target.value)} aria-label="Site">
          <option value="">All sites</option>
          {f.dcs.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <select value={f.pick} onChange={(e) => f.setPick(e.target.value)} aria-label="Type">
          <option value="">All types</option>
          {f.options.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>
      <BarChart rows={bars} />
    </Panel>
  );
}

function ByOwnerPanel({ rows }: { rows: CompositionRow[] }) {
  const f = useCubeFilters(rows, (r) => r.type_label);

  const byOwner = new Map<string, number>();
  for (const r of f.shown) byOwner.set(r.owner, (byOwner.get(r.owner) ?? 0) + r.n);
  const bars = [...byOwner.entries()].map(([label, n]): BarRow => ({
    label,
    n,
    href: `/assets/inventory?owner_group=${encodeURIComponent(label)}`,
  }));

  return (
    <Panel title="By owner" total={bars.reduce((t, r) => t + r.n, 0)}>
      <div className="asset-panel-filters">
        <select value={f.dc} onChange={(e) => f.pickDc(e.target.value)} aria-label="Site">
          <option value="">All sites</option>
          {f.dcs.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <select value={f.pick} onChange={(e) => f.setPick(e.target.value)} aria-label="Type">
          <option value="">All types</option>
          {f.options.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>
      <BarChart rows={bars} />
    </Panel>
  );
}

/** The renewal timeline behind site and make filters - renewal is a
 *  per-vendor conversation, and this is where "when does APC cover lapse"
 *  gets its answer. A SEQUENCE, so it is never sorted by size: expired
 *  first, then each quarter, then the rest; quarter buckets sort lexically
 *  within their band, so (band, bucket) is the complete ordering. */
function CoverLapsesPanel({ rows }: { rows: AssetCharts['warranty_runway'] }) {
  const f = useCubeFilters(rows, (r) => r.vendor);

  const byBucket = new Map<string, { band: number; n: number }>();
  for (const r of f.shown) {
    const g = byBucket.get(r.bucket);
    if (g) g.n += r.n;
    else byBucket.set(r.bucket, { band: r.band, n: r.n });
  }
  const bars = [...byBucket.entries()]
    .sort((a, b) => a[1].band - b[1].band || a[0].localeCompare(b[0]))
    .map(([bucket, g]) => ({
      label: bucket.replace('Beyond 2 years', 'Later'),
      n: g.n,
      colour: g.band === 0 ? 'var(--critical)'
        : g.band === 2 ? 'var(--border-strong)' : undefined,
    }));

  return (
    <Panel title="When cover lapses" total={bars.reduce((t, r) => t + r.n, 0)}>
      <div className="asset-panel-filters">
        <select value={f.dc} onChange={(e) => f.pickDc(e.target.value)} aria-label="Site">
          <option value="">All sites</option>
          {f.dcs.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <select value={f.pick} onChange={(e) => f.setPick(e.target.value)} aria-label="Make">
          <option value="">All makes</option>
          {f.options.map((v) => <option key={v} value={v}>{v}</option>)}
        </select>
      </div>
      {/* is-short: quarter labels need a fraction of the diagonal room the
          By-room chart's site-and-hall names reserve. */}
      <div className="asset-vcols-tilt is-short">
        <VColumns rows={bars} />
      </div>
    </Panel>
  );
}

/** The four cover states behind the same (site, make) filters as the runway
 *  beside it, in worsening-to-best order rather than by size, so the eye
 *  lands on the problem first and the shape stays comparable between visits.
 *  Colour is the state, matching the Cover column in the table: expired is a
 *  fault, expiring a warning, no cover recorded an absence and deliberately
 *  painted as neither. */
function CoverStatePanel({ rows }: { rows: AssetCharts['cover_state'] }) {
  const f = useCubeFilters(rows, (r) => r.vendor);

  const n = (state: string) =>
    f.shown.filter((r) => r.state === state).reduce((t, r) => t + r.n, 0);
  const bars: BarRow[] = [
    { label: 'Expired', n: n('expired'), colour: 'var(--critical)',
      href: '/assets/inventory?warranty_state=expired' },
    { label: 'Expiring', n: n('expiring'), colour: 'var(--warn)',
      href: '/assets/inventory?warranty_state=expiring' },
    { label: 'Covered', n: n('active'), colour: 'var(--ok)',
      href: '/assets/inventory?warranty_state=active' },
    { label: 'No cover recorded', n: n('unknown'), colour: 'var(--border-strong)',
      href: '/assets/inventory?warranty_state=unknown' },
  ];

  return (
    <Panel title="Cover state" total={bars.reduce((t, r) => t + r.n, 0)}>
      <div className="asset-panel-filters">
        <select value={f.dc} onChange={(e) => f.pickDc(e.target.value)} aria-label="Site">
          <option value="">All sites</option>
          {f.dcs.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <select value={f.pick} onChange={(e) => f.setPick(e.target.value)} aria-label="Make">
          <option value="">All makes</option>
          {f.options.map((v) => <option key={v} value={v}>{v}</option>)}
        </select>
      </div>
      <BarChart sorted={false} limitable={false} rows={bars} />
    </Panel>
  );
}

/** Spend per supplier behind a contract-status filter on the same expiring
 *  threshold the cover charts use: "what am I paying now" and "what lapsed"
 *  are different meetings, and the all-time sum answers neither. */
function ContractSpendPanel({ rows }: { rows: AssetCharts['contract_spend'] }) {
  const [status, setStatus] = useState('');
  const shown = rows.filter((r) => !status || r.status === status);

  const bySupplier = new Map<string, { contracts: number; total: number }>();
  for (const r of shown) {
    const g = bySupplier.get(r.label);
    if (g) { g.contracts += r.contracts; g.total += r.total; }
    else bySupplier.set(r.label, { contracts: r.contracts, total: r.total });
  }
  const bars = [...bySupplier.entries()].map(([label, g]): BarRow => ({
    label: `${label} (${g.contracts})`, n: g.total,
  }));

  return (
    <Panel title="Contract spend">
      <div className="asset-panel-filters">
        <select value={status} onChange={(e) => setStatus(e.target.value)}
                aria-label="Contract status">
          <option value="">All contracts</option>
          <option value="active">Active</option>
          <option value="expiring">Expiring</option>
          <option value="expired">Expired</option>
        </select>
      </div>
      <BarChart limitable={false} format={money} rows={bars} />
    </Panel>
  );
}

/** The nine fields and their fill order, worst-consequence first. */
const COMPLETENESS_FIELDS = [
  ['Serial number', 'serial_number'],
  ['Asset tag', 'asset_tag'],
  ['Placement', 'placement'],
  ['Owner', 'owner_group'],
  ['Cover', 'warranty_expires'],
  ['Supplier', 'supplier_id'],
  ['Cost centre', 'cost_centre'],
  ['Purchase date', 'purchase_date'],
  ['Install date', 'install_date'],
] as const;

/** Field-fill ratios behind site and type filters, which turn a wall of
 *  ratios into a cleanup work-list: "which fields are empty on the PDUs in
 *  DC1". Every row of the cube carries its own denominator, so the ratios
 *  stay honest under any filter. Ratio bars share one denominator column, so
 *  a full field and an empty one are compared down the same column - the
 *  value of this chart is the rows that are EMPTY. */
function CompletenessPanel({ rows }: { rows: AssetCharts['completeness'] }) {
  const f = useCubeFilters(rows, (r) => r.type_label);

  const total = f.shown.reduce((t, r) => t + r.total, 0);
  const bars = COMPLETENESS_FIELDS.map(([label, key]): BarRow => {
    const filled = f.shown.reduce((t, r) => t + r[key], 0);
    return {
      label,
      n: filled,
      of: total,
      colour: filled === 0 ? 'var(--critical)'
        : filled === total ? 'var(--ok)' : 'var(--warn)',
    };
  });

  return (
    <Panel title="Record completeness">
      <div className="asset-panel-filters">
        <select value={f.dc} onChange={(e) => f.pickDc(e.target.value)} aria-label="Site">
          <option value="">All sites</option>
          {f.dcs.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <select value={f.pick} onChange={(e) => f.setPick(e.target.value)} aria-label="Type">
          <option value="">All types</option>
          {f.options.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>
      <BarChart sorted={false} limitable={false} rows={bars} />
    </Panel>
  );
}

/** The two dropdowns every cube panel wears, in one place. */
function FilterRow({ f, label, all }: {
  f: {
    dc: string; pickDc: (v: string) => void;
    pick: string; setPick: (v: string) => void;
    dcs: string[]; options: string[];
  };
  label: string; all: string;
}) {
  return (
    <div className="asset-panel-filters">
      <select value={f.dc} onChange={(e) => f.pickDc(e.target.value)} aria-label="Site">
        <option value="">All sites</option>
        {f.dcs.map((d) => <option key={d} value={d}>{d}</option>)}
      </select>
      <select value={f.pick} onChange={(e) => f.setPick(e.target.value)} aria-label={label}>
        <option value="">{all}</option>
        {f.options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </div>
  );
}

const roomOf = (r: { room: string | null }): string => r.room ?? '';

/** The one chart where a colour per bar earns itself: here the hue IS the
 *  state, and it is the same hue the chips in the table use, so the two read
 *  as one vocabulary. Empty states are kept - "nothing is decommissioned" is
 *  worth seeing, and a chart whose rows come and go cannot be compared with
 *  itself week to week. Site and type filters: "what state is the DC2 server
 *  fleet in" is the refresh-planning version of the question. */
function ByLifecyclePanel({ rows }: { rows: AssetCharts['by_lifecycle'] }) {
  const f = useCubeFilters(rows, (r) => r.type_label);

  const bars = Object.keys(LIFECYCLE_HUE).map((key): BarRow => ({
    label: humanise(key),
    n: f.shown.filter((r) => r.key === key).reduce((t, r) => t + r.n, 0),
    colour: LIFECYCLE_HUE[key],
    href: `/assets/inventory?lifecycle=${encodeURIComponent(key)}`,
  }));

  return (
    <Panel title="By lifecycle" total={bars.reduce((t, r) => t + r.n, 0)}>
      <FilterRow f={f} label="Type" all="All types" />
      <BarChart limitable={false} rows={bars} />
    </Panel>
  );
}

/** Placement states, in the fixed placed-to-missing order. */
const PLACEMENT_STATES = ['In a rack', 'Floor-standing', 'Not placed'];

/** Floor-standing is NOT a gap. A chiller in a plant room is placed; it
 *  simply has no rack, and colouring it as a problem would report the
 *  estate's own design as a data fault. "Not placed" is the only row here
 *  that means something is missing - and the type filter is how somebody
 *  hunts it down. */
function PlacementPanel({ rows }: { rows: AssetCharts['placement'] }) {
  const f = useCubeFilters(rows, (r) => r.type_label);

  const bars = PLACEMENT_STATES.map((label): BarRow => ({
    label,
    n: f.shown.filter((r) => r.label === label).reduce((t, r) => t + r.n, 0),
    colour: label === 'Not placed' ? 'var(--warn)'
      : label === 'Floor-standing' ? 'var(--border-strong)' : undefined,
  }));

  return (
    <Panel title="How it is placed" total={bars.reduce((t, r) => t + r.n, 0)}>
      <FilterRow f={f} label="Type" all="All types" />
      <BarChart sorted={false} limitable={false} rows={bars} />
    </Panel>
  );
}

/** One part-to-whole, so a donut earns its keep: the centre carries the
 *  figure everyone wants first, and every segment's own value is in the
 *  legend - nobody reads a number off an arc. Held is still not the free
 *  colour: those units are spoken for. Site and room filters, because
 *  capacity is a per-hall conversation and the estate-wide total is the
 *  least actionable version of the number. */
function CabinetSpacePanel({ rows }: { rows: AssetCharts['rack_space'] }) {
  const f = useCubeFilters(rows, roomOf);
  const s = f.shown.reduce((t, r) => ({
    racks: t.racks + r.racks,
    u_total: t.u_total + r.u_total,
    u_used: t.u_used + r.u_used,
    u_held: t.u_held + r.u_held,
  }), { racks: 0, u_total: 0, u_used: 0, u_held: 0 });
  const free = Math.max(0, s.u_total - s.u_used - s.u_held);

  return (
    <Panel title="Cabinet space">
      <FilterRow f={f} label="Room" all="All rooms" />
      <Donut
        centre={`${s.u_total.toLocaleString()}U`}
        centreLabel={`${s.racks} racks`}
        parts={[
          { label: 'Installed', n: s.u_used, colour: 'var(--accent)' },
          { label: 'Held for planned', n: s.u_held, colour: 'var(--warn)' },
          { label: 'Free', n: free, colour: 'var(--border-strong)' },
        ]}
      />
    </Panel>
  );
}

/** A bounded ratio read as UTILIZATION, the way an operator quotes it -
 *  "the hall is at 85%" - with the needle climbing toward red as the hall
 *  fills, which is what every neighbouring capacity panel frames too. Green
 *  to 60% used, amber to 85%, red above. The positions still free stay
 *  printed in the label, because that is the number an install is planned
 *  with. The denominator lives on the room, so the gauge stays honest at
 *  every filter level. */
function FloorSpacePanel({ rows }: { rows: AssetCharts['floor_space'] }) {
  const f = useCubeFilters(rows, roomOf);
  const designed = f.shown.reduce((t, r) => t + r.designed, 0);
  const installed = f.shown.reduce((t, r) => t + r.installed, 0);
  const area = f.shown.reduce((t, r) => t + r.area_m2, 0);
  const rooms = f.shown.length;
  const free = Math.max(0, designed - installed);

  return (
    <Panel title="Floor space">
      <FilterRow f={f} label="Room" all="All rooms" />
      <Gauge
        value={installed}
        max={designed}
        unit=""
        label={`rack positions used · ${free.toLocaleString()} free · ${rooms} room${rooms === 1 ? '' : 's'} · ${Math.round(area).toLocaleString()} m²`}
        bands={[[0.6, 'var(--ok)'], [0.85, 'var(--warn)'], [1, 'var(--critical)']]}
      />
    </Panel>
  );
}

/** Chassis heights that exist; a continuous axis would imply 5U equipment
 *  somebody could buy. */
const CHASSIS_SIZES = [1, 2, 3, 4, 6, 8];

/** The number fragmentation costs. Total free U says nothing about
 *  placeability - 1392U spread as slivers takes hundreds of 1U servers and
 *  few 4U chassis, and this is the chart where that fall-off is visible.
 *  Fits are additive per rack, so "can Hall A take another blade chassis"
 *  is an exact client-side sum. */
function StillFitsPanel({ rows }: { rows: AssetCharts['fragmentation'] }) {
  const f = useCubeFilters(rows, roomOf);
  const bars = CHASSIS_SIZES.map((size) => ({
    label: `${size}U`,
    n: f.shown.filter((r) => r.size === size).reduce((t, r) => t + r.fits, 0),
  }));

  return (
    <Panel title="What still fits">
      <FilterRow f={f} label="Room" all="All rooms" />
      <VColumns caption="Potential new items" rows={bars} />
    </Panel>
  );
}

/** How full a rack is, in bands somebody would act on rather than even
 *  tenths - "Empty" and "Over 90%" are the two that mean something alone. */
const FILL_BANDS = ['Empty', '1-25%', '26-50%', '51-75%', '76-90%', 'Over 90%'];

/** The distribution behind the single "free U" figure. A mean hides whether
 *  the space is one empty cabinet or forty part-used ones, and only the
 *  first of those takes a full-height install. */
function RackFillPanel({ rows }: { rows: AssetCharts['rack_fill'] }) {
  const f = useCubeFilters(rows, roomOf);
  const bands = FILL_BANDS.map((band) => ({
    band,
    n: f.shown.filter((r) => r.band === band).reduce((t, r) => t + r.n, 0),
  }));

  return (
    <Panel title="How full the racks are">
      <FilterRow f={f} label="Room" all="All rooms" />
      <Histogram rows={bands} />
    </Panel>
  );
}

/** "By room", as vertical columns behind a site and a room filter.
 *
 *  Room names are long for a column chart, so the site filter earns its keep
 *  twice: it narrows the rows, and with a site chosen the DC prefix is dropped
 *  from every label so the room name gets the width. The room list follows the
 *  site - a room of a filtered-out site is not offered, and a selection that
 *  the new site does not have is cleared rather than silently shown empty. */
function ByRoomPanel({ rows }: { rows: AssetCharts['by_room'] }) {
  const [dc, setDc] = useState('');
  const [room, setRoom] = useState('');
  const [type, setType] = useState('');

  const dcs = uniqSorted(rows.map((r) => r.dc));
  const rooms = uniqSorted(rows.filter((r) => !dc || r.dc === dc).map((r) => r.room));
  // The type list follows BOTH narrower filters: a type no surviving room
  // holds is not offered.
  const types = uniqSorted(rows
    .filter((r) => (!dc || r.dc === dc) && (!room || r.room === room))
    .map((r) => r.type_label));

  const pickDc = (next: string) => {
    setDc(next);
    if (room && !rows.some((r) => (!next || r.dc === next) && r.room === room)) {
      setRoom('');
    }
    if (type && !rows.some((r) => (!next || r.dc === next) && r.type_label === type)) {
      setType('');
    }
  };
  const pickRoom = (next: string) => {
    setRoom(next);
    if (type && !rows.some((r) => (!dc || r.dc === dc)
        && (!next || r.room === next) && r.type_label === type)) {
      setType('');
    }
  };

  const shown = rows.filter((r) => (!dc || r.dc === dc)
    && (!room || r.room === room) && (!type || r.type_label === type));

  // One column per room; the per-type rows sum away, and the columns re-rank
  // by the filtered count - "where do the PDUs sit" should lead with the
  // room that holds the most of them.
  const byLabel = new Map<string, number>();
  for (const r of shown) {
    const key = dc ? r.room : r.label;
    byLabel.set(key, (byLabel.get(key) ?? 0) + r.n);
  }
  const cols = [...byLabel.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([label, n]) => ({ label, n }));

  return (
    <Panel title="By room" total={cols.reduce((t, r) => t + r.n, 0)} wide>
      <div className="asset-panel-filters">
        <select value={dc} onChange={(e) => pickDc(e.target.value)} aria-label="Site">
          <option value="">All sites</option>
          {dcs.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <select value={room} onChange={(e) => pickRoom(e.target.value)} aria-label="Room">
          <option value="">All rooms</option>
          {rooms.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <select value={type} onChange={(e) => setType(e.target.value)} aria-label="Type">
          <option value="">All types</option>
          {types.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>
      {/* Tilted labels: sixteen room names across one panel truncate to a
          single letter when horizontal, and a label that says nothing is not
          a label. The tilt is on the wrapper, not VColumns, because the other
          column charts have short labels and should keep them flat. */}
      <div className="asset-vcols-tilt">
        <VColumns rows={cols} />
      </div>
    </Panel>
  );
}

/** The total names its unit ("664 devices", not a bare "664"): the page is
 *  full of figures that could as easily be U, kW or racks, and the one word
 *  where the number sits settles it for every panel at once. Counting
 *  devices is the default because every counted panel here counts them; a
 *  future money or capacity total passes its own unit. */
function Panel({ title, total, unit = 'devices', wide, children }: {
  title: string; total?: number; unit?: string; wide?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className={`asset-panel${wide ? ' asset-panel-wide' : ''}`}>
      <h3>
        {title}
        {total != null && (
          <span className="asset-panel-total">
            {total.toLocaleString()}
            <span className="unit"> {unit}</span>
          </span>
        )}
      </h3>
      {children}
    </div>
  );
}

function Histogram({ rows }: { rows: { band: string; n: number }[] }) {
  const max = Math.max(1, ...rows.map((r) => r.n));
  const { bind, tipEl } = useHoverTip();
  return (
    <div className="asset-vcols-frame">
    <div className="asset-vcols-ylabel">Number of cabinets</div>
    <div className="asset-histogram">
      {rows.map((r) => (
        <div className="asset-histogram-col" key={r.band}
             {...bind(<><b>{r.band}</b> {r.n.toLocaleString()} rack{r.n === 1 ? '' : 's'}</>)}>
          <div className="asset-col-stack">
            <div className="v">{r.n || ''}</div>
            <div
              className={`asset-histogram-bar${toneOf(r.band)}`}
              style={{ height: `${(r.n / max) * 100}%` }}
            />
          </div>
          <div className="k">{r.band}</div>
        </div>
      ))}
      </div>
    {tipEl}
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

/** Money, rounded to whole units. Pennies on a five-figure contract are noise
 *  in a chart somebody reads to compare suppliers. */
function money(n: number): string {
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
}


