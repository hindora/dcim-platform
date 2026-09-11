import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { api, type AlarmCategory, type ThermalRow, type ThermalSpread } from '../../api/client';
import { AlarmPanel } from '../home/AlarmPanel';
import {
  Column, type Crumb, Crumbs, DataTable, Delta, Notes, Num, PageHead, ScopeTabs,
  Seg, TableFoot, tone,
} from '../../components/estate';
import { Tip } from '../../components/HoverTip';
import { downloadCsv, stampedName } from '../../lib/csv';
import { RoomDrawer } from '../home/RoomDrawer';
import { useEstateTable } from './useEstateTable';
import { ThermalTrend } from './ThermalTrend';
import { RoomCooling } from './RoomCooling';
import { Plant, usePlant, verdictLabel, verdictTone } from './Plant';
import { Facility } from './Facility';

/** Thermal: how warm the estate is running, and how much of it is in band.
 *
 *  Compliance is the share of intake READINGS inside the ASHRAE recommended
 *  envelope, not the share of racks. A rack polled every ten seconds and one
 *  polled hourly are not equally strong evidence, and weighting them equally
 *  would let a quiet sensor outvote a busy one.
 *
 *  Relative humidity comes from the rack PDU environment probes and is shown
 *  beside compliance, not folded into it. There is no composite score: it
 *  would be a number we invented sitting beside five that were measured.
 *
 *  Three windows, and they answer different questions. NOW is the newest
 *  reading from each sensor, counting one reading per SENSOR: it is what to
 *  watch while something is happening, because an hour's mean needs an hour
 *  to show a step change and holds it for an hour after it clears. LAST HOUR
 *  and BY DAY count every reading over a window, which is what compliance
 *  means and what a quiet floor should be read on.
 *
 *  Two ceilings, two tones. Above the recommended band (27 C) a row is warn;
 *  above the allowable ceiling (32 C) it is critical. Those are the same two
 *  lines the inlet temperature alarm rules draw, so the table and the alarm
 *  list never disagree about how bad a number is.
 *
 *  Intake per rack comes from the rack's front environment probe where one
 *  reported, else from the servers' BMC inlet, and each row says which. That
 *  is the order a DCIM uses: the probe is the cold aisle at the rack face,
 *  the BMC is behind the bezel and blind in a rack with no servers. Rooms
 *  and sites are the racks added up, so the tiers cannot disagree.
 *
 *  Average and Max hide the most common real finding, overcooling, so each
 *  row also carries the 90th percentile of its readings and a four-way
 *  spread against the ASHRAE lines: below the recommended floor, in band,
 *  above it but allowable, above the allowable ceiling. The below share is
 *  printed on its own because it is the evidence a site raises a setpoint
 *  on. p90 is taken over the row's pooled readings in one query per tier,
 *  never summarised from the tier below.
 *
 *  Under the table, the same readings over time for whatever the table is
 *  showing - the estate, a site, a room - as average, p90 and max against
 *  the ASHRAE band, with the range control every other trend wears. The Δ
 *  columns say "warmer than yesterday"; the line says since when.
 *
 *  NOW has no yesterday, so it carries a RATE instead: the same sensors read
 *  again fifteen minutes ago, in K per hour, on the tip beside the average.
 *  It had a column of its own and lost it, deliberately - with this much
 *  cooling headroom a hall barely moves, so the column said "flat" on every
 *  row on every visit, and a column people learn to skip costs something
 *  every day to pay for the rare hour it earns. The measurement is kept,
 *  because it is the one figure that says whether there are twenty minutes
 *  or two, and because a denser floor would want it back.
 *
 *  Warm and warm-with-somebody-told are different situations, so each row
 *  carries its open thermal conditions and a site or a room opens the same
 *  drill-down the home page uses. The count and the panel take one category
 *  list from the server, so the number cannot disagree with the rows behind
 *  it. A rack row shows its count and goes to its elevation on click, which
 *  is where its story continues; the panel groups by room and could not
 *  answer for one rack without saying something it does not know.
 *
 *  Three tiers: sites, rooms, racks. A rack is where an engineer actually
 *  goes, so the table does not stop one level short of it. Rack rows add
 *  exhaust, ΔT and the sensor count, and a rack row opens its elevation with
 *  the thermal overlay already on. The room drawer stays one click away from
 *  the drilled-in header.
 */

type Unit = 'c' | 'f';

function conv(c: number | null, unit: Unit): number | null {
  if (c === null || c === undefined) return null;
  return unit === 'c' ? c : c * 9 / 5 + 32;
}

/** A DIFFERENCE in temperature scales by 9/5 with no offset.
 *
 *  Running a delta through the absolute conversion adds 32 to a change, which
 *  turns "half a degree warmer than yesterday" into "32.9 degrees warmer".
 */
function convDelta(c: number | null, unit: Unit): number | null {
  if (c === null || c === undefined) return null;
  return unit === 'c' ? c : c * 9 / 5;
}

/** The four-way split as one partition bar, with the figures in the tip.
 *
 *  Hue is state here: cool for overcooled, ok for in band, warn and
 *  critical for the two ceilings - the same two lines the row's lead
 *  colour and the inlet alarm rules use. Zero-width segments are simply
 *  absent; the bar is a partition, not four bars.
 */
function Spread({ d, low, high, allowable }: {
  d: ThermalSpread | null | undefined; low: number; high: number; allowable: number;
}) {
  if (!d) return <Tip className="dash" tip="no intake readings in this window">—</Tip>;
  const segs: Array<[string, number, string]> = [
    ['cool', d.below_pct, `below ${low} °C (overcooled)`],
    ['ok', d.in_band_pct, `${low}–${high} °C recommended`],
    ['warn', d.above_recommended_pct, `${high}–${allowable} °C allowable`],
    ['critical', d.above_allowable_pct, `above ${allowable} °C`],
  ];
  return (
    <Tip tip={<>{segs.map(([k, v, label]) => (
      <span key={k} className="spread-line"><b>{v.toFixed(1)}%</b> {label}</span>
    ))}</>}>
      <span className="stack-bar cell" role="img"
            aria-label={`${d.below_pct}% overcooled, ${d.in_band_pct}% in band, ${d.above_recommended_pct}% above recommended, ${d.above_allowable_pct}% above allowable`}>
        {segs.filter(([, v]) => v > 0).map(([k, v]) => (
          <span key={k} className={`stack-seg ${k}`} style={{ width: `${v}%` }} />
        ))}
      </span>
    </Tip>
  );
}

/** A room's cooling units, in one cell.
 *
 *  The count is the cell; the diagnosis is the tip. Supply and return are the
 *  page's own thesis - a high SUPPLY is the unit failing to make cold air, a
 *  high RETURN is the room feeding it hot air, and they send an engineer to
 *  opposite ends of the building - but they are two temperatures and a delta,
 *  which is three numbers more than a column can hold beside fifteen others.
 *
 *  Toned by the worst thing among them, so a hall that needs attention is
 *  visible without reading the number: stopped or discharging warm is
 *  critical, a hot return is warn, everything behaving is quiet.
 */
function Cooling({ c, unit }: {
  c: ThermalRow['cooling']; unit: Unit;
}) {
  if (!c) {
    return <Tip className="dash" tip="no cooling unit reports into this room">—</Tip>;
  }
  const bad = c.units_stopped + c.units_high_supply;
  const tone = bad ? 'critical' : c.units_high_return ? 'warn' : undefined;
  const deg = (v: number | null) => (v === null ? '—' : conv(v, unit)?.toFixed(1));
  return (
    <Tip tip={<>
      <span className="spread-line"><b>{c.units}</b> unit{c.units === 1 ? '' : 's'}
        {c.units_stopped ? <>, <b>{c.units_stopped}</b> stopped</> : null}
        {c.units_high_supply ? <>, <b>{c.units_high_supply}</b> discharging warm</> : null}
        {c.units_high_return ? <>, <b>{c.units_high_return}</b> on hot return</> : null}
        {!bad && !c.units_high_return ? ', all behaving' : null}</span>
      {c.supply_c !== null && (
        <span className="spread-line">supply <b>{deg(c.supply_c)}</b>, return <b>{deg(c.return_c)}</b>
          {c.delta_t_k !== null ? <> · ΔT <b>{convDelta(c.delta_t_k, unit)?.toFixed(1)}</b></> : null}</span>
      )}
      {c.supply_c === null && (
        <span className="spread-line">no running unit is reporting air temperatures</span>
      )}
    </>}>
      <span className={tone}>{c.units}{bad || c.units_high_return
        ? ` · ${bad + c.units_high_return}` : ''}</span>
    </Tip>
  );
}

/** What spoke for a rack, in the words an operator would use.
 *
 *  Ordered the way the server picks: a probe measures the cold aisle at the
 *  rack face, which is what ASHRAE means by intake; a BMC sits behind the
 *  bezel; a switch's front panel is behind it AND next to the ASIC. Each is
 *  named on the row because a reading is only as good as where it was taken.
 */
const SOURCE_TIP: Record<string, string> = {
  probes: 'the rack\'s front environment probe: the cold aisle at the rack face, which is what ASHRAE calls intake',
  servers: 'server BMC inlet sensors, used because no rack probe reported; behind the bezel and a degree or two warm',
  network: 'the front-panel sensor on this rack\'s switches, used because it has no probe and no servers - a spine or management rack. It reads warm, but it is what this rack has instead of nothing',
};

const SOURCE_WORD: Record<string, (n: number) => string> = {
  probes: (n) => (n === 1 ? 'probe' : 'probes'),
  servers: (n) => (n === 1 ? 'server' : 'servers'),
  network: (n) => (n === 1 ? 'switch' : 'switches'),
};

export function Thermal() {
  const [mode, setMode] = useState<'daily' | 'live' | 'now'>('now');
  // Which sensor speaks for a rack. AUTO is the platform's own order - probe,
  // then servers, then the switches' front panels - and is right every day.
  // Pinning answers the question it cannot: does this hall read the same by
  // probe as it does by BMC? There is no "both", because averaging two
  // sources gives a figure that is neither.
  const [source, setSource] = useState<'auto' | 'probes' | 'servers'>('auto');
  const [unit, setUnit] = useState<Unit>('c');
  const [focus, setFocus] = useState<string>('');
  const [compare, setCompare] = useState<string>('');
  const [drawerRoom, setDrawerRoom] = useState<{ id: string; name: string } | null>(null);
  const [drill, setDrill] = useState<{ kind: 'site' | 'room'; id: string; label: string } | null>(null);
  const navigate = useNavigate();

  // PLANT is a third tab rather than a third drill level, because it is a
  // different population: the machines that hold the air where it is, none of
  // which is in a rack and most of which stand in rooms that have no rack
  // intake sensor at all. Those rooms could only ever appear in the table
  // above as a row of dashes.
  const [params, setParams] = useSearchParams();
  const plantTab = params.get('scope') === 'plant';
  // A facility room is a drill like any other: opening one replaces the page
  // below the header, rather than appending to it. It used to leave the halls
  // table and the intake chart on screen underneath, so "open the UPS room"
  // produced a screen showing the UPS room AND two server halls AND a trend of
  // rack air the room has none of.
  const facilityRoom = params.get('froom');
  // Enabled for the facility drill too: the trail has to name the room, and
  // the room's name lives in this payload. Same cache entry the table reads,
  // so it costs one request either way.
  const plant = usePlant(plantTab || !!facilityRoom);
  const pt = plant.data?.totals;
  const froom = facilityRoom
    ? plant.data?.facility_rooms.find((r) => r.id === facilityRoom) ?? null
    : null;

  const { data, isLoading, error } = useQuery({
    queryKey: ['estate-thermal', mode, focus, compare, source],
    queryFn: () => api.estateThermal({
      mode,
      focus: mode === 'daily' ? (focus || undefined) : undefined,
      compare: mode === 'daily' ? (compare || undefined) : undefined,
      source: source === 'auto' ? undefined : source,
    }),
    // An instant is worth re-asking often; the probes behind it are polled
    // every two to four minutes, so half a minute keeps the page ahead of
    // them without asking for the same answer twice.
    refetchInterval: mode === 'now' ? 30_000 : mode === 'live' ? 60_000 : false,
  });

  const t = useEstateTable<ThermalRow>(data?.sites ?? [], data?.rooms ?? [], data?.racks ?? []);
  const rackTier = t.tier === 'racks';
  const u = unit === 'c' ? '°C' : '°F';
  const floor = data?.band.low_c ?? 18;
  const recommended = data?.band.high_c ?? 27;
  const allowable = data?.band.allowable_high_c ?? 32;
  // The rate half of the same guidance. The page grades against the server's
  // numbers rather than a second copy that can drift from them.
  const rateLimit = data?.band.rate_limit_k_per_h ?? 20;
  const rateNoise = data?.band.rate_noise_k_per_h ?? 2;
  const rateMins = data?.band.rate_window_minutes ?? null;
  const rhHigh = data?.band.rh_high_pct ?? 60;

  /** warn above recommended, critical above allowable, else by in-band share. */
  function severity(r: ThermalRow): 'ok' | 'warn' | 'critical' | 'none' {
    if (r.avg_c === null) return 'none';
    if (r.max_c !== null && r.max_c > allowable) return 'critical';
    if (r.max_c !== null && r.max_c > recommended) return 'warn';
    return tone(r.compliance_pct === null ? null : 100 - r.compliance_pct);
  }

  const tierLabel = { sites: 'Site', rooms: 'Room', racks: 'Rack' }[t.tier];
  /** Which way this row is going, in words, or nothing to say.
   *
   *  NOW only: the other windows compare whole days or hours, which is a real
   *  change and a fictional rate. Silent below the noise floor, because the
   *  sensors cannot tell a slow drift from their own jitter and a tip that
   *  always says something is one nobody reads.
   */
  function rateTip(r: ThermalRow): string | null {
    if (mode !== 'now' || r.rate_k_per_h === null || r.rate_k_per_h === undefined) {
      return null;
    }
    const v = r.rate_k_per_h;
    if (Math.abs(v) < rateNoise) return `steady over the last ${rateMins ?? 15} minutes`;
    const dir = v > 0 ? 'rising' : 'falling';
    const past = Math.abs(v) >= rateLimit
      ? `, past the ${rateLimit} K/h ASHRAE limit on rate of change` : '';
    return `${dir} ${Math.abs(v).toFixed(1)} K/h${past}`;
  }

  const columns: Column<ThermalRow>[] = [
    {
      key: 'name', label: tierLabel,
      help: 'A site, a room or a rack. Click a row to go deeper.',
      sort: (r) => r.name,
      render: (r) => (
        <div className="name-cell">
          <span className="n">{r.name}</span>
          {r.kind === 'room' && <span className="where">{r.site_code}{r.floor ? ` · floor ${r.floor}` : ''}</span>}
          {r.kind === 'rack' && <span className="where">{r.room_name}{r.row ? ` · row ${r.row}` : ''}{r.u_height ? ` · ${r.u_height}U` : ''}</span>}
        </div>
      ),
    },
    ...(rackTier ? [{
      // What spoke for the rack, and how many of them. The word is the
      // source, in the order the server picks them: a probe is the intake
      // reading, servers are the fallback, and a rack of switches is graded
      // on its front panels rather than left blank.
      key: 'sensors', label: 'Intake from', align: 'mid' as const, width: 108,
      help: 'Which sensor spoke for this rack.',
      sort: (r: ThermalRow) => r.sensors ?? 0,
      render: (r: ThermalRow) => r.source ? (
        <Tip tip={SOURCE_TIP[r.source] ?? 'intake readings from this rack'}>
          {r.sensors} {SOURCE_WORD[r.source]?.(r.sensors ?? 0)
            ?? (r.sensors === 1 ? 'sensor' : 'sensors')}
        </Tip>
      ) : <Tip className="dash" tip="nothing in this rack reported an intake temperature">—</Tip>,
    }] : [{
      key: 'racks', label: 'Racks', align: 'mid' as const, width: 80,
      help: 'Racks here, and how many reported air.',
      sort: (r: ThermalRow) => r.rack_count ?? 0,
      render: (r: ThermalRow) => r.sources ? (
        <Tip tip={<>{r.sources.probes} by rack probe · {r.sources.servers} by servers
                  · {r.sources.network ?? 0} by switch front panel
                  · {(r.rack_count ?? 0) - r.sources.probes - r.sources.servers
                     - (r.sources.network ?? 0)} silent</>}>
          {r.rack_count ?? 0}
        </Tip>
      ) : (r.rack_count ?? 0),
    }]),
    {
      key: 'avg', label: `Average ${u}`, align: 'num', width: 104,
      help: 'Mean intake air across this row.',
      sort: (r) => r.avg_c,
      // Which way it is going rides HERE rather than in a column of its own.
      // A rate column read "flat" on every row on every visit, because a hall
      // with this much headroom barely moves - and a column people learn to
      // skip costs something every day to pay for the rare hour it earns.
      // Beside the number it describes, it costs nothing and is there when
      // somebody asks.
      render: (r) => (
        rateTip(r) ? <Tip tip={rateTip(r)}><Num value={conv(r.avg_c, unit)} why={r.note} /></Tip>
                   : <Num value={conv(r.avg_c, unit)} why={r.note} />
      ),
    },
    ...(mode === 'now' ? [] : [{
      key: 'davg', label: 'Δ avg', align: 'num' as const, width: 84,
      help: 'Change in the average since the window before.',
      sort: (r: ThermalRow) => r.delta_avg,
      render: (r: ThermalRow) => <Delta value={convDelta(r.delta_avg, unit)}
                                        why={r.delta_note} />,
    }]),
    {
      key: 'p90', label: `p90 ${u}`, align: 'num', width: 92,
      help: 'The 90th percentile intake reading.',
      sort: (r) => r.p90_c,
      render: (r) => (
        <Tip tip={r.p90_c === null ? (r.note ?? 'no intake readings in this window')
          : '90th percentile of this row\'s pooled intake readings: what it runs at without one sensor\'s spike deciding'}>
          <span className={r.p90_c !== null && r.p90_c > allowable ? 'critical'
            : r.p90_c !== null && r.p90_c > recommended ? 'warn' : undefined}>
            <Num value={conv(r.p90_c, unit)} />
          </span>
        </Tip>
      ),
    },
    {
      key: 'max', label: `Max ${u}`, align: 'num', width: 96,
      help: 'The hottest single intake reading.',
      sort: (r) => r.max_c,
      render: (r) => <Num value={conv(r.max_c, unit)} why={r.note} />,
    },
    ...(mode === 'now' ? [] : [{
      key: 'dmax', label: 'Δ max', align: 'num' as const, width: 84,
      help: 'Change in the hottest reading since the window before.',
      sort: (r: ThermalRow) => r.delta_max,
      render: (r: ThermalRow) => <Delta value={convDelta(r.delta_max, unit)}
                                        why={r.delta_note} />,
    }]),
    ...(rackTier ? [
      {
        key: 'exhaust', label: `Exhaust ${u}`, align: 'num' as const, width: 104,
      help: 'Mean air leaving the servers in this rack.',
        sort: (r: ThermalRow) => r.exhaust_c ?? null,
        render: (r: ThermalRow) => <Num value={conv(r.exhaust_c ?? null, unit)}
                                        why="no exhaust sensor reported in this window" />,
      },
      {
        key: 'dt', label: 'ΔT K', align: 'num' as const, width: 76,
      help: 'Exhaust minus intake: the heat the air carried away.',
        sort: (r: ThermalRow) => r.delta_t_k ?? null,
        render: (r: ThermalRow) => (
          <Tip tip="server exhaust minus this row's intake - low on a loaded rack is bypass air, not a cooling shortage">
            <Num value={convDelta(r.delta_t_k ?? null, unit)} />
          </Tip>
        ),
      },
    ] : []),
    {
      key: 'compliance', label: 'In band', align: 'num', width: 96,
      help: <>Share of readings inside {floor}-{recommended} °C.</>,
      sort: (r) => r.compliance_pct,
      render: (r) => <Num value={r.compliance_pct} digits={1} unit="%" why={r.note} />,
    },
    {
      key: 'below', label: 'Below band', align: 'num', width: 100,
      help: <>Share of readings under {floor} °C: overcooled.</>,
      sort: (r) => r.below_pct,
      render: (r) => (
        <Tip tip={r.below_pct === null ? (r.note ?? 'no intake readings in this window')
          : `share of readings under ${floor} °C: overcooled air, which costs fan and chiller energy and is the evidence for raising a setpoint`}>
          <Num value={r.below_pct} digits={1} unit="%" />
        </Tip>
      ),
    },
    {
      key: 'spread', label: 'Spread', align: 'mid', width: 116,
      help: 'Where the readings fell against the ASHRAE lines.',
      sort: (r) => (r.compliance_pct === null ? null : 100 - r.compliance_pct),
      render: (r) => <Spread d={r.distribution} low={floor} high={recommended} allowable={allowable} />,
    },
    ...(rackTier ? [] : [{
      key: 'cooling', label: 'Cooling', align: 'mid' as const, width: 104,
      help: 'The room\'s cooling units, and how many are not behaving.',
      sort: (r: ThermalRow) => r.cooling?.units ?? null,
      render: (r: ThermalRow) => <Cooling c={r.cooling} unit={unit} />,
    }]),
    {
      key: 'alarms', label: 'Alarms', align: 'num', width: 90,
      help: 'Open cooling and environmental alarms here.',
      sort: (r) => r.alarms_open ?? 0,
      render: (r) => {
        const n = r.alarms_open ?? 0;
        const where = r.kind === 'rack' ? 'in this rack' : `in ${r.name}`;
        if (!n) {
          return <Tip className="dash" tip={`nothing open ${where}`}>0</Tip>;
        }
        // A CRAH stands on the floor and belongs to no rack, so a hall can
        // show two while every rack under it shows none. Every other figure
        // here folds from the racks; this one does not, and unexplained that
        // reads as a fault in the page rather than plant doing its job.
        const inRacks = r.alarms_in_racks ?? n;
        const onPlant = Math.max(n - inRacks, 0);
        const split = r.kind === 'rack' || !onPlant ? ''
          : inRacks
            ? ` - ${inRacks} in racks, ${onPlant} on floor-standing plant`
            : ` - none in racks, all ${onPlant} on floor-standing plant such as CRAHs`;
        const label = `${n} open cooling or environmental condition${n === 1 ? '' : 's'} ${where}${split}`;
        // A rack has no drill-down of its own: the panel groups by room, and
        // answering for one rack would mean showing rows that are not it.
        // The row already goes to the rack's elevation, which is where a
        // rack's story continues.
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
      key: 'rh', label: 'RH %', align: 'num', width: 84,
      help: 'Mean humidity from this row\'s rack probes.',
      sort: (r) => r.rh_avg,
      render: (r) => (
        <Tip tip={r.rh_avg === null
          ? 'no rack humidity probe reported in this window'
          : <>max <b>{r.rh_max}%</b> · {r.rh_probes} probe{r.rh_probes === 1 ? '' : 's'}
               · recommended ceiling {rhHigh}%</>}>
          <span className={r.rh_max !== null && r.rh_max > rhHigh ? 'warn' : undefined}>
            <Num value={r.rh_avg} />
          </span>
        </Tip>
      ),
    },
    {
      key: 'samples', label: 'Readings', align: 'num', width: 96,
      help: 'How many readings this row is based on.',
      sort: (r) => r.samples,
      render: (r) => (r.samples ? r.samples.toLocaleString() : <span className="dash">—</span>),
    },
  ];

  function exportCsv() {
    downloadCsv(
      stampedName('thermal', data?.window.label),
      ['scope', 'name', 'site', 'room', 'floor', 'racks', 'source', 'sensors', `average_${unit}`,
       `p90_${unit}`, `max_${unit}`, `exhaust_${unit}`, 'delta_t_k', 'in_band_pct',
       'rate_k_per_h',
       'below_band_pct', 'above_recommended_pct', 'above_allowable_pct', 'open_alarms',
       'cooling_units', 'cooling_units_not_behaving', 'cooling_supply_c',
       'cooling_return_c', 'cooling_delta_t_k',
       'rh_avg_pct', 'rh_max_pct', 'rh_probes', 'readings', 'delta_avg_c', 'delta_max_c', 'note'],
      t.filtered.map((r) => [
        r.kind, r.name, r.site_code, r.room_name ?? '', r.floor ?? '', r.rack_count ?? '',
        r.source ?? '', r.sensors ?? '',
        conv(r.avg_c, unit)?.toFixed(1) ?? '', conv(r.p90_c, unit)?.toFixed(1) ?? '',
        conv(r.max_c, unit)?.toFixed(1) ?? '',
        conv(r.exhaust_c ?? null, unit)?.toFixed(1) ?? '', r.delta_t_k ?? '',
        r.compliance_pct ?? '', r.below_pct ?? '',
        r.rate_k_per_h ?? '',
        r.distribution?.above_recommended_pct ?? '', r.distribution?.above_allowable_pct ?? '',
        r.alarms_open ?? 0,
        r.cooling?.units ?? '',
        r.cooling ? r.cooling.units_stopped + r.cooling.units_high_supply
                    + r.cooling.units_high_return : '',
        r.cooling?.supply_c ?? '', r.cooling?.return_c ?? '',
        r.cooling?.delta_t_k ?? '',
        r.rh_avg ?? '', r.rh_max ?? '', r.rh_probes,
        r.samples, r.delta_avg ?? '', r.delta_max ?? '',
        r.note ?? '',
      ]),
    );
  }

  const totals = data?.totals;

  /** Clear the facility drill, leaving whatever site the reader was in. */
  function dropFacility() {
    setParams((prev) => {
      const q = new URLSearchParams(prev);
      q.delete('froom');
      return q;
    });
  }

  /** The trail, built from the same state the tables are.
   *
   *  Every level the reader can step back to is a button; where they are is
   *  not. A site's code rather than its name, because that is what the rows
   *  themselves are labelled with and a trail that renamed things would be a
   *  second vocabulary to learn. */
  const crumbs: Crumb[] = [];
  if (t.selected || t.selectedRoom || froom || t.scope === 'rooms') {
    crumbs.push({
      label: 'All sites',
      onClick: () => {
        dropFacility();
        t.setScope('sites');
      },
    });
  }
  const site = t.selected ?? null;
  if (site) {
    const deeper = Boolean(t.selectedRoom || froom);
    crumbs.push({
      label: site.site_code,
      note: deeper ? undefined : site.name !== site.site_code ? site.name : undefined,
      onClick: deeper ? () => {
        dropFacility();
        if (t.selectedRoom) t.clearDrill();
      } : undefined,
    });
  } else if (!site && t.scope === 'rooms' && !froom) {
    crumbs.push({ label: 'All rooms' });
  }
  if (t.selectedRoom) {
    crumbs.push({
      label: t.selectedRoom.name,
      note: t.selectedRoom.floor ? `floor ${t.selectedRoom.floor}` : undefined,
    });
  } else if (froom) {
    crumbs.push({ label: froom.name, note: froom.purpose });
  }

  return (
    <div className="estate">
      <PageHead
        title="Thermal"
        sub={plantTab
          ? <>The cooling chain that holds that air where it is: what is running,
              what it is moving, and what is spare.{' '}
              <Link to="/analytics?view=cooling">Chiller staging and loop detail →</Link></>
          : <>Intake air. ASHRAE band {floor}–{recommended} °C, allowable to {allowable} °C.{' '}
              <Link to="/analytics?view=thermal">Rack-level ΔT and hot spots →</Link></>}
        // Six KPIs on the title row; the header lets the subtitle wrap
        // before it lets the band drop under the title. The below-band
        // share is a table figure, not a headline: it is sorted on when a
        // setpoint is being decided, and In band already says how much sits
        // outside while the spread bar shows which side.
        kpis={plantTab ? [
          // The load, measured twice and shown twice. They are two independent
          // readings of the same heat - what the CRAHs and CDUs say they are
          // delivering, and flow times ΔT at the chillers - and the gap
          // between them is the instrument check. Averaging them would report
          // a number that matches neither.
          { caption: 'Air side', value: pt?.air_load_kw ?? null, unit: 'kW',
            why: 'no cooling unit reported a delivered load' },
          { caption: 'Water side', value: pt?.water_load_kw ?? null, unit: 'kW',
            why: 'no running chiller reported both loop ends and a flow' },
          { caption: 'Running capacity', value: pt?.capacity_kw ?? null, unit: 'kW',
            why: 'no chiller is running' },
          { caption: 'Utilisation', value: pt?.utilisation_pct ?? null, unit: '%',
            tone: (pt?.utilisation_pct ?? 0) > 90 ? 'warn' : undefined },
          { caption: 'Machines',
            value: pt ? `${pt.running}/${pt.machines}` : null,
            tone: pt && pt.stopped > pt.standby ? 'warn' : 'ok' },
          // Judged on the RUNNING set: a standby machine that has to start,
          // pull down and stage on does not help in the minutes after a trip.
          { caption: 'Redundancy', value: verdictLabel(pt?.redundancy ?? null),
            tone: pt?.redundancy ? ({
              n_plus_1: 'ok', ok: 'ok', tight: 'warn',
            } as Record<string, 'ok' | 'warn' | 'critical'>)[pt.redundancy]
              ?? (verdictTone(pt.redundancy) === 'none' ? undefined
                  : verdictTone(pt.redundancy) as 'ok' | 'warn' | 'critical')
              : undefined,
            why: 'no chiller stage has reported' },
        ] : [
          { caption: 'Average', value: conv(totals?.avg_c ?? null, unit), unit: u,
            why: 'no rack intake sensor reported in this window' },
          { caption: 'p90', value: conv(totals?.p90_c ?? null, unit), unit: u,
            tone: (totals?.p90_c ?? 0) > allowable ? 'critical'
              : (totals?.p90_c ?? 0) > recommended ? 'warn' : undefined,
            why: 'no rack intake sensor reported in this window' },
          { caption: 'Max', value: conv(totals?.max_c ?? null, unit), unit: u,
            tone: (totals?.max_c ?? 0) > allowable ? 'critical'
              : (totals?.max_c ?? 0) > recommended ? 'warn' : undefined },
          { caption: 'In band', value: totals?.compliance_pct ?? null, unit: '%',
            tone: (totals?.compliance_pct ?? 100) < 95 ? 'warn' : 'ok' },
          { caption: 'RH', value: totals?.rh_avg ?? null, unit: '%',
            tone: (totals?.rh_max ?? 0) > rhHigh ? 'warn' : undefined,
            why: 'no rack humidity probe reported in this window' },
          // White space only: rack intake sensors exist where racks do, so
          // counting a generator room as a room that failed to report made the
          // ratio read as a fleet of dead sensors.
          { caption: 'Halls reporting',
            value: totals ? `${totals.rooms_reporting}/${totals.rooms}` : null,
            tone: totals && totals.rooms_reporting < totals.rooms ? 'warn' : 'ok' },
        ]}
      />

      <div className="estate-tools">
        <ScopeTabs<'sites' | 'rooms' | 'plant'>
          scope={plantTab ? 'plant' : t.scope}
          tabs={[{ key: 'sites', label: 'SITES' },
                 { key: 'rooms', label: 'ROOMS' },
                 { key: 'plant', label: 'PLANT' }]}
          onChange={(s) => {
            if (s === 'plant') {
              setParams((prev) => {
                const q = new URLSearchParams(prev);
                q.set('scope', 'plant');
                q.delete('site'); q.delete('room');
                return q;
              });
            } else {
              setParams((prev) => {
                const q = new URLSearchParams(prev);
                // Both drills that belong to the other tabs. A facility room
                // left open here hid the table AND was not rendered itself,
                // because the facility list only exists at the room tier -
                // which is a blank page, the one state no tab should reach.
                q.delete('stage');
                q.delete('froom');
                return q;
              });
              t.setScope(s);
            }
          }} />

        {!plantTab && (
          <input className="grow" type="search"
                 placeholder={rackTier ? 'Search racks' : 'Search sites and rooms'}
                 aria-label="Search" value={t.search}
                 onChange={(e) => t.setSearch(e.target.value)} />
        )}
        {plantTab && <span className="grow" />}
        <Seg label="Unit" value={unit} onChange={setUnit}
             options={[{ key: 'c', label: '°C' }, { key: 'f', label: '°F' }]} />
        {/* Window, intake source and the day pickers all describe rack intake
            readings. None of them means anything to a chiller: a plant view is
            a NOW view by definition, and an hour's mean of a machine that
            tripped twenty minutes ago would show it half running. */}
        {!plantTab && (
          <Seg label="Window" value={mode} onChange={setMode}
               options={[{ key: 'now', label: 'NOW' },
                         { key: 'live', label: 'LAST HOUR' },
                         { key: 'daily', label: 'BY DAY' }]} />
        )}
        {!plantTab && (
          <Seg label="Intake source" value={source} onChange={setSource}
               options={[{ key: 'auto', label: 'AUTO' },
                         { key: 'probes', label: 'PROBES' },
                         { key: 'servers', label: 'SERVERS' }]} />
        )}
        {!plantTab && mode === 'daily' && (
          <>
            <label className="field">
              <span>Focus day</span>
              <input type="date" value={focus} onChange={(e) => setFocus(e.target.value)} />
            </label>
            <label className="field">
              <span>Compare with</span>
              <input type="date" value={compare} onChange={(e) => setCompare(e.target.value)} />
            </label>
          </>
        )}
      </div>

      {/* Where the reader is. Outside the panel, because it answers "what am I
          looking at" rather than being part of the table - and because a back
          button alone can only say one level, so somebody three deep had to
          press it to find out where it went. */}
      {!plantTab && (
        <Crumbs
          onBack={() => {
            if (facilityRoom) { dropFacility(); return; }
            t.clearDrill();
          }}
          items={crumbs} />
      )}

      {/* One tab, one population. PLANT is the machines; everything below is
          rack intake readings, and nothing in it applies to a chiller. */}
      {plantTab ? <Plant unit={unit} /> : (<>
      {!facilityRoom && (
      <div className="estate-panel">
        {(t.tier === 'rooms' || t.selectedRoom || t.selected) && (() => {
          const head = t.selectedRoom ?? t.selected ?? null;
          return (
            // Identity and the way back live in the trail above now. What is
            // left is the table's NAME - the facility table underneath has
            // always carried one, and two unlabelled tables stacked leaves a
            // reader working out which is which from the columns - and the
            // figures for whatever the table is showing.
            <div className="estate-selected">
              {t.tier === 'rooms' && <span className="who">Server rooms</span>}
              {t.selectedRoom && head && (
                <button className="back" title="Devices, alarms and trend for this room"
                        onClick={() => setDrawerRoom({ id: head.id, name: head.name })}>
                  ROOM DETAILS
                </button>
              )}
              {head && (
              <div className="pairs">
                <span className="pair"><span className="cap">Average</span>
                  <span className="v"><Num value={conv(head.avg_c, unit)} /> {u}</span></span>
                <span className="pair"><span className="cap">p90</span>
                  <span className="v"><Num value={conv(head.p90_c, unit)} /> {u}</span></span>
                <span className="pair"><span className="cap">Max</span>
                  <span className="v"><Num value={conv(head.max_c, unit)} /> {u}</span></span>
                <span className="pair"><span className="cap">In band</span>
                  <span className="v"><Num value={head.compliance_pct} unit="%" /></span></span>
                <span className="pair"><span className="cap">Below</span>
                  <span className="v"><Num value={head.below_pct} unit="%" /></span></span>
                <span className="pair"><span className="cap">RH</span>
                  <span className="v"><Num value={head.rh_avg} unit="%" /></span></span>
              </div>
              )}
            </div>
          );
        })()}

        <DataTable
          rows={t.visible}
          columns={columns}
          lead={severity}
          // Site -> its rooms, room -> its racks, rack -> its elevation with
          // the thermal overlay on. The room drawer is the button in the
          // drilled-in header, so it is one click away rather than the click.
          onRowClick={(r) => {
            if (r.kind === 'rack') {
              navigate(`/racks/${r.id}?overlay=thermal&from=thermal`);
              return;
            }
            // Leaving any facility room behind: two drills open at once would
            // be two answers to "which room am I looking at".
            if (facilityRoom) {
              setParams((prev) => {
                const q = new URLSearchParams(prev);
                q.delete('froom');
                return q;
              });
            }
            t.drillInto(r);
          }}
          empty={isLoading ? 'Loading…'
            : error ? 'Could not load thermal data.'
            : 'Nothing matches this search.'}
        />

        <TableFoot total={t.filtered.length} page={t.page} pageSize={t.pageSize}
                   noun={t.tier}
                   onPage={t.setPage} onPageSize={t.setPageSize} onCsv={exportCsv} />
      </div>
      )}

      {/* The rooms with no racks in them. They cannot appear in the table
          above - a switchroom has no intake sensor and never will - so they
          get a table measured on what they actually hold. Shown at the room
          tier, which is where somebody looking at a site's rooms is. */}
      {t.tier === 'rooms' && (
        <Facility unit={unit} siteId={t.selected?.site_id ?? null}
                  siteCode={t.selected?.site_code ?? null} />
      )}

      {/* Drilled into a room, the units cooling it. They stand on the floor,
          so they are in no rack and appear nowhere in the table above - which
          is why a hall could show open conditions while every rack under it
          showed none. Supply against return is also the page's own thesis,
          and it is invisible until the units are listed. */}
      {t.selectedRoom && !facilityRoom && (
        <RoomCooling roomId={t.selectedRoom.id} roomName={t.selectedRoom.name} unit={unit} />
      )}

      {/* The chart follows the drill: the estate, then the site, then the
          room the table is showing.

          Absent inside a facility room, and not merely hidden: the trend is
          built from RACK intake, scoped by the rack row's room, so a room with
          no racks in it could only ever draw an empty chart. Its air is
          measured - by the transmitter on the wall - but that is a different
          series from the one this chart is, and an empty axis would read as
          "nothing is happening" rather than "this is not what is plotted
          here". */}
      {!facilityRoom && (
        <ThermalTrend unit={unit} scope={
          t.selectedRoom ? { kind: 'room', id: t.selectedRoom.id, label: t.selectedRoom.name }
          : t.selected ? { kind: 'site', id: t.selected.id, label: t.selected.name }
          : undefined} />
      )}

      <Notes items={[
        ...(data?.notes ?? []),
        'The trend reads the five-minute rollup for a day of hourly points and the '
        + 'hourly rollup for anything longer, so its p90 is over one value per sensor '
        + 'per five minutes or per hour and can sit a little off the table\'s '
        + 'reading-level p90. Hourly points redraw their newest two hours from '
        + 'the readings themselves, because a rollup is never more current than '
        + 'its refresh. A gap in the line is a period nothing reported.',
        data ? (data.window.compare_label
          ? `Window: ${data.window.label}, compared with ${data.window.compare_label}. `
            + 'Days are UTC so every row covers the same 24 hours.'
          : 'Window: the newest reading from each sensor, taken '
            + `${new Date(data.window.focus_end).toLocaleTimeString()}.`) : '',
      ].filter(Boolean)} />
      </>)}

      {drawerRoom && (
        <RoomDrawer roomId={drawerRoom.id} roomName={drawerRoom.name}
                    onClose={() => setDrawerRoom(null)} />
      )}

      {drill && data && (
        <AlarmPanel
          categories={data.alarm_categories as AlarmCategory[]}
          // The panel appends the scope itself, so naming it here says it twice.
          title="Thermal conditions"
          scope={drill}
          onClose={() => setDrill(null)} />
      )}
    </div>
  );
}
