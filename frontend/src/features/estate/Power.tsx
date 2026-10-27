import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import {
  api, type AlarmCategory, type PowerFeeds, type PowerRow, type PowerSpread,
} from '../../api/client';
import { AlarmPanel } from '../home/AlarmPanel';
import { RoomDrawer } from '../home/RoomDrawer';
import {
  Column, Crumb, Crumbs, DataTable, Delta, FacilityToggle, Notes, Num, PageHead,
  ScopeTabs, Seg, TableFoot,
} from '../../components/estate';
import { Tip } from '../../components/HoverTip';
import { downloadCsv, stampedName } from '../../lib/csv';
import { useEstateTable } from './useEstateTable';
import { useTrendRange } from './ThermalTrend';
import { PowerDraw, PowerLoad, PowerPue } from './PowerTrend';

/** Power: where the estate's kilowatts go, how close each rack and hall is
 *  to what it was built for, and what that does to PUE.
 *
 *  Three windows, because they answer different questions. NOW is the live
 *  draw from the hot mirror, against fifteen minutes ago. LAST HOUR is sixty
 *  minutes of five-minute buckets. BY DAY is a date range. In the windowed
 *  views AVERAGE is the mean - what the estate costs to run - and PEAK is
 *  the COINCIDENT maximum: loads summed per bucket, then the largest bucket
 *  taken, at every tier, so a site's peak is a moment that happened rather
 *  than its halls' private worst moments added up.
 *
 *  Three tiers, like Thermal: site, room, rack. A rack's load is the draw
 *  its servers and switches report - the same population the room's IT
 *  column sums - so racks add up to their room, and every row is judged
 *  against a rating the row names: the rack's own, the room's design figure
 *  or the sum of its racks', the site's the same way.
 */

type Window = 'now' | 'hour' | 'day';
type Stat = 'average' | 'peak';
type Unit = 'kw' | 'kva';

/** Local midnight, `n` days back, as the value a date input wants. */
function daysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

/** The midnight AFTER a picked day, so "21st to 21st" is that whole day
 *  rather than the empty instant at its start - which the server refuses. */
function endOfDay(day: string): string {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString();
}

/** Nominal power factor for the kVA view where nothing meters apparent
 *  power. Rack loads report watts; a server PSU runs at 0.95 or better
 *  under load, and the tooltip says it is nominal. */
const NOMINAL_PF = 0.95;
const UNIT_KEY = 'dcim.power.unit';

function readUnit(): Unit {
  try { return localStorage.getItem(UNIT_KEY) === 'kva' ? 'kva' : 'kw'; } catch { return 'kw'; }
}

/** The spread of a row's racks against their ratings, as a bar.
 *
 *  Four bands and a grey. Blue is headroom, not a problem: a rack under
 *  60 % has room for the other side's load on a failover. Grey is a rack
 *  with no rating or no reading - not a band, and said so in the tip. */
function Spread({ d, warn, critical }: { d: PowerSpread | null | undefined; warn: number; critical: number }) {
  if (!d) return <Tip className="dash" tip="no racks here">—</Tip>;
  const n = d.under_60 + d.b60_80 + d.b80_90 + d.over_90 + d.unrated;
  if (!n) return <Tip className="dash" tip="no racks here">—</Tip>;
  const segs: Array<[string, number, string]> = [
    ['cool', d.under_60, 'under 60 % of rating: headroom for a failover'],
    ['ok', d.b60_80, `60–${warn} % of rating`],
    ['warn', d.b80_90, `${warn}–${critical} % of rating: no room for the other side`],
    ['critical', d.over_90, `${critical} % and over: the next server lands on a breaker`],
    ['unknown', d.unrated, 'no rating, or nothing reporting'],
  ];
  return (
    <Tip tip={<>{segs.map(([k, v, label]) => (
      <span key={k} className="spread-line"><b>{v}</b> {v === 1 ? 'rack' : 'racks'} {label}</span>
    ))}</>}>
      <span className="stack-bar cell" role="img"
            aria-label={`${d.under_60} racks under 60 %, ${d.b60_80} at 60 to ${warn} %, ${d.b80_90} at ${warn} to ${critical} %, ${d.over_90} over ${critical} %, ${d.unrated} unrated`}>
        {segs.filter(([, v]) => v > 0).map(([k, v]) => (
          <span key={k} className={`stack-seg ${k}`} style={{ width: `${(100 * v) / n}%` }} />
        ))}
      </span>
    </Tip>
  );
}

/** A rack's feeds in one cell: the A strip, the B strip, and how the loads
 *  are corded. Amber where a load has one cord, red where one has none -
 *  the two facts a maintenance window turns on. */
function Feeds({ f }: { f: PowerFeeds | null | undefined }) {
  if (!f || !f.loads) return <Tip className="dash" tip="no IT or network load in this rack">—</Tip>;
  const sides = [
    f.a.length ? `A ${f.a.join(', ')}` : 'A —',
    f.b.length ? `B ${f.b.join(', ')}` : 'B —',
    ...(f.other.length ? [`unsided ${f.other.join(', ')}`] : []),
  ].join(' · ');
  const cording = f.unfed
    ? `${f.unfed} of ${f.loads} loads fed by nothing`
    : f.single_cord
      ? `${f.single_cord} of ${f.loads} loads on one cord`
      : `all ${f.loads} loads on both sides`;
  const tone = f.unfed ? 'critical' : f.single_cord ? 'warn' : undefined;
  return (
    <Tip tip={<>{sides}<br />{cording}. Dual means two different sides; two cords into one strip is one feed.</>}>
      <span className={`pw-feeds ${tone ?? ''}`}>
        <span className="side">{f.a.length ? 'A' : '·'}</span>
        <span className="side">{f.b.length ? 'B' : '·'}</span>
        <span className="n">{f.unfed ? `${f.unfed} unfed` : f.single_cord ? `${f.single_cord} single` : `${f.dual_cord} dual`}</span>
      </span>
    </Tip>
  );
}

function basisWords(basis: string | null | undefined, racks?: number): string {
  switch (basis) {
    case 'rack': return 'the rack\'s own rating';
    case 'design': return 'the design IT figure in the inventory';
    case 'racks': return `the sum of ${racks ?? 'its'} rack ratings`;
    case 'rooms': return 'the sum of the rooms\' ratings';
    default: return 'no rating';
  }
}

export function Power() {
  const [win, setWin] = useState<Window>('now');
  const [stat, setStat] = useState<Stat>('average');
  const [from, setFrom] = useState(daysAgo(1));
  const [to, setTo] = useState(daysAgo(0));
  const [unit, setUnit] = useState<Unit>(readUnit);
  const [drawerRoom, setDrawerRoom] = useState<{ id: string; name: string } | null>(null);
  const [drill, setDrill] = useState<{ kind: 'site' | 'room'; id: string; label: string } | null>(null);
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const trendRange = useTrendRange();

  useEffect(() => { try { localStorage.setItem(UNIT_KEY, unit); } catch { /* per-viewer convenience only */ } }, [unit]);

  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['estate-power', win, stat, from, to],
    queryFn: () => api.estatePower(
      win === 'now' ? { live: true }
        : win === 'hour' ? { hour: true, mode: stat }
          : { mode: stat, start: new Date(`${from}T00:00:00Z`).toISOString(), end: endOfDay(to) },
    ),
    // NOW is the hot mirror and moves every poll; the hour window moves by
    // a bucket every five minutes; a day range does not move at all.
    refetchInterval: win === 'now' ? 30_000 : win === 'hour' ? 60_000 : false,
    // Hold the rows while another window loads. A week of buckets takes a
    // few seconds, and a page that blanks its six headline figures for that
    // long reads as broken rather than busy.
    placeholderData: (prev) => prev,
  });

  const t = useEstateTable<PowerRow>(data?.sites ?? [], data?.rooms ?? [], data?.racks ?? []);
  const bands = data?.bands ?? { warn_pct: 80, critical_pct: 90, pue_warn: 1.6 };
  const rackTier = t.tier === 'racks';
  const u = unit === 'kw' ? 'kW' : 'kVA';
  const conv = (kw: number | null | undefined): number | null =>
    kw === null || kw === undefined ? null : unit === 'kw' ? kw : kw / NOMINAL_PF;
  const unitTip = unit === 'kva' ? ' Apparent power at a nominal 0.95 power factor; nothing on this row meters kVA.' : '';

  /** The row's state, read down the left edge. Load against rating first;
   *  then the feeds, because a rack on one cord is the rack a maintenance
   *  window takes out. Alarms are a column, not the edge - a condition on
   *  a strip is already counted where it stands. */
  function severity(r: PowerRow): 'ok' | 'warn' | 'critical' | 'none' {
    if (r.total_kw === null || r.total_kw === undefined) return 'none';
    if (r.band === 'critical' || (r.feeds?.unfed ?? 0) > 0) return 'critical';
    if (r.band === 'warn' || (r.feeds?.single_cord ?? 0) > 0) return 'warn';
    return r.band ? 'ok' : 'none';
  }

  const tierLabel = { sites: 'Site', rooms: 'Room', racks: 'Rack' }[t.tier];
  const statWord = win === 'now' ? 'now' : stat === 'peak' ? 'at the coincident peak' : 'on average';

  const columns: Column<PowerRow>[] = [
    {
      key: 'name', label: tierLabel,
      help: 'A site, a room or a rack. Click a row to go deeper; a rack opens its elevation with the power overlay.',
      sort: (r) => r.name,
      render: (r) => (
        <div className="name-cell">
          <span className="n">{r.name}</span>
          {r.kind === 'room' && (
            <span className="where">{r.site_code}{r.floor ? ` · floor ${r.floor}` : ''}</span>
          )}
          {r.kind === 'rack' && <span className="where">{r.room_name}</span>}
        </div>
      ),
    },
    {
      key: 'total', label: `Load ${u}`, align: 'num', width: 110,
      help: rackTier
        ? `The draw the rack's servers and switches report, ${statWord}. The strip's own meter, on the rack page, reads a few percent above it.`
        : `Everything metered in this scope, ${statWord}: IT, cooling and the rest. Rack PDUs are excluded so the servers behind them are not counted twice.`,
      sort: (r) => r.total_kw,
      render: (r) => (
        <Tip tip={r.total_kw === null ? (r.note ?? 'nothing here reports power')
          : `${r.total_kw.toFixed(1)} kW ${statWord}${unitTip}`}>
          <Num value={conv(r.total_kw)} why={r.note} />
        </Tip>
      ),
    },
    {
      key: 'pct', label: 'of rating', align: 'num', width: 96,
      help: `IT load as a share of what the row is rated for - the rack's own rating, a room's design figure or the sum of its racks', a site's the same way. Amber from ${bands.warn_pct} %, red from ${bands.critical_pct} %.`,
      sort: (r) => r.load_pct ?? null,
      render: (r) => (
        <Tip tip={r.load_pct === null || r.load_pct === undefined
          ? (r.rated_kw ? 'nothing here reports power' : 'no rating recorded for this row')
          : <><b>{r.it_ac_kw?.toFixed(1)} kW</b> of <b>{r.rated_kw?.toFixed(1)} kW</b> rated · {basisWords(r.rating_basis, r.racks_rated)}
              {r.band === 'critical' ? ` · past ${bands.critical_pct} %` : r.band === 'warn' ? ` · past ${bands.warn_pct} %` : ''}</>}>
          <span className={r.band === 'critical' ? 'critical' : r.band === 'warn' ? 'warn' : undefined}>
            <Num value={r.load_pct} unit="%" />
          </span>
        </Tip>
      ),
    },
    {
      key: 'delta', label: 'Δ vs prior', align: 'num', width: 104,
      help: win === 'now' ? 'Against the same reading fifteen minutes ago.'
        : `Against ${data?.window.compare_label ?? 'the window before'}.`,
      sort: (r) => r.delta_total,
      render: (r) => (
        <Delta value={conv(r.delta_total)} unit={` ${u}`}
               why={r.total_kw === null ? 'nothing reporting now' : 'nothing reported in the comparison window'} />
      ),
    },
    ...(rackTier ? [] : [{
      key: 'spread', label: 'Racks', align: 'mid' as const, width: 116,
      help: 'Where this row\'s racks sit against their ratings. Blue is headroom, grey is a rack with no rating or no reading.',
      sort: (r: PowerRow) => (r.spread ? r.spread.over_90 * 1000 + r.spread.b80_90 : null),
      render: (r: PowerRow) => <Spread d={r.spread} warn={bands.warn_pct} critical={bands.critical_pct} />,
    }]),
    ...(rackTier ? [{
      key: 'alloc', label: `Allocated ${u}`, align: 'num' as const, width: 118,
      help: 'What the devices in the rack were budgeted at - the model\'s budget where one is recorded, else nameplate derated - plus any held reservation. The planning number the metered load is read against.',
      sort: (r: PowerRow) => r.allocated_kw ?? null,
      render: (r: PowerRow) => (
        <Tip tip={r.allocated_kw === null || r.allocated_kw === undefined
          ? 'no device in this rack carries a budget or a nameplate'
          : <>{r.allocated_kw.toFixed(1)} kW budgeted{r.reserved_kw ? ` · ${r.reserved_kw.toFixed(1)} kW reserved` : ''}
              {r.unrated_devices ? ` · ${r.unrated_devices} device${r.unrated_devices === 1 ? '' : 's'} with no rating` : ''}{unitTip}</>}>
          <Num value={conv(r.allocated_kw)} />
        </Tip>
      ),
    }, {
      key: 'feeds', label: 'Feeds', align: 'mid' as const, width: 120,
      help: 'The strips feeding this rack\'s loads, by side, and whether every load has a cord on each. One cord is one maintenance window away from dark.',
      sort: (r: PowerRow) => (r.feeds ? r.feeds.unfed * 1000 + r.feeds.single_cord : null),
      render: (r: PowerRow) => <Feeds f={r.feeds} />,
    }] : [{
      key: 'it', label: `IT ${u}`, align: 'num' as const, width: 96,
      help: 'Servers, storage and network gear.',
      sort: (r: PowerRow) => r.it_ac_kw,
      render: (r: PowerRow) => <Num value={conv(r.it_ac_kw)} why={r.note} />,
    }, {
      key: 'cooling', label: `Cooling ${u}`, align: 'num' as const, width: 104,
      help: 'CRAHs, CDUs, chillers, pumps and towers standing in this scope.',
      sort: (r: PowerRow) => r.cooling_kw,
      render: (r: PowerRow) => <Num value={conv(r.cooling_kw)} why={r.note} />,
    }, {
      key: 'other', label: `Other ${u}`, align: 'num' as const, width: 96,
      help: 'Everything metered that is neither IT nor cooling: lighting, security, the odd plant.',
      sort: (r: PowerRow) => r.other_kw,
      render: (r: PowerRow) => <Num value={conv(r.other_kw)} why={r.note} />,
    }, {
      key: 'pue', label: 'PUE', align: 'num' as const, width: 80,
      help: `Total over IT, as a ratio of the draw in this window - not the energy-based figure under Analytics. Amber past ${bands.pue_warn}.`,
      sort: (r: PowerRow) => r.pue,
      render: (r: PowerRow) => (
        <span className={r.pue !== null && r.pue > bands.pue_warn ? 'warn' : undefined}>
          <Num value={r.pue} digits={3}
               why={r.it_ac_kw ? 'no total to divide' : 'no IT load in this scope to divide by'} />
        </span>
      ),
    }]),
    {
      key: 'alarms', label: 'Alarms', align: 'num', width: 84,
      help: 'Open power conditions here: strips, UPS, ATS, switchgear, generators.',
      sort: (r) => r.alarms_open ?? 0,
      render: (r) => {
        const n = r.alarms_open ?? 0;
        const where = r.kind === 'rack' ? 'in this rack' : `in ${r.name}`;
        if (!n) return <Tip className="dash" tip={`nothing open ${where}`}>0</Tip>;
        const inRacks = r.alarms_in_racks ?? n;
        const onPlant = Math.max(n - inRacks, 0);
        const split = r.kind === 'rack' || !onPlant ? ''
          : inRacks
            ? ` - ${inRacks} in racks, ${onPlant} on floor-standing gear`
            : ` - none in racks, all ${onPlant} on floor-standing gear such as a UPS or a panel`;
        const label = `${n} open power condition${n === 1 ? '' : 's'} ${where}${split}`;
        if (r.kind === 'rack') return <Tip tip={label}>{n}</Tip>;
        return (
          <Tip tip={`${label} - open them`}>
            <button type="button" className="link-button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setDrill({ kind: r.kind as 'site' | 'room', id: r.id, label: r.name });
                    }}>
              {n}
            </button>
          </Tip>
        );
      },
    },
    {
      key: 'reporting', label: 'Reporting', align: 'num', width: 96,
      help: rackTier
        ? 'Loads in the rack with a reading, over loads in it.'
        : win === 'now'
          ? 'Devices with a reading in the hot mirror, over devices that could have one. Below, racks with a reading over racks.'
          : 'Racks with a reading in this window, over racks in the row.',
      sort: (r) => (typeof r.reporting === 'number' ? r.reporting : r.racks_reporting ?? null),
      render: (r) => {
        if (r.kind === 'rack') {
          if (!r.loads) return <Tip className="dash" tip="no IT or network device in this rack">—</Tip>;
          return typeof r.reporting === 'number'
            ? <Tip tip={`${r.reporting} of ${r.loads} loads reporting`}>{r.reporting}<span className="muted">/{r.loads}</span></Tip>
            : <Tip tip={`${r.loads} loads in the rack`}>{r.loads}</Tip>;
        }
        const racks = r.rack_count ?? 0;
        const dev = typeof r.reporting === 'object' && r.reporting ? r.reporting : null;
        if (!racks && !dev) return <Tip className="dash" tip="no racks and nothing reporting here">—</Tip>;
        return (
          <Tip tip={<>{dev ? <>{dev.reporting} of {dev.devices} devices reporting<br /></> : null}
                       {racks ? `${r.racks_reporting ?? 0} of ${racks} racks with a reading` : 'no racks here'}</>}>
            {dev ? <>{dev.reporting}<span className="muted">/{dev.devices}</span></>
              : <>{r.racks_reporting ?? 0}<span className="muted">/{racks}</span></>}
          </Tip>
        );
      },
    },
  ];

  function exportCsv() {
    downloadCsv(
      stampedName('power', data?.window.label ?? win),
      ['scope', 'name', 'site', 'room', 'floor', 'total_kw', 'it_ac_kw', 'it_dc_kw',
       'cooling_kw', 'other_kw', 'pue', 'delta_total_kw', 'rated_kw', 'rating_basis',
       'load_pct', 'band', 'allocated_kw', 'reserved_kw', 'feeds_a', 'feeds_b',
       'loads', 'dual_cord', 'single_cord', 'unfed', 'open_alarms', 'racks',
       'racks_reporting', 'note'],
      t.filtered.map((r) => [
        r.kind, r.name, r.site_code, r.room_name ?? '', r.floor ?? '', r.total_kw ?? '',
        r.it_ac_kw ?? '', '', r.cooling_kw ?? '', r.other_kw ?? '', r.pue ?? '',
        r.delta_total ?? '', r.rated_kw ?? '', r.rating_basis ?? '', r.load_pct ?? '',
        r.band ?? '', r.allocated_kw ?? '', r.reserved_kw ?? '',
        r.feeds?.a.join(' ') ?? '', r.feeds?.b.join(' ') ?? '', r.feeds?.loads ?? r.loads ?? '',
        r.feeds?.dual_cord ?? '', r.feeds?.single_cord ?? r.single_cord ?? '',
        r.feeds?.unfed ?? r.unfed ?? '', r.alarms_open ?? 0, r.rack_count ?? '',
        r.racks_reporting ?? '', r.note ?? '',
      ]),
    );
  }

  const totals = data?.totals;
  const head = t.selectedRoom ?? t.selected ?? null;

  const crumbs: Crumb[] = [];
  if (t.selected || t.selectedRoom || t.scope === 'rooms') {
    crumbs.push({ label: 'All sites', onClick: () => t.setScope('sites') });
  }
  if (t.selected) {
    const deeper = Boolean(t.selectedRoom);
    crumbs.push({
      label: t.selected.site_code,
      note: deeper ? undefined : t.selected.name !== t.selected.site_code ? t.selected.name : undefined,
      onClick: deeper ? () => t.clearDrill() : undefined,
    });
  } else if (t.scope === 'rooms') {
    crumbs.push({ label: 'All rooms' });
  }
  if (t.selectedRoom) {
    crumbs.push({
      label: t.selectedRoom.name,
      note: t.selectedRoom.floor ? `floor ${t.selectedRoom.floor}` : undefined,
    });
  }
  // A stale link resolves to the level above; the trail says so by not
  // claiming a room it cannot find.
  const wantsRoom = params.get('room');
  const stale = Boolean(wantsRoom) && data && !t.selectedRoom;
  // The charts' scope comes from the row the reader drilled into, and a row
  // only exists once the payload has arrived. Drawn before then, the panels
  // ask for the whole estate's trend and throw it away a moment later.
  const wantsSite = params.get('site');
  const scopeKnown = (!wantsSite || Boolean(t.selected)) && (!wantsRoom || Boolean(t.selectedRoom));

  const windowNote = !data ? ''
    : win === 'now'
      ? `Now: the newest reading the ingest worker wrote for each device${totals?.as_of ? `, as of ${new Date(totals.as_of).toLocaleTimeString()}` : ''}; compared with ${data.window.compare_label ?? 'fifteen minutes ago'}.`
      : `Window: ${data.window.label}, ${stat === 'peak' ? 'coincident peak per bucket' : 'mean'}, compared with ${data.window.compare_label ?? 'the window before'}. Days are UTC.`;

  return (
    <div className="estate">
      <PageHead
        title="Power"
        sub={<>Metered draw against what each rack and hall was built for. Amber from {bands.warn_pct} % of
          rating, red from {bands.critical_pct} %; rack PDUs are excluded from every total so the servers behind
          them are not counted twice.{' '}
          <Link to="/analytics?view=power">Redundancy census and supplies →</Link></>}
        kpis={[
          { caption: 'Total', value: conv(totals?.total_kw ?? null), unit: u,
            why: 'nothing in the estate reports power' },
          { caption: 'IT', value: conv(totals?.it_ac_kw ?? null), unit: u,
            why: 'no server, storage or network device reports power' },
          { caption: 'Cooling', value: conv(totals?.cooling_kw ?? null), unit: u,
            why: 'no cooling unit reports power' },
          { caption: 'PUE', value: totals?.pue ?? null, digits: 3,
            tone: (totals?.pue ?? 0) > bands.pue_warn ? 'warn' : 'ok',
            why: 'no IT load reported, so there is nothing to divide by' },
          { caption: 'Of rating', value: totals?.load_pct ?? null, unit: '%',
            tone: totals?.band === 'critical' ? 'critical' : totals?.band === 'warn' ? 'warn' : 'ok',
            why: 'no room or rack carries a rating' },
          { caption: 'Halls reporting',
            value: totals ? `${totals.halls_reporting}/${totals.halls}` : null,
            tone: totals && totals.halls_reporting < totals.halls ? 'warn' : 'ok' },
        ]}
      />

      <div className="estate-tools">
        <ScopeTabs scope={t.scope} onChange={t.setScope} />
        {(t.scope === 'rooms' || t.selected) && !t.selectedRoom && (
          <FacilityToggle on={t.includeFacility} count={t.facilityCount}
                          onChange={t.setIncludeFacility} />
        )}
        <input className="grow" type="search" placeholder="Search sites, rooms and racks"
               aria-label="Search" value={t.search}
               onChange={(e) => t.setSearch(e.target.value)} />
        <Seg label="Unit" value={unit} onChange={setUnit}
             options={[{ key: 'kw', label: 'kW' }, { key: 'kva', label: 'kVA' }]} />
        <Seg label="Window" value={win} onChange={setWin}
             options={[{ key: 'now', label: 'NOW' },
                       { key: 'hour', label: 'LAST HOUR' },
                       { key: 'day', label: 'BY DAY' }]} />
        {win !== 'now' && (
          <Seg label="Statistic" value={stat} onChange={setStat}
               options={[{ key: 'average', label: 'AVERAGE' },
                         { key: 'peak', label: 'PEAK' }]} />
        )}
        {win === 'day' && (
          <>
            <label className="field">
              <span>From</span>
              <input type="date" value={from} max={to}
                     onChange={(e) => setFrom(e.target.value)} />
            </label>
            <label className="field">
              <span>To</span>
              <input type="date" value={to} min={from} max={daysAgo(0)}
                     onChange={(e) => setTo(e.target.value)} />
            </label>
          </>
        )}
      </div>

      <Crumbs onBack={() => t.clearDrill()} items={crumbs} />

      <div className="estate-panel">
        {(t.tier === 'rooms' || head) && (
          <div className="estate-selected">
            {t.tier === 'rooms' && !head && <span className="who">Server rooms</span>}
            {t.selectedRoom && head && (
              <button className="back" title="Devices, alarms and trend for this room"
                      onClick={() => setDrawerRoom({ id: head.id, name: head.name })}>
                ROOM DETAILS
              </button>
            )}
            {head && (
              <div className="pairs">
                <span className="pair"><span className="cap">Total</span>
                  <span className="v"><Num value={conv(head.total_kw)} /> {u}</span></span>
                <span className="pair"><span className="cap">IT</span>
                  <span className="v"><Num value={conv(head.it_ac_kw)} /> {u}</span></span>
                <span className="pair"><span className="cap">Cooling</span>
                  <span className="v"><Num value={conv(head.cooling_kw)} /> {u}</span></span>
                <span className="pair"><span className="cap">PUE</span>
                  <span className="v"><Num value={head.pue} digits={3} /></span></span>
                <span className="pair"><span className="cap">Of rating</span>
                  <span className="v">
                    <Tip tip={head.rated_kw
                      ? `${head.rated_kw.toFixed(1)} kW rated: ${basisWords(head.rating_basis, head.racks_rated)}`
                      : 'no rating recorded'}>
                      <Num value={head.load_pct} unit="%" />
                    </Tip>
                  </span></span>
                <span className="pair"><span className="cap">Racks</span>
                  <span className="v">{head.racks_reporting ?? 0}<span className="muted">/{head.rack_count ?? 0}</span></span></span>
              </div>
            )}
          </div>
        )}

        <DataTable
          rows={t.visible}
          columns={columns}
          lead={severity}
          // Site -> its rooms, room -> its racks, rack -> its elevation with
          // the power overlay on.
          onRowClick={(r) => {
            if (r.kind === 'rack') {
              navigate(`/racks/${r.id}?overlay=power&from=power`);
              return;
            }
            t.drillInto(r);
          }}
          empty={isLoading ? 'Loading…'
            : error ? 'Could not load power data.'
            : stale ? 'That room is not in this payload any more. Pick one above.'
            : 'Nothing matches this search.'}
        />

        <TableFoot total={t.filtered.length} page={t.page} pageSize={t.pageSize}
                   noun={isFetching && data ? `${t.tier} · updating` : t.tier}
                   onPage={t.setPage} onPageSize={t.setPageSize} onCsv={exportCsv} />
      </div>

      {/* The charts follow the drill: the estate, then the site, then the
          room the table is showing. */}
      {scopeKnown && (() => {
        const chartScope = t.selectedRoom
          ? { kind: 'room' as const, id: t.selectedRoom.id, label: t.selectedRoom.name }
          : t.selected
            ? { kind: 'site' as const, id: t.selected.id, label: t.selected.name }
            : undefined;
        return (
          <>
            <PowerDraw scope={chartScope} range={trendRange} />
            <PowerPue scope={chartScope} range={trendRange} />
            <PowerLoad scope={chartScope} range={trendRange} />
          </>
        );
      })()}

      <Notes items={[
        ...(data?.notes ?? []),
        'The trends read the five-minute rollup for up to two days of hourly points and '
        + 'the hourly rollup beyond, with the newest two hours redrawn from the readings; '
        + 'a gap in a line is a period nothing reported.',
        // The one number that would otherwise look like an error: the header
        // totals the whole estate, the rows show white space.
        !t.includeFacility && !t.selectedRoom && data?.totals.facility?.rooms
          ? `The totals above include ${data.totals.facility.rooms} facility rooms `
            + `(plant, switchrooms, roof) drawing ${data.totals.facility.total_kw ?? 0} kW, `
            + `of which ${data.totals.facility.cooling_kw ?? 0} kW is cooling. `
            + 'Their rows are hidden; their load is not.'
          : '',
        head?.rated_kw
          ? `${head.name} is rated at ${head.rated_kw.toFixed(1)} kW IT: ${basisWords(head.rating_basis, head.racks_rated)}.`
          : '',
        unit === 'kva'
          ? `kVA is kW over a nominal ${NOMINAL_PF} power factor. Nothing on these rows meters apparent power; the switchgear, UPS and panel meters do, on their own pages.`
          : '',
        windowNote,
      ].filter(Boolean)} />

      {drawerRoom && (
        <RoomDrawer roomId={drawerRoom.id} roomName={drawerRoom.name}
                    onClose={() => setDrawerRoom(null)} />
      )}

      {drill && data && (
        <AlarmPanel
          categories={data.alarm_categories as AlarmCategory[]}
          title="Power conditions"
          scope={drill}
          onClose={() => setDrill(null)} />
      )}
    </div>
  );
}
