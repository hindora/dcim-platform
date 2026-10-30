import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import {
  api, type AlarmCategory, type PowerBank, type PowerFailover, type PowerFeeds, type PowerOutlets, type PowerRow,
  type PowerSpread,
} from '../../api/client';
import { AlarmPanel } from '../home/AlarmPanel';
import { RoomDrawer } from '../home/RoomDrawer';
import {
  Column, Crumb, Crumbs, DataTable, Delta, EmptyRacksToggle, FacilityToggle, Notes, Num, PageHead,
  ScopeTabs, Seg, TableFoot,
} from '../../components/estate';
import { Tip } from '../../components/HoverTip';
import { downloadCsv, stampedName } from '../../lib/csv';
import { useEstateTable } from './useEstateTable';
import { useTrendRange } from './ThermalTrend';
import { PowerDraw, PowerLoad, PowerPue } from './PowerTrend';
import { PowerSupply, useSupply } from './PowerSupply';
import { RoomPanels } from './RoomPanels';

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
function Feeds({ f, r, warnPct }: { f: PowerFeeds | null | undefined; r: PowerRow; warnPct: number }) {
  if (!f || !f.loads) return <Tip className="dash" tip="no IT or network load in this rack">—</Tip>;
  const legs = r.legs ?? [];
  const psuFailed = Array.isArray(r.psu_failed) ? r.psu_failed : [];
  const imb = r.imbalance_pct ?? null;
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
  const shared = f.shared_upstream ?? [];
  const tone = f.unfed || psuFailed.length ? 'critical'
    : f.single_cord || shared.length || (imb !== null && imb >= warnPct) ? 'warn' : undefined;
  const word = f.unfed ? `${f.unfed} unfed`
    : psuFailed.length ? `${psuFailed.length} on one PSU`
      : f.single_cord ? `${f.single_cord} single`
        : shared.length ? `${f.dual_cord} dual · shared`
          : imb !== null && imb >= warnPct ? `${f.dual_cord} dual · ${imb.toFixed(0)} %`
            : `${f.dual_cord} dual`;
  return (
    <Tip tip={<>{sides}<br />{cording}. Dual means two different sides; two cords into one strip is one feed.
      {shared.length ? <><br />Both sides hang off {shared.join(', ')}: above that point the two cords survive nothing.</> : null}
      {psuFailed.length ? <><br />Running on one supply: {psuFailed.join(', ')} - the other cord carries nothing until the part is swapped.</> : null}
      {legs.length ? legs.map((l) => (
        <span key={l.strip_id} className="spread-line">{l.strip}: {l.legs.map((a) => `${a.toFixed(1)} A`).join(' / ')}
          {l.imbalance_pct !== null ? ` · ${l.imbalance_pct.toFixed(0)} % imbalance` : ''}</span>
      )) : null}</>}>
      <span className={`pw-feeds ${tone ?? ''}`}>
        <span className="side">{f.a.length ? 'A' : '·'}</span>
        <span className="side">{f.b.length ? 'B' : '·'}</span>
        <span className="n">{word}</span>
      </span>
    </Tip>
  );
}

/** What the surviving side carries if the other is lost. The level that
 *  binds is named, because the fix differs: a strip over its rating needs a
 *  bigger strip, a leg or a bank over its breaker needs cords moved. */
function Failover({ f }: { f: PowerFailover | null | undefined }) {
  if (!f) {
    return <Tip className="dash" tip="Not fed from both sides, or a side is not reporting a draw: there is no survivor to judge.">—</Tip>;
  }
  const tone = f.pct >= 100 ? 'critical' : f.band === 'warn' || f.band === 'critical' ? 'warn' : undefined;
  const level = f.basis === 'strip' ? 'strip' : f.basis === 'leg' ? 'leg' : 'bank';
  return (
    <Tip tip={<>Lose side {f.lost_side}: {f.survivor} carries {f.detail} - {f.pct.toFixed(0)} % of what it is rated for.
      {f.pct >= 100 ? ' It trips, and the rack goes dark with it.' : ''}
      <br />The rack draws {f.total_kw.toFixed(1)} kW across both sides. Strip, legs and banks are each judged; this is the one that binds first.
      {f.mirrored ? <><br />Assumes each load's two cords sit in the same outlet position on each strip, as racks are cabled.</> : null}
      {f.basis === 'strip' ? <><br />Assumes every load is dual-corded; a load on the lost side alone goes dark instead of moving.</> : null}</>}>
      <span className={`pw-failover ${tone ?? ''}`}>
        <span className="v">{f.pct.toFixed(0)} %</span>
        <span className="lv">{level}</span>
      </span>
    </Tip>
  );
}

/** The busiest bank against its breaker, now. */
function WorstBank({ banks, warnPct }: { banks: PowerBank[] | undefined; warnPct: number }) {
  if (!banks || !banks.length) {
    return <Tip className="dash" tip="No bank on this rack's strips reports a breaker - a 16 A strip has none - or the strips do not report their banks.">—</Tip>;
  }
  const w = banks[0];
  const tone = w.pct >= 100 ? 'critical' : w.pct >= warnPct ? 'warn' : undefined;
  return (
    <Tip tip={<>{banks.map((b) => (
      <span key={`${b.strip}:${b.bank}`} className="spread-line">
        {b.strip} bank {b.bank}: {b.amps.toFixed(1)} A of {b.breaker_a.toFixed(0)} A ({b.pct.toFixed(0)} %)</span>
    ))}Amber from {warnPct.toFixed(0)} % of the breaker, the continuous-load line; the bank breaker trips long before the strip's inlet does.</>}>
      <span className={tone}><Num value={w.pct} /> %</span>
    </Tip>
  );
}

/** The busiest metered receptacles, where a strip has a CT behind them. */
function Outlets({ o, warnPct }: { o: PowerOutlets | null | undefined; warnPct: number }) {
  if (!o) {
    return <Tip className="dash" tip="No strip on this rack meters its outlets. A Metered strip reads its inlet, legs and banks; a Switched one (an AP8953) has a relay per outlet and no current transformer. Per-outlet current needs a metered-by-outlet unit.">—</Tip>;
  }
  const top = o.rows[0];
  const cls = top && top.pct >= 100 ? 'critical' : top && top.pct >= warnPct ? 'warn' : undefined;
  return (
    <Tip tip={<>{o.rows.map((r) => (
      <span key={`${r.strip}:${r.outlet}`} className="spread-line">
        {r.strip} outlet {r.outlet}: {r.amps.toFixed(2)} A{r.load ? ` · ${r.load}` : ' · nothing corded'}</span>
    ))}{o.more ? <span className="spread-line">and {o.more} more</span> : null}
      {o.idle ? <span className="spread-line">{o.idle} idle receptacle{o.idle === 1 ? '' : 's'} at 0 A</span> : null}
      {o.strips_metered < o.strips ? <span className="spread-line">{o.strips - o.strips_metered} of {o.strips} strips here cannot meter per outlet</span> : null}
      Live current per receptacle, from the strip's own outlet meters. Amber from {warnPct.toFixed(0)} % of a C13's 10 A, the continuous-load line.</>}>
      <span className={cls}>{top ? `${top.amps.toFixed(1)} A` : '0 A'}</span>
      <span className="muted"> · {o.rows.length + o.more}{o.hot ? ` · ${o.hot} hot` : ''}</span>
    </Tip>
  );
}

/** Rooms and sites: how many racks fail either check. */
function FeedRisk({ r }: { r: PowerRow }) {
  const fo = r.racks_failover_over ?? 0;
  const bk = r.racks_bank_hot ?? 0;
  if (!fo && !bk) return <Tip className="dash" tip="Every dual-fed rack here survives losing a side, and no bank is past 80 % of its breaker.">—</Tip>;
  const parts = [fo ? `${fo} failover` : null, bk ? `${bk} bank` : null].filter(Boolean).join(' · ');
  return (
    <Tip tip={<>{fo ? <>{fo} rack{fo === 1 ? '' : 's'} where the surviving side would carry past the warn line if the other were lost.<br /></> : null}
      {bk ? <>{bk} rack{bk === 1 ? '' : 's'} with a bank past 80 % of its breaker now.</> : null}</>}>
      <span className="warn">{parts}</span>
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
  const [params, setParams] = useSearchParams();
  const trendRange = useTrendRange();
  // SUPPLY is a different population, not a coarser view of the same rows,
  // so it is a tab beside the tiers rather than a tier (as Thermal's PLANT).
  const supplyTab = params.get('scope') === 'supply';
  const supplySite = params.get('site');
  const supply = useSupply(supplySite, supplyTab);
  const sh = supply.data?.headline;

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
  // The runway for the scope the reader is in, from the forecast service:
  // the daily p95 projected against the scope's rating. One scope at a
  // time, because a projection per row is ninety days of history per row.
  const scoped = t.selectedRoom ?? t.selected ?? null;
  const runway = useQuery({
    queryKey: ['power-runway', scoped?.kind ?? '', scoped?.id ?? '', scoped?.rated_kw ?? 0],
    queryFn: () => api.forecast(scoped!.kind === 'site' ? 'datacenter' : 'room', scoped!.id,
                                { metric: 'it_power', capacity: scoped!.rated_kw ?? undefined }),
    enabled: Boolean(scoped && scoped.rated_kw),
    staleTime: 300_000,
    placeholderData: (prev) => prev,
  });
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
    const psuFailed = Array.isArray(r.psu_failed) ? r.psu_failed.length : 0;
    const fo = r.failover?.pct ?? 0;
    const bank = r.bank_worst_pct ?? 0;
    if (r.band === 'critical' || (r.feeds?.unfed ?? 0) > 0 || psuFailed > 0
        || bank >= 100 || fo >= 100) return 'critical';
    if (r.band === 'warn' || (r.feeds?.single_cord ?? 0) > 0
        || (r.imbalance_pct ?? 0) >= (data?.totals.leg_imbalance_warn_pct ?? 10)
        || fo >= bands.warn_pct || bank >= (data?.totals.bank_warn_pct ?? 80)
        || (r.racks_failover_over ?? 0) > 0 || (r.racks_bank_hot ?? 0) > 0) return 'warn';
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
        ? `The draw the rack's servers and switches report, ${statWord}. A load that publishes no draw of its own is read at the outlet its cord lands on, where the strip meters per outlet, and the tip says how many were. The strip's own meter, on the rack page, reads a few percent above it.`
        : `Everything metered in this scope, ${statWord}: IT, cooling and the rest. Rack PDUs are excluded so the servers behind them are not counted twice.`,
      sort: (r) => r.total_kw,
      render: (r) => (
        <Tip tip={r.total_kw === null ? (r.note ?? 'nothing here reports power')
          : <>{r.total_kw.toFixed(1)} kW {statWord}{unitTip}
              {r.measured_at_strip ? <><br />{r.measured_at_strip} of {r.loads} load{r.loads === 1 ? '' : 's'} measured at the strip's outlets - the load publishes no draw of its own.</> : null}</>}>
          <Num value={conv(r.total_kw)} why={r.note} />
          {r.measured_at_strip ? <span className="muted"> ·strip</span> : null}
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
    ...(rackTier ? [{
      key: 'stranded', label: `Stranded ${u}`, align: 'num' as const, width: 104,
      help: `Allocated minus the ${data?.totals.p95_days ?? 7}-day p95 of the draw: budgeted for this rack and not being drawn. Where the next server fits without a new feed.`,
      sort: (r: PowerRow) => r.stranded_kw ?? null,
      render: (r: PowerRow) => (
        <Tip tip={r.stranded_kw == null
          ? (r.allocated_kw == null ? 'nothing in this rack carries a budget' : 'no draw in the last week to measure against')
          : <>{r.allocated_kw?.toFixed(1)} kW allocated, p95 {r.p95_kw?.toFixed(1)} kW over {data?.totals.p95_days ?? 7} days{unitTip}</>}>
          <Num value={conv(r.stranded_kw)} />
        </Tip>
      ),
    }] : [{
      key: 'headroom', label: `Headroom ${u}`, align: 'num' as const, width: 108,
      help: `Rating minus the ${data?.totals.p95_days ?? 7}-day p95 of the coincident IT draw: the margin a planner has, not the margin this minute has. Negative is a scope already over its rating at the 95th percentile.`,
      sort: (r: PowerRow) => r.headroom_kw ?? null,
      render: (r: PowerRow) => (
        <Tip tip={r.headroom_kw == null
          ? (r.rated_kw ? 'no draw in the last week to measure against' : 'no rating recorded for this row')
          : <>{r.rated_kw?.toFixed(1)} kW rated, p95 {r.p95_kw?.toFixed(1)} kW over {data?.totals.p95_days ?? 7} days
              {r.stranded_kw != null ? <> · {r.stranded_kw.toFixed(1)} kW stranded in its racks</> : null}{unitTip}</>}>
          <span className={(r.headroom_kw ?? 0) < 0 ? 'critical' : undefined}>
            <Num value={conv(r.headroom_kw)} />
          </span>
        </Tip>
      ),
    }]),
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
      help: 'The strips feeding this rack\'s loads, by side, and whether every load has a cord on each. One cord is one maintenance window away from dark; a load on one supply is the same with the cord still plugged in. The tip carries each strip\'s leg currents and their imbalance.',
      sort: (r: PowerRow) => (r.feeds ? r.feeds.unfed * 1000 + r.feeds.single_cord : null),
      render: (r: PowerRow) => <Feeds f={r.feeds} r={r} warnPct={data?.totals.leg_imbalance_warn_pct ?? 10} />,
    }, {
      key: 'failover', label: 'On failover', align: 'num' as const, width: 112,
      help: 'If one side is lost, what the other carries: the whole rack on the surviving strip, its legs and its banks, each against its own rating. The worst of the three, and which one it is. Past 100 % the survivor trips and the rack goes dark - the failure 2N cabling was meant to survive.',
      sort: (r: PowerRow) => r.failover?.pct ?? null,
      render: (r: PowerRow) => <Failover f={r.failover} />,
    }, {
      key: 'bank', label: 'Worst bank', align: 'num' as const, width: 104,
      help: 'The busiest outlet bank against its breaker, now. A strip\'s outlets are wired in banks, each behind its own breaker; a 32 A strip is several 16 A banks, and one bank trips while the strip reads half loaded.',
      sort: (r: PowerRow) => r.bank_worst_pct ?? null,
      render: (r: PowerRow) => <WorstBank banks={r.banks} warnPct={data?.totals.bank_warn_pct ?? 80} />,
    }, {
      key: 'outlets', label: 'Outlets', align: 'num' as const, width: 108,
      help: 'The busiest receptacle on this rack\'s strips and how many carry a load, where a strip meters per outlet. Only a metered-by-outlet unit has a current transformer behind each receptacle; a Metered or Switched strip reports banks at best, and shows a dash here. The tip names what is corded to each.',
      sort: (r: PowerRow) => r.outlets?.max_a ?? null,
      render: (r: PowerRow) => <Outlets o={r.outlets} warnPct={data?.totals.outlet_warn_pct ?? 80} />,
    }] : [{
      key: 'risk', label: 'Feed risk', align: 'mid' as const, width: 120,
      help: 'Racks here whose surviving side would run past the warn line if the other were lost, and racks with a bank past 80 % of its breaker now. Drill in to see which.',
      sort: (r: PowerRow) => (r.racks_failover_over ?? 0) * 1000 + (r.racks_bank_hot ?? 0),
      render: (r: PowerRow) => <FeedRisk r={r} />,
    }, {
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
          ? 'Loads with a reading now, over the loads a site meters one by one: IT and network gear, and the cooling units with a motor. Sensors, valves, gateways and power gear draw from a panel metered upstream and are not counted.'
          : 'Racks with a reading in this window, over racks with something in them.',
      sort: (r) => (typeof r.reporting === 'number' ? r.reporting : r.racks_reporting ?? null),
      render: (r) => {
        if (r.kind === 'rack') {
          if (!r.loads) return <Tip className="dash" tip="no IT or network device in this rack">—</Tip>;
          return typeof r.reporting === 'number'
            ? <Tip tip={`${r.reporting} of ${r.loads} loads reporting${r.measured_at_strip ? `, ${r.measured_at_strip} of them at the strip's outlets` : ''}`}>{r.reporting}<span className="muted">/{r.loads}</span></Tip>
            : <Tip tip={`${r.loads} loads in the rack`}>{r.loads}</Tip>;
        }
        // Empty cabinets have nothing to report, so they are not in the count.
        const racks = r.racks_with_loads ?? r.rack_count ?? 0;
        const dev = typeof r.reporting === 'object' && r.reporting ? r.reporting : null;
        if (!racks && !dev) {
          return <Tip className="dash" tip="Nothing here is metered on its own: its gear draws from a panel that is metered upstream">—</Tip>;
        }
        return (
          <Tip tip={<>{dev ? <>{dev.reporting} of {dev.devices} metered loads reporting<br /></> : null}
                       {racks ? `${r.racks_reporting ?? 0} of ${racks} racks with a reading` : 'no racks with loads here'}</>}>
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
          <Link to="/power?scope=supply">The feed: supplies and redundancy →</Link></>}
        kpis={supplyTab ? [
          { caption: 'Utility', value: sh ? `${sh.utility.healthy}/${sh.utility.total}` : null,
            tone: sh && sh.utility.healthy < sh.utility.total ? 'critical' : 'ok',
            why: 'no incoming feed in this scope' },
          { caption: 'On generator', value: sh ? `${sh.generators.running}/${sh.generators.total}` : null,
            tone: sh && (sh.generators.running > 0 || sh.ats_on_emergency > 0) ? 'warn' : 'ok',
            why: 'no generator in this scope' },
          { caption: 'UPS load', value: sh?.ups.load_max_pct ?? null, unit: '%',
            tone: (sh?.ups.load_max_pct ?? 0) >= 90 ? 'critical' : (sh?.ups.load_max_pct ?? 0) >= 80 ? 'warn' : 'ok',
            why: 'no UPS in this scope reports a load' },
          { caption: 'UPS runtime', value: sh?.ups.runtime_min_minutes ?? null, unit: 'min', digits: 0,
            tone: sh && (sh.ups.on_battery > 0 || (sh.ups.runtime_min_minutes ?? 999) < (sh.ups_runtime_floor_min ?? 5)) ? 'critical' : 'ok',
            tip: <>The shortest runtime any UPS here reports at its present load, against the{' '}
              {sh?.genset_window_s ?? 15} s it takes the generator to carry the load (NFPA 110 Type 10 start,
              transfer, and the UPS re-qualifying the source). {sh?.ups_runtime_floor_min ?? 5} min is the floor:
              a failed crank or a retried transfer eats minutes, not seconds.</>,
            why: 'no UPS in this scope reports a runtime' },
          { caption: 'On one leg', value: sh?.on_one_leg ?? null, digits: 0,
            tone: (sh?.on_one_leg ?? 0) > 0 ? 'warn' : 'ok',
            why: 'no IT load is fed in this scope' },
          { caption: 'Imbalance', value: sh?.imbalance_worst?.pct ?? null, unit: '%',
            tone: (sh?.imbalance_worst?.pct ?? 0) > 3 ? 'warn' : 'ok',
            why: 'nothing in this scope reports phase figures' },
        ] : [
          { caption: 'Total', value: conv(totals?.total_kw ?? null), unit: u,
            why: 'nothing in the estate reports power' },
          { caption: 'IT', value: conv(totals?.it_ac_kw ?? null), unit: u,
            why: 'no server, storage or network device reports power' },
          { caption: 'Cooling', value: conv(totals?.cooling_kw ?? null), unit: u,
            why: 'no cooling unit reports power' },
          // The Green Grid figure where the meters allow it - kWh over kWh,
          // with its category - and the draw ratio only where they do not.
          // The two disagreed on one screen for a month; now the ratio is the
          // tooltip under the energy figure, never a second headline.
          ...(totals?.energy_pue?.pue != null && totals.energy_pue.method === 'energy' ? [{
            caption: 'PUE', value: totals.energy_pue.pue, digits: 3,
            unit: `L${totals.energy_pue.category ?? 1}`,
            tone: (!totals.energy_pue.plausible ? 'warn'
              : totals.energy_pue.pue > bands.pue_warn ? 'warn' : 'ok') as 'ok' | 'warn' | 'critical',
            tip: <>Energy-based, Green Grid category {totals.energy_pue.category ?? 1}: {totals.energy_pue.total_facility_kwh?.toFixed(0)} kWh facility over {totals.energy_pue.it_kwh?.toFixed(0)} kWh at the UPS output, last {totals.energy_pue.hours.toFixed(0)} h.
              {totals.pue != null ? <> Draw ratio {statWord}: {totals.pue.toFixed(3)}.</> : null}
              {totals.energy_pue.note ? <> {totals.energy_pue.note}.</> : null}</>,
          }] : [{
            caption: 'PUE', value: totals?.pue ?? null, digits: 3, unit: 'ratio',
            tone: ((totals?.pue ?? 0) > bands.pue_warn ? 'warn' : 'ok') as 'ok' | 'warn' | 'critical',
            tip: <>A ratio of draw, total over IT, {statWord}. {totals?.energy_pue?.note ? `No energy figure: ${totals.energy_pue.note}.` : 'No energy counters answered for this window.'}</>,
            why: 'no IT load reported, so there is nothing to divide by',
          }]),
          { caption: 'Of rating', value: totals?.load_pct ?? null, unit: '%',
            tone: totals?.band === 'critical' ? 'critical' : totals?.band === 'warn' ? 'warn' : 'ok',
            why: 'no room or rack carries a rating' },
          { caption: 'Halls reporting',
            value: totals ? `${totals.halls_reporting}/${totals.halls}` : null,
            tone: totals && totals.halls_reporting < totals.halls ? 'warn' : 'ok' },
        ]}
      />

      <div className="estate-tools">
        <ScopeTabs<'sites' | 'rooms' | 'supply'>
          scope={supplyTab ? 'supply' : t.scope}
          tabs={[{ key: 'sites', label: 'SITES' },
                 { key: 'rooms', label: 'ROOMS' },
                 { key: 'supply', label: 'SUPPLY' }]}
          onChange={(k) => {
            if (k === 'supply') { setParams({ scope: 'supply' }); return; }
            t.setScope(k);
          }} />
        {!supplyTab && (t.scope === 'rooms' || t.selected) && !t.selectedRoom && (
          <FacilityToggle on={t.includeFacility} count={t.facilityCount}
                          onChange={t.setIncludeFacility} />
        )}
        {!supplyTab && t.selectedRoom && (
          <EmptyRacksToggle on={t.includeEmpty} count={t.emptyCount}
                            onChange={t.setIncludeEmpty} />
        )}
        {!supplyTab && (<>
        <input className="grow" type="search" placeholder="Search sites, rooms and racks"
               aria-label="Search" value={t.search}
               onChange={(e) => t.setSearch(e.target.value)} />
        <Seg label="Unit" value={unit} onChange={setUnit}
             options={[{ key: 'kw', label: 'kW' }, { key: 'kva', label: 'kVA' }]} />
        <Seg label="Window" value={win} onChange={setWin}
             options={[{ key: 'now', label: 'NOW' },
                       { key: 'hour', label: 'LAST HOUR' },
                       { key: 'day', label: 'BY DAY' }]} />
        </>)}
        {!supplyTab && win !== 'now' && (
          <Seg label="Statistic" value={stat} onChange={setStat}
               options={[{ key: 'average', label: 'AVERAGE' },
                         { key: 'peak', label: 'PEAK' }]} />
        )}
        {!supplyTab && win === 'day' && (
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

      {supplyTab ? (
        <PowerSupply siteId={supplySite}
                     sites={supply.data?.sites ?? (data?.sites ?? []).map((s) => ({ id: s.id, code: s.site_code }))}
                     onSite={(id) => setParams(id ? { scope: 'supply', site: id } : { scope: 'supply' })} />
      ) : (<>
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
                <span className="pair"><span className="cap">Headroom</span>
                  <span className="v">
                    <Tip tip={head.headroom_kw == null ? 'needs a rating and a week of draw'
                      : `${head.rated_kw?.toFixed(1)} kW rated minus p95 ${head.p95_kw?.toFixed(1)} kW over ${data?.totals.p95_days ?? 7} days`}>
                      <span className={(head.headroom_kw ?? 0) < 0 ? 'critical' : undefined}><Num value={conv(head.headroom_kw)} /></span>
                    </Tip> {u}</span></span>
                <span className="pair"><span className="cap">Stranded</span>
                  <span className="v">
                    <Tip tip={head.stranded_kw == null ? 'no rack here carries a budget and a week of draw'
                      : head.stranded_top?.length
                        ? <>{head.stranded_top.map((x) => <span key={x.name} className="spread-line"><b>{x.kw.toFixed(1)} kW</b> {x.name}</span>)}</>
                        : 'allocated minus p95, summed over the rooms'}>
                      <Num value={conv(head.stranded_kw)} />
                    </Tip> {u}</span></span>
                <span className="pair"><span className="cap">Runway</span>
                  <span className="v">
                    {!head.rated_kw ? <Tip className="dash" tip="no rating to run out of">—</Tip>
                      : runway.isLoading ? <span className="muted">…</span>
                      : runway.error || !runway.data ? <Tip className="dash" tip="the forecast did not answer">—</Tip>
                      : runway.data.method === 'insufficient_history'
                        ? <Tip className="dash" tip={runway.data.method_reason}>—</Tip>
                        : runway.data.runway.days == null
                          ? <Tip tip={runway.data.runway.reason}><span className="muted">none in {runway.data.points.length} d</span></Tip>
                          : <Tip tip={<>{runway.data.runway.reason}. {runway.data.method} fit over {runway.data.history_days} days{runway.data.trend_per_day != null ? <>, {(runway.data.trend_per_day * 7).toFixed(2)} kW/week</> : null}
                              {runway.data.runway.earliest_days != null ? <>; between {runway.data.runway.earliest_days} and {runway.data.runway.latest_days} days</> : null}</>}>
                              <span className={runway.data.runway.days < 90 ? 'warn' : undefined}>{runway.data.runway.days} d</span>
                            </Tip>}
                  </span></span>
              </div>
            )}
          </div>
        )}

        <DataTable
          rows={t.visible}
          columns={columns}
          lead={severity}
          // Site -> its rooms, room -> its racks, rack -> its page, open on
          // the power question this table was asking.
          onRowClick={(r) => {
            if (r.kind === 'rack') {
              navigate(`/racks/${r.id}?tab=power&from=power`);
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

      {/* Drilled into a room, its power panels: the two RPPs, each read through
          the meter clamped on it - the branch circuits and the phase balance
          that the rack table above cannot show. */}
      {t.selectedRoom && (
        <RoomPanels roomId={t.selectedRoom.id} roomName={t.selectedRoom.name} />
      )}

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
      </>)}

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
