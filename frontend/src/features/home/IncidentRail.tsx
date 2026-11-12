import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { api, type AnomalyIncident } from '../../api/client';
import { CategoryGlyph } from '../../components/CategoryGlyph';
import { relativeTime } from '../../lib/format';

const DAY_MS = 24 * 3600 * 1000;
/** How far back the resolved list reaches, and how many of them show before
 *  the reader asks for the rest. A week covers a handover across a weekend. */
const RESOLVED_WINDOW_MS = 7 * DAY_MS;
const RESOLVED_FOLDED = 6;

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
                      title={r.room_id ? 'This room’s alarms' : undefined}>
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

/** Three rows of the shape a real row has, so the panel stands at its
 *  working height while the two queries are in flight. Without them the
 *  panel is a bare 44 px head for the first few hundred milliseconds and
 *  the whole rail jumps when the lists mount. */
function IncidentSkeleton() {
  return (
    <ul className="inc-list inc-skel" aria-hidden>
      {[0, 1, 2].map((n) => (
        <li key={n} className="inc">
          <div className="inc-head">
            <span className="skel skel-site" />
            <span className="inc-text">
              <span className="skel skel-title" />
              <span className="skel skel-meta" />
            </span>
            <span className="skel skel-tile" />
          </div>
        </li>
      ))}
    </ul>
  );
}

/** The right rail of the home page: what is wrong now, as incidents, in the
 *  site table's own vocabulary - a raised panel, an uppercase head, rows
 *  keyed by site code, status as the same tinted tile, rooms nested under
 *  the row that reaches them. Under the open ones it holds what was resolved
 *  in the last seven days, a handful at a time, so a handover can read the
 *  week without the history. */
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
    queryFn: () => api.anomalyIncidents({ state: 'resolved', limit: '200' }),
    refetchInterval: 120_000,
    retry: false,
  });
  const openItems = [...(open.data?.items ?? [])].sort((a, b) =>
    (a.site_code ?? '').localeCompare(b.site_code ?? '') || a.opened_at.localeCompare(b.opened_at));
  const [showAll, setShowAll] = useState(false);
  const cutoff = Date.now() - RESOLVED_WINDOW_MS;
  const recent: AnomalyIncident[] = (done.data?.items ?? [])
    .filter((i) => i.resolved_at && new Date(i.resolved_at).getTime() > cutoff)
    .sort((a, b) => b.resolved_at!.localeCompare(a.resolved_at!));
  const shown = showAll ? recent : recent.slice(0, RESOLVED_FOLDED);
  const folded = recent.length - RESOLVED_FOLDED;
  // One gate for both lists: the panel takes its working height once and
  // the resolved section never lands a beat after the open one.
  const loading = open.isLoading || done.isLoading;

  const openOne = (i: AnomalyIncident) => {
    if (i.root_alarm_id) navigate(`/?alarm=${i.root_alarm_id}`);
    else if (i.room_id) navigate(`/?room=${i.room_id}`);
  };

  return (
    <section className="inc-panel" aria-label="Incidents">
      <header className="inc-panel-head">
        <span>Incidents</span>
        <span className={`inc-count${openItems.length ? ' on' : ''}`}>
          {open.isLoading ? '…' : `${openItems.length} open`}
        </span>
      </header>
      {loading && <IncidentSkeleton />}
      {open.isError && <p className="inc-empty">Incidents could not be read.</p>}
      {!loading && !open.isError && openItems.length === 0 && (
        <p className="inc-empty">No open incidents. Every open alarm stands on its own.</p>
      )}
      {openItems.length > 0 && (
        <ul className="inc-list">
          {openItems.map((i) => (
            <IncidentRow key={i.id} i={i} onOpen={() => openOne(i)}
                         onRoom={(room) => navigate(`/?room=${room}`)} />
          ))}
        </ul>
      )}
      {!loading && recent.length > 0 && (
        <>
          <header className="inc-panel-head sub">
            <span>Resolved, last 7 days</span>
            <span className="inc-count">{recent.length}</span>
          </header>
          <ul className="inc-list done">
            {shown.map((i) => (
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
          {folded > 0 && (
            <button type="button" className="inc-more" aria-expanded={showAll}
                    onClick={() => setShowAll((v) => !v)}>
              {showAll ? 'Show fewer' : `Show all ${recent.length}`}
            </button>
          )}
        </>
      )}
    </section>
  );
}
