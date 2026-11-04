import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { api, type AnomalyIncident } from '../../api/client';
import { CategoryGlyph } from '../../components/CategoryGlyph';
import { relativeTime } from '../../lib/format';

const DAY_MS = 24 * 3600 * 1000;

/** The tone the site table gives a category tile, for the cause's domain. */
const CAUSE_TONE: Record<string, { tone: string; glyph: 'cooling' | 'power'; word: string }> = {
  power: { tone: 'pwr', glyph: 'power', word: 'power' },
  plant: { tone: 'cool', glyph: 'cooling', word: 'cooling plant' },
  loop: { tone: 'cool', glyph: 'cooling', word: 'cooling' },
  unit: { tone: 'cool', glyph: 'cooling', word: 'cooling unit' },
  zone: { tone: 'cool', glyph: 'cooling', word: 'airflow' },
  server: { tone: 'it', glyph: 'cooling', word: 'equipment' },
  room: { tone: 'cool', glyph: 'cooling', word: 'airflow' },
};

const sevClass = (worst?: string) => {
  const s = (worst ?? '').toLowerCase();
  return s === 'critical' || s === 'major' ? `sev-${s}` : s ? 'sev-minor' : '';
};

/** Where the incident is, in the words the site table uses: the site code
 *  first, because that is how the rows above are keyed, then the room when
 *  the cause stands in one. A plant cause stands in the plant room; its
 *  reach is the rooms listed under it. */
function where(i: AnomalyIncident): string {
  if (i.tier === 'room') return i.room_name ?? 'a room';
  const rooms = i.evidence.by_room?.length ?? 0;
  if (rooms > 1) return `${rooms} rooms`;
  return i.evidence.by_room?.[0]?.room_name ?? i.room_name ?? '';
}

function IncidentRow({ i, onOpen, onRoom }: {
  i: AnomalyIncident; onOpen: () => void; onRoom: (roomId: string) => void;
}) {
  const cause = CAUSE_TONE[i.tier] ?? CAUSE_TONE.unit;
  const worst = (i.evidence.worst ?? 'INFO').toLowerCase();
  const folded = i.evidence.folded ?? 0;
  const rooms = i.evidence.by_room ?? [];
  return (
    <li className="inc">
      <button type="button" className="inc-head" onClick={onOpen}
              title={i.root_alarm_id ? 'Open the alarm that caused it' : 'Open this room’s alarms'}>
        <span className="inc-site" title={i.site_name ?? undefined}>{i.site_code ?? '—'}</span>
        <span className="inc-text">
          <span className="inc-title">{i.title}</span>
          <span className="inc-meta">
            {where(i)}{where(i) ? ' · ' : ''}{cause.word} · {worst} · since {relativeTime(i.opened_at)}
            {folded > 0 ? ` · ${folded} folded` : ''}
          </span>
        </span>
        <span className={`ind on ${cause.tone} ${sevClass(i.evidence.worst)}`} aria-hidden>
          <CategoryGlyph kind={cause.glyph} />
          <span>{i.alarm_count}</span>
        </span>
      </button>
      {rooms.length > 0 && (
        <ul className="inc-rooms" aria-label={`Rooms ${i.title} reaches`}>
          {rooms.map((r) => (
            <li key={r.room_id ?? 'none'}>
              <button type="button" className="inc-room" disabled={!r.room_id}
                      onClick={() => r.room_id && onRoom(r.room_id)}
                      title={r.room_id ? 'This room’s alarms, folded ones included' : undefined}>
                <span className="tick" aria-hidden />
                <span className="inc-room-name">{r.room_name ?? 'No room'}</span>
                <span className="inc-room-n">{r.devices} dev · {r.alarms} alarm{r.alarms === 1 ? '' : 's'}</span>
                <span className={`inc-room-sev ${sevClass(r.worst)}`}>{r.worst.toLowerCase()}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

/** The right rail of the home page: what is wrong now, as incidents, in the
 *  site table's own vocabulary - a raised panel, an uppercase head, rows
 *  keyed by site code, status as the same tinted tile, rooms nested under
 *  the row that reaches them. On a quiet day it holds what was resolved in
 *  the last 24 hours, so a handover can read the night without the history. */
export function IncidentRail() {
  const navigate = useNavigate();
  const open = useQuery({
    queryKey: ['anomaly-incidents', 'open'],
    queryFn: () => api.anomalyIncidents({ state: 'open', limit: '20' }),
    refetchInterval: 30_000,
    retry: false,
  });
  const done = useQuery({
    queryKey: ['anomaly-incidents', 'resolved'],
    queryFn: () => api.anomalyIncidents({ state: 'resolved', limit: '20' }),
    refetchInterval: 120_000,
    retry: false,
  });
  const openItems = [...(open.data?.items ?? [])].sort((a, b) =>
    (a.site_code ?? '').localeCompare(b.site_code ?? '') || a.opened_at.localeCompare(b.opened_at));
  const recent: AnomalyIncident[] = (done.data?.items ?? [])
    .filter((i) => i.resolved_at && Date.now() - new Date(i.resolved_at).getTime() < DAY_MS)
    .slice(0, 6);

  const openOne = (i: AnomalyIncident) => {
    if (i.root_alarm_id) navigate(`/alarms/${i.root_alarm_id}`);
    else if (i.room_id) navigate(`/alarms?room=${i.room_id}&symptoms=1`);
  };

  return (
    <section className="inc-panel" aria-label="Incidents">
      <header className="inc-panel-head">
        <span>Incidents</span>
        <span className={`inc-count${openItems.length ? ' on' : ''}`}>
          {open.isLoading ? '…' : `${openItems.length} open`}
        </span>
      </header>
      {open.isError && <p className="inc-empty">Incidents could not be read.</p>}
      {!open.isLoading && !open.isError && openItems.length === 0 && (
        <p className="inc-empty">No open incidents. Every open alarm stands on its own.</p>
      )}
      {openItems.length > 0 && (
        <ul className="inc-list">
          {openItems.map((i) => (
            <IncidentRow key={i.id} i={i} onOpen={() => openOne(i)}
                         onRoom={(room) => navigate(`/alarms?room=${room}&symptoms=1`)} />
          ))}
        </ul>
      )}
      {recent.length > 0 && (
        <>
          <header className="inc-panel-head sub">
            <span>Resolved, last 24 h</span>
            <span className="inc-count">{recent.length}</span>
          </header>
          <ul className="inc-list done">
            {recent.map((i) => (
              <li key={i.id} className="inc">
                <button type="button" className="inc-head" onClick={() => openOne(i)}
                        title="Open the alarm that caused it">
                  <span className="inc-site" title={i.site_name ?? undefined}>{i.site_code ?? '—'}</span>
                  <span className="inc-text">
                    <span className="inc-title">{i.title}</span>
                    <span className="inc-meta">
                      {where(i)}{where(i) ? ' · ' : ''}
                      {relativeTime(i.opened_at)} to {relativeTime(i.resolved_at!)}
                      {' · '}{i.alarm_count} alarm{i.alarm_count === 1 ? '' : 's'}
                    </span>
                  </span>
                  <span className="ind" aria-hidden>
                    <CategoryGlyph kind={(CAUSE_TONE[i.tier] ?? CAUSE_TONE.unit).glyph} />
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
