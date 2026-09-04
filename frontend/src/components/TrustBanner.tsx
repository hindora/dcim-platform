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
 * is closed during the incident it exists for. It collapses to a single line,
 * and it goes away when the condition does.
 */

import { useEffect, useState } from 'react';
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

function headline(platform: PlatformState, socketDown: boolean): string {
  if (platform.telemetry_stale) {
    return `Telemetry is ${age(platform.telemetry_age_s)} — the numbers on `
      + 'this page may not describe the estate as it is now';
  }
  if (socketDown) {
    return 'Live updates have stopped — this page will not change until the '
      + 'connection is restored';
  }
  return 'The monitoring is degraded — the numbers are still arriving';
}

export function TrustBanner() {
  const [open, setOpen] = useState(true);
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
  if (!platform) return null;
  if (platform.state === 'ok' && !socketDown) return null;

  const conditions = platform.conditions ?? [];
  const shown = open ? conditions.slice(0, 3) : [];
  const hidden = conditions.length - shown.length;

  return (
    <div className={`trust-banner ${platform.state}`} role="status" aria-live="polite">
      <span className="head">{headline(platform, socketDown)}</span>

      {shown.map((c) => (
        <span key={`${c.alarm_type}:${c.instance}`} className="cond">
          <b>{humanise(c.alarm_type)}</b> {c.message}
          <i> since {new Date(c.first_seen).toLocaleTimeString([], {
            hour: '2-digit', minute: '2-digit' })}</i>
        </span>
      ))}
      {open && socketDown && !platform.telemetry_stale && (
        <span className="cond"><b>live_feed</b> the update socket is {socket}</span>
      )}
      {hidden > 0 && <span className="cond muted">+{hidden} more</span>}

      <span className="acts">
        {conditions.length > 3 && (
          <button className="link" onClick={() => setOpen((v) => !v)}>
            {open ? 'Collapse' : `Show ${conditions.length} conditions`}
          </button>
        )}
        <Link className="row-btn" to="/platform">PLATFORM HEALTH</Link>
      </span>
    </div>
  );
}
