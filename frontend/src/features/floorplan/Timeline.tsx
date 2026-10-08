import { useMemo, type ReactNode } from 'react';
import type { TwinFrameInfo } from '../../api/client';

/**
 * The viewer's timeline (docs/27 Phase 4): live, or the room at a moment in
 * the past. One day on the scrubber, a date to pick the day, a step size, play
 * to walk forward, and a compare pane that shows a second moment beside the
 * main one - either a fixed offset earlier, or a moment of its own on a second,
 * slimmer row.
 *
 * The scrubber is the instrument here: hour ticks on the track, the exact
 * time riding on the thumb, and the parts of the day nobody can go to - before
 * the room's oldest record, after now - greyed out. Everything else is quiet.
 */

/** The scrubber's resolution: the 5-minute rollup is the finest record kept past two hours. */
export const STEP_MIN = 5;
const DAY_MIN = 24 * 60;

/** How far play and the step buttons move per frame, minutes. */
export const STEP_OPTIONS: { value: number; label: string }[] = [
  { value: 5, label: '5 min' },
  { value: 15, label: '15 min' },
  { value: 60, label: '1 hour' },
];

/** Compare choices: null off, a number = seconds before the main moment
 *  (0 = the live room), 'pick' = a moment of its own on the compare row. */
export type CompareMode = number | 'pick' | null;
export const COMPARE_OPTIONS: { value: CompareMode; label: string }[] = [
  { value: null, label: 'Compare off' },
  { value: 3600, label: '1 hour earlier' },
  { value: 86_400, label: '24 hours earlier' },
  { value: 7 * 86_400, label: '7 days earlier' },
  { value: 0, label: 'Live beside it' },
  { value: 'pick', label: 'Pick a time' },
];

/** Transport glyphs as inline vectors: one stroke weight, one family, no font dependence. */
function Glyph({ kind }: { kind: 'play' | 'pause' | 'back' | 'fwd' }) {
  const d = {
    play: 'M6 4.5v11l9-5.5z',
    pause: 'M6 4.5h3v11H6zM11 4.5h3v11h-3z',
    back: 'M12 5l-5 5 5 5',
    fwd: 'M8 5l5 5-5 5',
  }[kind];
  const filled = kind === 'play' || kind === 'pause';
  return (
    <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden focusable="false">
      <path d={d} fill={filled ? 'currentColor' : 'none'} stroke={filled ? 'none' : 'currentColor'}
            strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

const pad = (n: number) => String(n).padStart(2, '0');
export const localDateKey = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const hm = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
const stepWords = (m: number) => (m >= 60 ? `${m / 60} hour${m === 60 ? '' : 's'}` : `${m} minutes`);

/** Floor a moment to the scrubber's resolution. */
export function snap(d: Date): Date {
  const x = new Date(d);
  x.setSeconds(0, 0);
  x.setMinutes(Math.floor(x.getMinutes() / STEP_MIN) * STEP_MIN);
  return x;
}

export function sourceLabel(f?: TwinFrameInfo | null): string {
  switch (f?.source) {
    case 'raw': return 'from the raw record';
    case '5m': return 'from the 5-minute record';
    case '1h': return 'from the hourly record';
    default: return '';
  }
}

/** One day of one moment: the date, back / forward, and the scrubber. The main
 *  row and the compare row are both this. */
function DayScrub({ t, earliest, step, label, children, onTime }: {
  /** null = live (main row only). */
  t: Date | null;
  earliest: Date | null;
  step: number;
  label: string;
  /** Slotted between the transport and the scrubber (the main row's play). */
  children?: ReactNode;
  onTime: (d: Date) => void;
}) {
  const now = new Date();
  const shown = t ?? now;
  const dayStart = useMemo(() => {
    const d = new Date(shown);
    d.setHours(0, 0, 0, 0);
    return d;
  }, [shown.getFullYear(), shown.getMonth(), shown.getDate()]); // eslint-disable-line react-hooks/exhaustive-deps
  const minutes = Math.round((shown.getTime() - dayStart.getTime()) / 60_000);
  const today = localDateKey(shown) === localDateKey(now);
  // The future is not a place one can go, and nor is before the room's oldest record.
  const maxMinutes = today ? Math.floor((now.getTime() - dayStart.getTime()) / 60_000 / STEP_MIN) * STEP_MIN : DAY_MIN - STEP_MIN;
  // A day wholly before the oldest record has nothing on it at all.
  const minMinutes = earliest == null || earliest <= dayStart ? 0
    : Math.min(DAY_MIN, Math.ceil((earliest.getTime() - dayStart.getTime()) / 60_000 / STEP_MIN) * STEP_MIN);
  const pct = (minutes / DAY_MIN) * 100;

  const at = (mins: number) => {
    const d = new Date(dayStart);
    d.setMinutes(Math.max(minMinutes, Math.min(maxMinutes, mins)));
    onTime(d);
  };
  const onDate = (value: string) => {
    const [y, m, d] = value.split('-').map(Number);
    if (!y || !m || !d) return;
    let next = new Date(y, m - 1, d, shown.getHours(), shown.getMinutes());
    if (next > now) next = snap(now);
    if (earliest && next < earliest) next = snap(new Date(earliest.getTime() + STEP_MIN * 60_000 - 1));
    onTime(next);
  };

  return (
    <>
      <input type="date" className="vw-date" value={localDateKey(shown)} max={localDateKey(now)}
             min={earliest ? localDateKey(earliest) : undefined}
             onChange={(e) => onDate(e.target.value)} aria-label={`${label}: day`} />

      <div className="vw-transport">
        <button type="button" onClick={() => at(minutes - step)} aria-label={`${label}: back ${stepWords(step)}`}
                title={`Back ${stepWords(step)}`} disabled={minutes <= minMinutes}><Glyph kind="back" /></button>
        {children}
        <button type="button" onClick={() => at(minutes + step)} aria-label={`${label}: forward ${stepWords(step)}`}
                title={`Forward ${stepWords(step)}`} disabled={!t || minutes >= maxMinutes}><Glyph kind="fwd" /></button>
      </div>

      <div className="vw-scrub" style={{
        ['--reach' as string]: `${(maxMinutes / DAY_MIN) * 100}%`,
        ['--from' as string]: `${(minMinutes / DAY_MIN) * 100}%`,
      }}>
        <input type="range" min={0} max={DAY_MIN - STEP_MIN} step={STEP_MIN}
               value={Math.min(minutes, DAY_MIN - STEP_MIN)}
               onChange={(e) => at(Number(e.target.value))} aria-label={`${label}: time of day`}
               aria-valuetext={`${hm(shown)}${t ? '' : ', live'}`} />
        <div className="vw-scrub-ticks" aria-hidden>
          {Array.from({ length: 25 }, (_, h) => (
            <span key={h} className={h % 6 === 0 ? 'major' : undefined} style={{ left: `${(h / 24) * 100}%` }}>
              {h % 6 === 0 && h < 24 ? `${pad(h)}:00` : ''}
            </span>
          ))}
        </div>
        <output className="vw-scrub-label" style={{ left: `${Math.max(Math.min(pct, (maxMinutes / DAY_MIN) * 100), 0)}%` }} aria-hidden>
          {t ? hm(shown) : `${hm(shown)} live`}
        </output>
      </div>
    </>
  );
}

export interface TimelineProps {
  /** null = live. */
  t: Date | null;
  playing: boolean;
  /** Minutes per frame for play and the step buttons. */
  step: number;
  cmp: CompareMode;
  /** The compare row's own moment, when cmp is 'pick'. */
  cmpT: Date | null;
  /** The room's oldest record; null = unknown or none. */
  earliest: Date | null;
  frame?: TwinFrameInfo | null;
  cmpFrame?: TwinFrameInfo | null;
  onLive: () => void;
  onTime: (d: Date) => void;
  onPlay: (playing: boolean) => void;
  onStep: (minutes: number) => void;
  onCmp: (mode: CompareMode) => void;
  onCmpTime: (d: Date) => void;
}

export function Timeline({ t, playing, step, cmp, cmpT, earliest, frame, cmpFrame,
  onLive, onTime, onPlay, onStep, onCmp, onCmpTime }: TimelineProps) {
  const atEnd = !t || t.getTime() + step * 60_000 > Date.now();
  return (
    <div className={`vw-time${t ? ' is-replay' : ''}${cmp === 'pick' ? ' has-compare' : ''}`} role="group" aria-label="Timeline">
      <div className="vw-time-row">
        <button type="button" className={`vw-live${t ? '' : ' is-on'}`} onClick={onLive}
                aria-pressed={!t} title={t ? 'Back to the live room' : 'Showing the live room'}>
          <span className="dot" aria-hidden />Live
        </button>

        <DayScrub t={t} earliest={earliest} step={step} label="Main view" onTime={onTime}>
          <button type="button" className="vw-play" onClick={() => onPlay(!playing)} aria-pressed={playing}
                  aria-label={playing ? 'Pause' : 'Play'}
                  title={playing ? 'Pause' : `Play forward, ${stepWords(step)} a frame`}
                  disabled={!t || (atEnd && !playing)}>
            <Glyph kind={playing ? 'pause' : 'play'} />
          </button>
        </DayScrub>

        <label className="vw-stepsel">
          <span>Step</span>
          <select value={step} onChange={(e) => onStep(Number(e.target.value))} aria-label="Step size for play and the step buttons">
            {STEP_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </label>

        {t && frame && <span className="vw-src" title={frame.note ?? undefined}>{sourceLabel(frame)}</span>}

        <select className="vw-cmp" value={cmp == null ? '' : String(cmp)} aria-label="Compare with another moment"
                onChange={(e) => {
                  const v = e.target.value;
                  onCmp(v === '' ? null : v === 'pick' ? 'pick' : Number(v));
                }}>
          {COMPARE_OPTIONS.map((o) => (
            <option key={o.label} value={o.value == null ? '' : String(o.value)}>{o.label}</option>
          ))}
        </select>
      </div>

      {cmp === 'pick' && (
        <div className="vw-time-row vw-time-cmp" role="group" aria-label="Compare moment">
          <span className="vw-cmp-tag">Compare</span>
          <DayScrub t={cmpT ?? new Date(Date.now() - 3600_000)} earliest={earliest} step={step}
                    label="Compare view" onTime={onCmpTime} />
          {cmpFrame && <span className="vw-src" title={cmpFrame.note ?? undefined}>{sourceLabel(cmpFrame)}</span>}
        </div>
      )}
    </div>
  );
}

/** The caption a compare pane wears: when it is, in words. */
export function paneCaption(t: Date | null, frame?: TwinFrameInfo | null): string {
  if (!t) return 'Live';
  const day = localDateKey(t) === localDateKey(new Date()) ? 'Today' : t.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  const src = sourceLabel(frame);
  return `${day} ${hm(t)}${src ? ` · ${src}` : ''}`;
}
