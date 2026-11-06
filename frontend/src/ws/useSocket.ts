import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { DcimSocket, type Frame, type SocketStatus } from './client';

let singleton: DcimSocket | null = null;

function socket(): DcimSocket {
  if (!singleton) {
    singleton = new DcimSocket(async () => (await api.wsTicket()).ticket);
    singleton.connect();
  }
  return singleton;
}

/** Subscribe to topics for as long as the component is mounted. */
export function useTopics(topics: string[]): void {
  const key = topics.join(',');
  useEffect(() => {
    const s = socket();
    s.subscribe(topics);
    return () => s.unsubscribe(topics);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
}

export function useSocketStatus(): SocketStatus {
  const [status, setStatus] = useState<SocketStatus>(socket().status);
  useEffect(() => socket().onStatus(setStatus), []);
  return status;
}

/**
 * Run a handler on every frame of the given event types.
 *
 * The handler is held in a ref so a re-render does not re-subscribe and drop
 * frames in the gap.
 */
export function useFrames(events: string[], onFrame: (f: Frame) => void): void {
  const ref = useRef(onFrame);
  ref.current = onFrame;
  const key = events.join(',');
  useEffect(() => {
    return socket().onFrame((frame) => {
      if (events.includes(frame.event)) ref.current(frame);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
}

/** The alarm frames the workers publish on the `alarms` topic. */
export const ALARM_EVENTS = ['alarm_created', 'alarm_updated', 'alarm_cleared'];

/** How long a doorbell waits for the crowd before it rings once. */
const SETTLE_MS = 1_500;

/**
 * Invalidate query keys when matching frames arrive.
 *
 * Deliberately a refetch rather than patching the cache from the frame: the
 * socket carries deltas with no replay, so after any gap the server's answer is
 * the only trustworthy one. The frame says "something changed, go look";
 * it never says what the number is.
 *
 * Rung at most once per `settle` window. A cascade raises nine hundred
 * conditions in a minute, and nine hundred recounts of the heaviest query
 * in the app is a load generator wearing a feature's clothes; thirty is
 * the same picture to a reader. The first frame starts the clock, and the
 * recount fires when it runs out, so nothing waits longer than `settle`.
 */
export function useInvalidateOn(events: string[], queryKeys: string[][],
                                settle: number = SETTLE_MS): void {
  const qc = useQueryClient();
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null);
  useFrames(events, () => {
    if (pending.current) return;
    pending.current = setTimeout(() => {
      pending.current = null;
      queryKeys.forEach((k) => qc.invalidateQueries({ queryKey: k }));
    }, settle);
  });
  // A bell still wound up when the page goes should not ring into an
  // unmounted cache.
  useEffect(() => () => {
    if (pending.current) clearTimeout(pending.current);
  }, []);
}
