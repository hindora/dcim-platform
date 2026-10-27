/**
 * Whether the numbers on this page can be believed - on every page.
 *
 * This is the one banner that belongs above the navigation, and the test it
 * has to pass is simple: it is still true after you navigate. A room that has
 * no rating recorded is a fact about that room's table. A collector that
 * stopped polling 1386 endpoints is a fact about every figure in the product,
 * including the counts in the nav itself.
 *
 * It used to be four separate banners, and which one you saw depended on where
 * you were standing:
 *
 *   Home.tsx        "The monitoring is degraded"          Home only
 *   Dashboard.tsx   "Telemetry is N seconds behind"       Dashboard only
 *   App.tsx         "Live updates closed"                 below the nav, scrolled away
 *   PlatformHealth  "No collector has ever checked in"    the page you visit only if you already suspect
 *
 * Home and Dashboard were two codepaths for one fact, with different wording
 * and different thresholds. And on /thermal or /power there was nothing at
 * all - so a thermal map could be drawn from a pipeline that had stopped an
 * hour ago with no indication whatsoever. That was the real defect; the
 * placement was only how it stayed hidden.
 *
 * Deliberately NOT dismissible. A trust banner that can be closed is one that
 * is closed during the incident it exists for. It is one line - a state word,
 * the consequence, the conditions grouped by kind - and the detail opens under
 * it on request. It goes away when the condition does.
 *
 * One line, not one line per condition: ten collectors going stale is one fact
 * with ten names. The old strip printed each as a sentence that differed from
 * the next only in a hostname and a count, wrapped to three rows, and pushed
 * the navigation a hundred pixels down the screen on every page.
 */

import { useEffect, useRef, useState } from 'react';
import { humanise } from '../lib/format';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type PlatformState } from '../api/client';
import { useSocketStatus } from '../ws/useSocket';
import type { SocketStatus } from '../ws/client';

/** How often to ask. The endpoint is two queries, and the answer is the
 *  premise of everything else on screen, so it is worth asking often - but a
 *  banner that flickers between states reads as noise, so not every second. */
const POLL_MS = 20_000;

/** How long the socket must stay away from 'open' before it counts as down.
 *  Every page load spends its first seconds handshaking, so without this
 *  grace each refresh opened with "live updates have stopped" over a feed
 *  that was two seconds from connecting - and a trust banner that cries on
 *  every load is one nobody believes during an incident. */
const SOCKET_GRACE_MS = 10_000;

/** How many rows of one kind the details list shows before it hands over to
 *  the platform page. Capped rather than scrolled: a scroll box inside a
 *  sticky strip is a trap for the wheel. */
const ROWS_PER_GROUP = 6;

/** True only once the socket has been non-open for the whole grace window;
 *  false again the moment it opens. */
function useSocketDown(status: SocketStatus): boolean {
  const [down, setDown] = useState(false);
  useEffect(() => {
    if (status === 'open') {
      setDown(false);
      return;
    }
    const t = window.setTimeout(() => setDown(true), SOCKET_GRACE_MS);
    return () => window.clearTimeout(t);
  }, [status]);
  return down;
}

function age(seconds: number | null): string {
  if (seconds === null) return 'not arriving at all';
  if (seconds < 90) return `${Math.round(seconds)}s old`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min old`;
  return `${Math.round(seconds / 3600)}h old`;
}

type Condition = PlatformState['conditions'][number];
type Tone = 'critical' | 'major' | 'warn';

/** The strip's colour and its one-word verdict. The word is there so the
 *  state is never carried by colour alone. */
function stripTone(platform: PlatformState, socketOnly: boolean): { tone: Tone; word: string } {
  if (socketOnly) return { tone: 'warn', word: 'Live updates paused' };
  if (platform.state === 'blind') return { tone: 'critical', word: 'Monitoring blind' };
  if (platform.state === 'impaired') return { tone: 'critical', word: 'Monitoring impaired' };
  return { tone: 'warn', word: 'Monitoring degraded' };
}

/** The consequence, in the reader's words. The state word already said how
 *  bad; this says what it means for the figures on the screen. */
function headline(platform: PlatformState, socketOnly: boolean): string {
  if (platform.telemetry_stale) {
    return `Telemetry is ${age(platform.telemetry_age_s)}, so figures on this `
      + 'page may not match the estate';
  }
  if (socketOnly) return 'This page will not refresh until the connection returns';
  if (platform.state === 'blind') return 'Figures on this page are not current';
  if (platform.state === 'impaired') return 'Some figures on this page may be missing or late';
  return 'Figures are still arriving, some of them late';
}

function sevTone(severity: string): Tone {
  const s = (severity || '').toUpperCase();
  if (s === 'CRITICAL') return 'critical';
  if (s === 'MAJOR') return 'major';
  return 'warn';
}

const TONE_RANK: Record<Tone, number> = { critical: 3, major: 2, warn: 1 };

interface Group {
  type: string;
  label: string;
  tone: Tone;
  since: number;
  items: Condition[];
}

function groupConditions(conds: Condition[]): Group[] {
  const by = new Map<string, Group>();
  for (const c of conds) {
    const t = sevTone(c.severity);
    const at = Date.parse(c.first_seen);
    const g = by.get(c.alarm_type);
    if (g) {
      g.items.push(c);
      if (TONE_RANK[t] > TONE_RANK[g.tone]) g.tone = t;
      if (at < g.since) g.since = at;
    } else {
      by.set(c.alarm_type, {
        type: c.alarm_type, label: humanise(c.alarm_type), tone: t, since: at, items: [c],
      });
    }
  }
  return [...by.values()].sort((a, b) =>
    TONE_RANK[b.tone] - TONE_RANK[a.tone] || b.items.length - a.items.length);
}

/** The message without the name the row already shows. Platform messages open
 *  "Collector col-dc1-bms last checked ..." and the row's first column is
 *  col-dc1-bms; saying it twice per row is most of what made the old strip a
 *  wall of text. */
function rest(message: string, instance: string): string {
  if (!instance) return message;
  const at = message.indexOf(instance);
  if (at < 0 || at > 24) return message;
  const tail = message.slice(at + instance.length).replace(/^[\s:,.-]+/, '');
  return tail ? tail.charAt(0).toUpperCase() + tail.slice(1) : message;
}

function clock(ms: number): string {
  return Number.isFinite(ms)
    ? new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : '';
}

/** The kind of trouble as a shape as well as a colour. Decorative beside the
 *  state word that follows it, so hidden from assistive tech. */
function Glyph({ kind }: { kind: 'alert' | 'stop' | 'link' }) {
  const common = {
    width: 16, height: 16, viewBox: '0 0 16 16', fill: 'none', stroke: 'currentColor',
    strokeWidth: 1.6, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
    'aria-hidden': true, focusable: false,
  };
  if (kind === 'stop') {
    return (
      <svg {...common}>
        <path d="M5.2 1.8h5.6l3.4 3.4v5.6l-3.4 3.4H5.2l-3.4-3.4V5.2z" />
        <path d="M8 4.8v3.8M8 11.2v.01" />
      </svg>
    );
  }
  if (kind === 'link') {
    return (
      <svg {...common}>
        <path d="M5 7.5L3.6 8.9a2.5 2.5 0 003.5 3.5L8.5 11M11 8.5l1.4-1.4a2.5 2.5 0 00-3.5-3.5L7.5 5" />
        <path d="M2 2l12 12" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <path d="M8 1.8L14.6 13.6H1.4z" />
      <path d="M8 6.2v3.4M8 11.6v.01" />
    </svg>
  );
}

export function TrustBanner() {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const socket = useSocketStatus();
  const { data: platform } = useQuery<PlatformState>({
    queryKey: ['platform-state'],
    queryFn: api.platformState,
    refetchInterval: POLL_MS,
    // The banner is the thing that tells you the rest of the page is stale.
    // It must not be stale itself, and it must keep asking on a tab somebody
    // left open on a wall.
    refetchOnWindowFocus: true,
    refetchIntervalInBackground: true,
    retry: false,
  });

  // A dropped socket alone is not worth interrupting anyone about - it
  // reconnects, and announcing every reconnect is how an operator learns to
  // ignore this strip. It earns a line here only when it STAYS down past the
  // grace window, or as a condition alongside an already-degraded pipeline.
  const socketDown = useSocketDown(socket);

  // The details drop over the page, so they fold away the way any popover
  // does: Escape, or a press anywhere outside the strip.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    const onDown = (e: PointerEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    window.addEventListener('pointerdown', onDown);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('pointerdown', onDown);
    };
  }, [open]);

  if (!platform) return null;
  if (platform.state === 'ok' && !socketDown) return null;

  const socketOnly = platform.state === 'ok' && socketDown;
  const groups = groupConditions(platform.conditions ?? []);
  const { tone, word } = stripTone(platform, socketOnly);
  const head = headline(platform, socketOnly);
  const since = groups.length ? Math.min(...groups.map((g) => g.since)) : NaN;
  const chips = groups.slice(0, 3);
  const moreGroups = groups.length - chips.length;
  const canOpen = groups.length > 0;
  const glyph = socketOnly ? 'link' : tone === 'critical' ? 'stop' : 'alert';

  return (
    <div ref={root} className={`trust-banner tone-${tone}${open ? ' open' : ''}`}
         role="status" aria-live="polite">
      <div className="tb-line">
        <span className="tb-state">
          <Glyph kind={glyph} />
          <span>{word}</span>
        </span>
        <span className="tb-head" title={head}>{head}</span>

        {(chips.length > 0 || (socketDown && !socketOnly)) && (
          <span className="tb-chips">
            {chips.map((g) => (
              <span key={g.type} className={`tb-chip tone-${g.tone}`}
                    title={g.items.map((c) => c.instance).filter(Boolean).join(', ')}>
                {g.label}
                {g.items.length > 1 && <b>{g.items.length}</b>}
              </span>
            ))}
            {moreGroups > 0 && <span className="tb-chip more">+{moreGroups} more</span>}
            {socketDown && !socketOnly && (
              <span className="tb-chip tone-warn">
                Live updates {socket === 'closed' ? 'closed' : 'reconnecting'}
              </span>
            )}
          </span>
        )}

        <span className="tb-acts">
          {Number.isFinite(since) && <span className="tb-since">since {clock(since)}</span>}
          {canOpen && (
            <button type="button" className="tb-btn" aria-expanded={open}
                    aria-controls="trust-banner-details" onClick={() => setOpen((v) => !v)}>
              {open ? 'Hide details' : 'Details'}
              <svg className="tb-caret" width="10" height="10" viewBox="0 0 10 10"
                   aria-hidden focusable={false}>
                <path d="M2 3.5l3 3 3-3" fill="none" stroke="currentColor" strokeWidth="1.5"
                      strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          )}
          <Link className="tb-btn" to="/platform">Platform health</Link>
        </span>
      </div>

      {open && canOpen && (
        <div id="trust-banner-details" className="tb-details">
          {groups.map((g) => (
            <section key={g.type} className="tb-group">
              <h4 className={`tone-${g.tone}`}>
                <span className="tb-dot" aria-hidden />
                {g.label}
                <span className="tb-n">{g.items.length}</span>
                <span className="tb-gsince">since {clock(g.since)}</span>
              </h4>
              <ul>
                {g.items.slice(0, ROWS_PER_GROUP).map((c) => (
                  <li key={`${c.alarm_type}:${c.instance}`}>
                    {c.instance ? <code>{c.instance}</code> : <span />}
                    <span>{rest(c.message, c.instance)}</span>
                  </li>
                ))}
              </ul>
              {g.items.length > ROWS_PER_GROUP && (
                <Link className="tb-rest" to="/platform">
                  {g.items.length - ROWS_PER_GROUP} more on Platform health
                </Link>
              )}
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
