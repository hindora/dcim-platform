import { useEffect, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useFrames, useSocketStatus, useTopics } from '../../../ws/useSocket';
import type { Frame } from '../../../ws/client';

/** The anomaly websocket contract (doc 30 section 8, doc 31 section 11).
 *
 *  Matched on the exact dotted strings. Frames are used for ROUTING only:
 *  the socket has no replay, so a frame says "something changed, ask again"
 *  and the REST answer is what gets drawn. */
export const ANOMALY_TOPIC = 'anomalies';

export const ANOMALY_EVENTS = [
  'anomaly.finding.opened',
  'anomaly.finding.updated',
  'anomaly.finding.resolved',
  'anomaly.incident.opened',
  'anomaly.incident.updated',
];

/** Every anomaly query key prefix, for a bulk frame or a reconnect. */
export const ANOMALY_KEYS = [
  'anomaly-findings', 'anomaly-finding', 'anomaly-summary', 'anomaly-health',
  'anomaly-detectors', 'anomaly-overrides', 'anomaly-exclusions',
  'anomaly-incidents', 'anomaly-incident', 'device-findings',
];

const DEBOUNCE_MS = 2000;

/** Subscribe to `anomalies` and invalidate on its frames.
 *
 *  `keys(frame)` returns the query keys a frame should refresh. A `bulk`
 *  summary (the sweep's cap, sent instead of N frames) refreshes every
 *  anomaly key; a frame that came through the alarm (`via: 'alarm'`) also
 *  refreshes the alarm caches. Frames within 2 s are coalesced, because a
 *  chiller trip opens dozens of findings in one sweep. After a reconnect
 *  every anomaly key is invalidated: after a gap, REST is the truth. */
export function useAnomalyLive(keys: (frame: Frame) => string[][]): void {
  const qc = useQueryClient();
  useTopics([ANOMALY_TOPIC]);

  const pending = useRef<Map<string, string[]>>(new Map());
  const timer = useRef<number | null>(null);

  const flush = () => {
    timer.current = null;
    for (const k of pending.current.values()) qc.invalidateQueries({ queryKey: k });
    pending.current.clear();
  };
  const queue = (list: string[][]) => {
    for (const k of list) pending.current.set(JSON.stringify(k), k);
    if (timer.current === null) timer.current = window.setTimeout(flush, DEBOUNCE_MS);
  };

  useFrames(ANOMALY_EVENTS, (frame) => {
    if (frame.change === 'bulk') {
      queue(ANOMALY_KEYS.map((k) => [k]));
      return;
    }
    const list = keys(frame);
    if (!list.length) return;
    if (frame.via === 'alarm') list.push(['alarms'], ['alarm']);
    queue(list);
  });

  const status = useSocketStatus();
  const prev = useRef(status);
  useEffect(() => {
    if (prev.current === 'reconnecting' && status === 'open') {
      queue(ANOMALY_KEYS.map((k) => [k]));
    }
    prev.current = status;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);
}

/** The finding a frame is about, when it carries one. */
export function frameFinding(frame: Frame): { id?: string; alarm_id?: string | null } | null {
  const f = frame.finding;
  return f && typeof f === 'object' ? (f as { id?: string; alarm_id?: string | null }) : null;
}
