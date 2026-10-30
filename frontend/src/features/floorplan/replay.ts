import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api, type ThermalField, type TwinRoomScene } from '../../api/client';

/** Replay that plays like a video (docs/27 Phase 4, 2026-10-30).
 *
 *  Three parts, each the thing a video player does:
 *
 *  - a buffer: the next few frames are fetched ahead while playing, and a
 *    few either side of a moment the reader jumped to, two requests at a
 *    time so the host is not flooded;
 *  - a gate: play advances only when the next frame is already in hand, so
 *    the timeline never runs ahead of the picture - it waits, says so, and
 *    goes on;
 *  - a tween: between two consecutive frames every reading is eased from the
 *    old value to the new over the time the frame is on screen. A jump is a
 *    cut, as a seek is in a video: easing Tuesday's colours into Friday's
 *    would draw a change that never happened at that pace.
 *
 *  The record is 5-minute buckets; nothing is invented between them. The
 *  tween moves the drawing, not the data. */

const AHEAD_PLAYING = 6;
const AROUND_PAUSED = 2;
const IN_FLIGHT = 2;
const TOP_UP = 3;
const FRESH_MS = 10 * 60_000;

const sceneKey = (roomId: string, iso: string) => ['twin-scene', roomId, iso] as const;
const fieldKey = (roomId: string, iso: string) => ['twin-field', roomId, iso] as const;

/** The moments the buffer should hold around `t`. */
export function wantedMoments(t: Date, stepMin: number, playing: boolean, now = Date.now()): string[] {
  const out: string[] = [];
  const at = (k: number) => new Date(t.getTime() + k * stepMin * 60_000);
  if (playing) {
    for (let k = 1; k <= AHEAD_PLAYING; k++) out.push(at(k).toISOString());
  } else {
    for (let k = 1; k <= AROUND_PAUSED; k++) {
      out.push(at(k).toISOString());
      out.push(at(-k).toISOString());
    }
  }
  return out.filter((iso) => new Date(iso).getTime() <= now);
}

/** Keeps the frames around `t` in the query cache. Returns `has(iso)`, true
 *  once that moment's scene is in hand, and re-renders as frames land.
 *
 *  Scenes come in runs from `/frames` - the room's geometry built once for
 *  the run, most of what a frame costs - one run in flight at a time. The
 *  heat map's field, when it is showing, is fetched per moment beside it. */
export function useFrameBuffer(roomId: string, t: Date | null, stepMin: number, playing: boolean,
                               withField: boolean, enabled: boolean) {
  const qc = useQueryClient();
  const [, setLanded] = useState(0);
  const run = useRef(false);
  const fields = useRef(new Set<string>());
  const tMs = t?.getTime() ?? null;

  useEffect(() => {
    if (!enabled || !roomId || tMs == null) return;
    let gone = false;
    const landed = () => { if (!gone) setLanded((n) => n + 1); };
    const wanted = wantedMoments(new Date(tMs), stepMin, playing);
    const missing = wanted.filter((iso) => !qc.getQueryData(sceneKey(roomId, iso)));
    // Top up in runs, not a frame at a time: each run builds the room once,
    // so three frames a request costs a third of three requests. The next
    // moment missing is always worth a run of its own.
    const nextMissing = missing.includes(wanted[0]);
    if (missing.length && !run.current && (nextMissing || missing.length >= TOP_UP || !playing)) {
      // One run from the earliest moment missing, long enough to cover the
      // rest; the server drops anything past now.
      const ms = missing.map((iso) => new Date(iso).getTime()).sort((a, b) => a - b);
      const from = ms[0];
      const n = Math.min(12, Math.round((ms[ms.length - 1] - from) / (stepMin * 60_000)) + 1);
      run.current = true;
      api.roomFrames(roomId, new Date(from).toISOString(), stepMin, Math.max(n, 1))
        .then((frames) => {
          for (const f of frames) {
            if (!f.frame) continue;
            const iso = new Date(f.frame.t).toISOString();
            // Never over a frame already read: the one on screen stays the
            // object it is, so nothing redraws for an identical picture.
            if (!qc.getQueryData(sceneKey(roomId, iso))) {
              qc.setQueryData(sceneKey(roomId, iso), f, { updatedAt: Date.now() });
            }
          }
        })
        .catch(() => { /* the moment's own query will try and show its error */ })
        .finally(() => { run.current = false; landed(); });
    }
    if (withField) {
      const want = wanted.filter((iso) => !qc.getQueryData(fieldKey(roomId, iso)) && !fields.current.has(iso));
      for (const iso of want.slice(0, Math.max(0, IN_FLIGHT - fields.current.size))) {
        fields.current.add(iso);
        qc.prefetchQuery({ queryKey: fieldKey(roomId, iso), queryFn: () => api.roomField(roomId, iso),
                           staleTime: FRESH_MS })
          .finally(() => { fields.current.delete(iso); landed(); });
      }
    }
    return () => { gone = true; };
  });   // every render: a landed run frees the way for the next

  return (iso: string) => Boolean(qc.getQueryData(sceneKey(roomId, iso)));
}

// ------------------------------------------------------------------ tween

type Num = number | null | undefined;
const lerp = (a: Num, b: Num, k: number): Num =>
  (a == null || b == null || !Number.isFinite(a) || !Number.isFinite(b)) ? b : a + (b - a) * k;

function lerpFields<T extends object>(a: T, b: T, keys: readonly (keyof T)[], k: number): T {
  const out = { ...b };
  for (const key of keys) {
    (out as Record<keyof T, unknown>)[key] = lerp(a[key] as Num, b[key] as Num, k);
  }
  return out;
}

function lerpList<T extends { id: string }>(a: T[], b: T[], keys: readonly (keyof T)[], k: number): T[] {
  const prev = new Map(a.map((x) => [x.id, x]));
  return b.map((x) => { const p = prev.get(x.id); return p ? lerpFields(p, x, keys, k) : x; });
}

const RACK_KEYS = ['max_inlet_c', 'load_kw'] as const;
const UNIT_KEYS = ['power_w', 'inlet_c', 'supply_c', 'return_c', 'temp_c', 'rh_pct'] as const;
const DEVICE_KEYS = ['power_w', 'inlet_c', 'temp_c'] as const;

/** `b` with every reading eased from `a` by `k` (0..1). States, severities
 *  and identities are `b`'s from the first tick: an alarm is raised or it
 *  is not, it does not fade in. */
export function lerpScene(a: TwinRoomScene, b: TwinRoomScene, k: number): TwinRoomScene {
  if (k >= 1) return b;
  return {
    ...b,
    plan: {
      ...b.plan,
      racks: lerpList(a.plan.racks, b.plan.racks, RACK_KEYS as never, k),
      equipment: lerpList(a.plan.equipment, b.plan.equipment, UNIT_KEYS as never, k),
    },
    devices: lerpList(a.devices, b.devices, DEVICE_KEYS as never, k),
  };
}

export function lerpField(a: ThermalField, b: ThermalField, k: number): ThermalField {
  if (k >= 1 || a.nx !== b.nx || a.ny !== b.ny || a.planes.length !== b.planes.length) return b;
  return {
    ...b,
    planes: b.planes.map((pb, i) => {
      const pa = a.planes[i];
      return { ...pb, temp: pb.temp.map((v, j) => lerp(pa.temp[j], v, k) as number | null) };
    }),
  };
}

/** Same room, same racks, and both live or both in the past: the only
 *  cases a tween is honest. Going live from a past moment is a cut. */
function sameShape(a: TwinRoomScene, b: TwinRoomScene): boolean {
  return a.plan.room_id === b.plan.room_id && a.plan.racks.length === b.plan.racks.length
    && Boolean(a.frame) === Boolean(b.frame);
}

/** Repaint rate while easing. The canvas renders on demand, and every step
 *  rebuilds the instanced colours, so ten a second is smooth to the eye
 *  without pinning the CPU of a host that has none to spare. */
const TICK_MS = 100;

/** What to draw: `target`, reached by easing from what was on screen when
 *  `tween` is true, or cut to at once when it is false. */
export function useTween<T>(target: T | undefined, tween: boolean, durationMs: number,
                            mix: (a: T, b: T, k: number) => T, compatible: (a: T, b: T) => boolean = () => true,
                            ): T | undefined {
  const [shown, setShown] = useState<T | undefined>(target);
  const shownRef = useRef<T | undefined>(target);
  shownRef.current = shown;
  useEffect(() => {
    if (target === undefined) { setShown(undefined); return; }
    const from = shownRef.current;
    const still = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    if (!tween || still || from === undefined || from === target || !compatible(from, target) || durationMs < TICK_MS * 2) {
      setShown(target);
      return;
    }
    const t0 = performance.now();
    let raf = 0;
    let last = 0;
    const step = (now: number) => {
      const k = Math.min(1, (now - t0) / durationMs);
      if (k >= 1) { setShown(target); return; }
      if (now - last >= TICK_MS) {
        last = now;
        // ease in-out: the change starts and ends gently, as a dial moves
        const e = k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
        setShown(mix(from, target, e));
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [target]); // eslint-disable-line react-hooks/exhaustive-deps
  return shown;
}

export const useTweenedScene = (target: TwinRoomScene | undefined, tween: boolean, ms: number) =>
  useTween(target, tween, ms, lerpScene, sameShape);

export const useTweenedField = (target: ThermalField | null | undefined, tween: boolean, ms: number) =>
  useTween(target ?? undefined, tween, ms, lerpField);

/** Was the last move in time a step (ease it) or a jump (cut)? A move of
 *  one step, or any move while playing, is a step; anything else - a click
 *  on the bar, a date typed, going live from the past - is a jump. A
 *  refresh at the same moment (live, every minute) eases too. */
export function useMoveKind(tIso: string | null, stepMin: number, playing: boolean): boolean {
  const prev = useRef<string | null | undefined>(undefined);
  const kind = useRef(false);
  if (tIso === null) { prev.current = null; return true; }   // live refreshes ease; a past->live move is cut by sameShape
  if (prev.current !== tIso) {
    const was = prev.current;
    if (was == null) kind.current = false;
    else {
      const dt = Math.abs(new Date(tIso).getTime() - new Date(was).getTime());
      kind.current = playing || Math.abs(dt - stepMin * 60_000) < 1000;
    }
    prev.current = tIso;
  }
  return kind.current;
}
