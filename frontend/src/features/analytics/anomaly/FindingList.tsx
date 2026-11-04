import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type AnomalyFinding, type FindingCause } from '../../../api/client';
import { CategoryGlyph, type GlyphKind } from '../../../components/CategoryGlyph';
import { metaFor } from '../../../components/alertMeta';
import { humanise, relativeTime } from '../../../lib/format';
import { num } from './format';
import '../anomaly.css';

/** The words for a category, as a finding uses them. The tag's colour comes
 *  from the same tone as the alarm strip, so a cooling finding and a cooling
 *  alarm read as the same owner; the word is always there beside it. */
const CATEGORY_WORD: Record<string, string> = {
  environmental: 'Environmental', cooling: 'Cooling', power: 'Power',
  it_equipment: 'IT equipment', network: 'Network', visibility: 'Sensor reading',
};
const CAUSE_WORD: Record<string, string> = {
  environmental: 'airflow', cooling: 'cooling', power: 'power',
  it_equipment: 'the equipment itself', network: 'the network',
};

export function CategoryTag({ category }: { category: string }) {
  const meta = metaFor(category);
  return (
    <span className={`fnd-cat t-${meta.tone}`}>
      <CategoryGlyph kind={meta.glyph as GlyphKind} size={13} />
      {CATEGORY_WORD[category] ?? humanise(category)}
    </span>
  );
}

const clock = (iso?: string | null) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';

/** What the root did, in a verb: "stopped", "tripped", or the alarm's name. */
function rootDid(type?: string | null): string {
  if (!type) return 'raised an alarm';
  if (type === 'plant_unit_stopped') return 'stopped';
  if (type.includes('breaker') || type.includes('trip')) return 'tripped';
  return `reported ${humanise(type).toLowerCase()}`;
}

/** The likely-cause sentence, from the incident the correlator tied the
 *  finding to. Facts only: what stopped, when, whether it is back. */
export function causeText(f: AnomalyFinding): string {
  const c: FindingCause = f.cause ?? { kind: 'none' };
  if (c.kind === 'incident') {
    const who = c.root_device_name ?? 'An upstream unit';
    const peers = c.peers ? ` with ${c.peers} more cooling unit${c.peers === 1 ? '' : 's'} in the room` : '';
    const what = `${who} ${rootDid(c.root_alarm_type)}${peers}`;
    const head = `Likely cause: ${CAUSE_WORD[c.category ?? ''] ?? c.category}.`;
    if (c.state === 'resolved') {
      const back = `${what} at ${clock(c.opened_at)} and was back by ${clock(c.resolved_at)}.`;
      return f.state === 'resolved'
        ? `${head} ${back}`
        : `${head} ${back} The reading is settling; this closes once it has been normal long enough.`;
    }
    return `${head} ${what}, since ${clock(c.opened_at)}.`;
  }
  if (c.kind === 'room') {
    return `Likely cause: airflow in ${c.room_name ?? 'the room'}. Several devices are hot with no `
      + 'cooling alarm behind them: check containment, blanking panels and floor tiles.';
  }
  if (f.category === 'environmental') {
    return 'No cooling or power fault found upstream. Check the rack itself: blanking panels, '
      + 'cable blockage, an open containment door.';
  }
  if (f.category === 'power') {
    return "No upstream power fault found. Check this circuit's load and what is plugged into it.";
  }
  return 'No upstream cause found.';
}

export function FindingItem({ f, showDevice = true }: { f: AnomalyFinding; showDevice?: boolean }) {
  return (
    <li className={`fnd-item t-${metaFor(f.category).tone}${f.state === 'acknowledged' ? ' is-acked' : ''}`}>
      <div className="fnd-head">
        <CategoryTag category={f.category} />
        <span className="fnd-name">{f.detector_name ?? f.detector_id}</span>
        {showDevice && (
          <Link className="fnd-dev" to={`/devices/${f.device_id}`}>{f.device_name}</Link>
        )}
      </div>
      <p className="fnd-summary">{f.summary}</p>
      <p className="fnd-cause">{causeText(f)}</p>
      <p className="fnd-meta">
        {f.observed != null && f.expected != null
          && `${num(f.observed, f.unit)} against ${num(f.expected, f.unit)}. `}
        {f.state === 'acknowledged' ? 'Acknowledged. ' : ''}
        {`Open ${relativeTime(f.first_seen).replace(/ ago$/, '')}`}
        {f.reopen_count > 0 ? `, back ${f.reopen_count} times` : ''}
      </p>
    </li>
  );
}

/** Findings on anything standing in a rack: its probes, PDUs and servers.
 *  Sensor-reading findings are counted apart - they are about the reading,
 *  not the rack. */
export function useRackFindings(rackId: string) {
  const q = useQuery<{ items: AnomalyFinding[] }>({
    queryKey: ['anomaly-findings', 'rack', rackId],
    queryFn: () => api.anomalyFindings({ rack_id: rackId, limit: '100' }),
    refetchInterval: 60_000,
    retry: false,
  });
  const items = q.data?.items ?? [];
  const plant = items.filter((f) => f.layer !== 'L0');
  const sensor = items.filter((f) => f.layer === 'L0');
  return { q, plant, sensor, devices: new Set(plant.map((f) => f.device_id)).size };
}

export function RackFindings({ rackId, limit, onMore }: {
  rackId: string; limit?: number; onMore?: () => void;
}) {
  const { q, plant, sensor } = useRackFindings(rackId);
  if (q.isLoading) return <p className="fnd-empty">Checking…</p>;
  if (q.isError) return <p className="fnd-empty">Findings could not be read.</p>;
  const shown = limit ? plant.slice(0, limit) : plant;
  const byRoom = plant.some((f) => f.cause?.kind === 'incident' && f.cause.by_room);
  return (
    <div className="fnd">
      {plant.length === 0 && <p className="fnd-empty">Nothing unusual on anything in this rack.</p>}
      {shown.length > 0 && <ul className="fnd-list">{shown.map((f) => <FindingItem key={f.id} f={f} />)}</ul>}
      {limit && plant.length > limit && onMore && (
        <button type="button" className="link-button fnd-more" onClick={onMore}>
          Show all {plant.length}
        </button>
      )}
      {byRoom && (
        <p className="fnd-note">
          A cooling unit is tied to this rack by room: every unit in the hall is taken to serve
          every rack in it until each rack's own cooling is mapped.
        </p>
      )}
      {sensor.length > 0 && (
        <p className="fnd-note">
          {sensor.length} sensor-reading {sensor.length === 1 ? 'finding' : 'findings'} (a spiky,
          stuck or missing reading) on this rack's devices, not counted above.
        </p>
      )}
    </div>
  );
}
