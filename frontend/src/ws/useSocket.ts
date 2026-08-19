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

/**
 * Invalidate query keys when matching frames arrive.
 *
 * Deliberately a refetch rather than patching the cache from the frame: the
 * socket carries deltas with no replay, so after any gap the server's answer is
 * the only trustworthy one.
 */
export function useInvalidateOn(events: string[], queryKeys: string[][]): void {
  const qc = useQueryClient();
  useFrames(events, () => {
    queryKeys.forEach((k) => qc.invalidateQueries({ queryKey: k }));
  });
}
