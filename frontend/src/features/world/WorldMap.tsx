import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { geoEqualEarth, geoGraticule10, geoMercator, geoPath, type GeoPermissibleObjects } from 'd3-geo';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { feature } from 'topojson-client';
import type { GeometryCollection, Topology } from 'topojson-specification';
import countries110m from 'world-atlas/countries-110m.json';
import { api, ApiError, type HazardAlert, type HazardSeverity, type SiteKpi, type SiteRow, type SitesOverview } from '../../api/client';
import { useHoverTip } from '../../components/HoverTip';
import { fmtT, useTempUnit } from '../floorplan/units';
import {
  ago, blueMarbleUrl, frameTimes, geoColorUrl, prefetch, radarUrl, tilesFor, useImageryReachable,
  useRadarIndex, utcClock, Z_MAX, type View,
} from './satellite';
import './world.css';

/**
 * Every site on the globe, coloured by the worst open alarm it holds, with
 * the outdoor air each site's cooling plant is working against.
 *
 * Positions are asset data (migration 0100): set by an operator - here, by
 * clicking the map - or the centroid of the site's metro when only the city is
 * known, drawn with a dashed ring and called approximate. Nothing is geocoded
 * over the network, so the plain map draws on an air-gapped install.
 *
 * Two basemaps. "Map" is Natural Earth vectors on an Equal Earth projection,
 * shipped in this chunk. "Satellite" switches to Web Mercator and draws NASA
 * Blue Marble tiles, with optional live cloud (GOES GeoColor) and rain (radar
 * composite) loops - see satellite.ts for what those are and are not.
 *
 * The map zooms and pans: wheel around the pointer, drag, the +/- buttons, or
 * the keyboard when it has focus. Markers keep their size at every zoom.
 */

type Fit = 'sites' | 'world';
type Base = 'map' | 'sat';
type Tone = 'critical' | 'major' | 'warn' | 'ok' | 'unknown';

const K_MIN = 1;
const K_MAX = 40;
// The 1:50m coastline loads once the map is drawn larger than about twice
// the whole-world fit - by zooming, or because "Fit sites" framed one country.
const DETAIL_SCALE = 0.5;
const IDENTITY: View = { k: 1, x: 0, y: 0 };
const FRAME_MS = 700;

const toLand = (t: unknown) => feature(t as Topology, (t as Topology).objects.countries as GeometryCollection);
const LAND_110 = toLand(countries110m);

function tone(s: SiteRow): Tone {
  if (s.alarms.critical > 0) return 'critical';
  if (s.alarms.major > 0) return 'major';
  if (s.alarms.minor > 0) return 'warn';
  return s.device_count > 0 ? 'ok' : 'unknown';
}

const TONE_LABEL: Record<Tone, string> = {
  critical: 'critical alarm open', major: 'major alarm open', warn: 'minor alarm open',
  ok: 'no open alarms', unknown: 'nothing monitored',
};
const LEGEND: { tone: Tone; label: string }[] = [
  { tone: 'critical', label: 'critical' }, { tone: 'major', label: 'major' },
  { tone: 'warn', label: 'minor' }, { tone: 'ok', label: 'clear' },
];

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function placed(s: SiteRow): s is SiteRow & { latitude: number; longitude: number } {
  return s.latitude != null && s.longitude != null;
}

/** Alarm counts as a sentence: "1 major · 2 minor", or "no open alarms". */
function alarmText(s: SiteRow): string {
  const parts = [[s.alarms.critical, 'critical'], [s.alarms.major, 'major'], [s.alarms.minor, 'minor']] as const;
  const open = parts.filter(([n]) => n > 0).map(([n, w]) => `${n} ${w}`);
  return open.length ? open.join(' · ') : 'no open alarms';
}

type IconKind = 'plus' | 'minus' | 'play' | 'pause' | 'building' | 'floor' | 'network' | 'warning' | 'open';
const ICON_PATHS: Record<IconKind, string> = {
  plus: 'M10 4v12M4 10h12',
  minus: 'M4 10h12',
  play: 'M6 4l10 6-10 6z',
  pause: 'M6 4v12M14 4v12',
  building: 'M4 17V5l6-2v14M10 17V8l6-2v11M4 17h12M7 8h.01M7 11h.01M7 14h.01M13 10h.01M13 13h.01',
  floor: 'M3 4h14v12H3zM3 10h14M9 4v6M12 10v6',
  network: 'M10 3v4M10 7l-5 4M10 7l5 4M3 11h4v4H3zM13 11h4v4h-4zM8 3h4v4H8z',
  warning: 'M10 3.5l7.5 13h-15zM10 8.5v3.5M10 14.3h.01',
  open: 'M8 4H4v12h12v-4M11 4h5v5M16 4l-7 7',
};

/* ----------------------------------------------------------------- warnings
 * Official warnings in effect over a site - the national weather service's
 * watches and warnings, and GDACS disaster events nearby. Read by the
 * platform (services/hazards); the map only shows them. Severity words are
 * the issuers' own: extreme / severe / moderate / minor. */
const SEV_RANK: Record<HazardSeverity, number> = { extreme: 0, severe: 1, moderate: 2, minor: 3 };
const worstOf = (alerts: HazardAlert[]): HazardAlert | null =>
  alerts.reduce<HazardAlert | null>((w, a) => (!w || SEV_RANK[a.severity] < SEV_RANK[w.severity] ? a : w), null);
/** "until 18:00" / "until Thu 18:00" - the issuer's end time, in the viewer's clock. */
function untilText(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const sameDay = d.toDateString() === new Date().toDateString();
  const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return `until ${sameDay ? t : `${d.toLocaleDateString([], { weekday: 'short' })} ${t}`}`;
}
function Icon({ kind }: { kind: IconKind }) {
  return (
    <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden focusable="false">
      <path d={ICON_PATHS[kind]} stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"
            fill={kind === 'play' ? 'currentColor' : 'none'} />
    </svg>
  );
}

/** `v=k/x/y` in the URL: the zoomed view, rounded so the link stays short. */
function parseView(raw: string | null): View {
  const p = (raw ?? '').split('/').map(Number);
  return p.length === 3 && p.every(Number.isFinite) && p[0] >= K_MIN && p[0] <= K_MAX
    ? { k: p[0], x: p[1], y: p[2] } : IDENTITY;
}

const reducedMotion = () => typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches;

export function WorldMap() {
  const qc = useQueryClient();
  // Basemap, layers, fit, zoom and selection live in the URL, so a view can
  // be shared and the back button behaves.
  const [params, setParams] = useSearchParams();
  const base: Base = params.get('base') === 'sat' ? 'sat' : 'map';
  const wx = useMemo(() => new Set((params.get('wx') ?? '').split(',').filter((x) => x === 'clouds' || x === 'rain')), [params]);
  const [fit, setFitState] = useState<Fit>(params.get('fit') === 'world' ? 'world' : 'sites');
  const [view, setView] = useState<View>(() => parseView(params.get('v')));
  const [selected, setSelectedState] = useState<string | null>(params.get('site'));
  // Functional update: the debounced view write must not clobber a key set since.
  const setParam = (k: string, v: string | null) => setParams((prev) => {
    const next = new URLSearchParams(prev);
    if (v == null) next.delete(k); else next.set(k, v);
    return next;
  }, { replace: true });
  const setBase = (b: Base) => { setParam('base', b === 'map' ? null : b); setView(IDENTITY); };
  const toggleWx = (layer: 'clouds' | 'rain') => {
    const next = new Set(wx);
    if (next.has(layer)) next.delete(layer); else next.add(layer);
    setParam('wx', next.size ? [...next].join(',') : null);
    // Rain on the plain map changes its projection; start the view afresh.
    if (layer === 'rain' && base === 'map') setView(IDENTITY);
  };
  const setFit = (f: Fit) => { setFitState(f); setParam('fit', f === 'sites' ? null : f); };
  const setSelected = (id: string | null) => { setSelectedState(id); setParam('site', id); };
  // The view is written once zooming settles - a wheel spin is dozens of steps.
  useEffect(() => {
    const id = window.setTimeout(() => {
      const v = view.k === 1 && view.x === 0 && view.y === 0
        ? null : `${view.k.toFixed(2)}/${Math.round(view.x)}/${Math.round(view.y)}`;
      if ((params.get('v') ?? null) !== v) setParam('v', v);
    }, 400);
    return () => window.clearTimeout(id);
  }, [view]); // eslint-disable-line react-hooks/exhaustive-deps

  const [placing, setPlacing] = useState<string | null>(null);         // site id being placed
  const [pending, setPending] = useState<{ lat: string; lon: string } | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [land50, setLand50] = useState<ReturnType<typeof toLand> | null>(null);
  const { bind, tipEl } = useHoverTip();
  const frame = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const drag = useRef<{ x: number; y: number; vx: number; vy: number; moved: boolean } | null>(null);
  const [size, setSize] = useState({ w: 960, h: 520 });
  useTempUnit();   // re-render when the viewer flips °C/°F

  const { data, error, isLoading } = useQuery<SitesOverview>({
    queryKey: ['sites-overview'],
    queryFn: () => api.sitesOverview(),
    refetchInterval: 30_000,
  });

  // The SVG is drawn at the panel's measured size, never a scaled viewBox:
  // marker radii and labels must stay the same size at every width.
  useEffect(() => {
    const el = frame.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => {
      const w = Math.max(320, Math.round(e.contentRect.width));
      setSize({ w, h: Math.round(Math.min(620, Math.max(300, w * 0.52))) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [isLoading]);   // the panel only exists once the sites have loaded


  const sites = useMemo(() => data?.sites ?? [], [data]);
  const onMap = useMemo(() => sites.filter(placed), [sites]);
  const unplaced = sites.filter((s) => !placed(s));

  // Each site's own weather: the dry/wet bulb its cooling towers read. The
  // same endpoint the site page uses, so the two never disagree.
  const kpis = useQueries({
    queries: sites.map((s) => ({
      queryKey: ['site-kpi', s.id], queryFn: () => api.siteKpi(s.id),
      staleTime: 60_000, refetchInterval: 120_000,
    })),
  });
  const hz = useQuery({
    queryKey: ['site-hazards'], queryFn: () => api.siteHazards(),
    staleTime: 4 * 60_000, refetchInterval: 5 * 60_000,
  });
  const alertsOf = (s: SiteRow): HazardAlert[] => hz.data?.sites[s.id]?.alerts ?? [];
  const weather = useMemo(() => {
    const m = new Map<string, SiteKpi['weather']>();
    sites.forEach((s, i) => { const d = kpis[i]?.data; if (d) m.set(s.id, d.weather); });
    return m;
  }, [sites, kpis]);

  // Raster tiles are Web Mercator, so any raster layer puts the map in
  // Mercator; the plain map without one keeps Equal Earth.
  const mercator = base === 'sat' || wx.has('rain');
  const projection = useMemo(() => {
    const p = mercator ? geoMercator() : geoEqualEarth();
    const pad = 24;
    const extent: [[number, number], [number, number]] = [[pad, pad], [size.w - pad, size.h - pad]];
    if (fit === 'sites' && onMap.length) {
      // Frame the sites with a margin of a few degrees, so two sites in one
      // country do not fill the window and one site is not a single point.
      // The ring runs CLOCKWISE (north, then east): d3 treats a spherical
      // polygon's exterior as clockwise, and the other way round means
      // "everything but this box" - which fits the whole world.
      const lons = onMap.map((s) => s.longitude), lats = onMap.map((s) => s.latitude);
      const m = 8;
      const w = Math.max(-179.9, Math.min(...lons) - m), e = Math.min(179.9, Math.max(...lons) + m);
      const so = Math.max(-80, Math.min(...lats) - m), n = Math.min(80, Math.max(...lats) + m);
      const box: GeoPermissibleObjects = {
        type: 'Polygon',
        coordinates: [[[w, so], [w, n], [e, n], [e, so], [w, so]]],
      };
      p.fitExtent(extent, box);
    } else {
      p.fitExtent(extent, { type: 'Sphere' });
    }
    return p;
  }, [mercator, fit, size.w, size.h, onMap]);
  const path = useMemo(() => geoPath(projection), [projection]);
  const fine = projection.scale() * view.k > DETAIL_SCALE * size.w;
  // The finer coastline, only once the coarse one would show.
  useEffect(() => {
    if (!fine || land50) return;
    let live = true;
    import('world-atlas/countries-50m.json').then((m) => { if (live) setLand50(toLand(m.default ?? m)); });
    return () => { live = false; };
  }, [fine, land50]);
  const landPath = useMemo(() => path((fine && land50) || LAND_110) ?? '', [path, fine, land50]);

  // --- satellite view ---------------------------------------------------------
  const sat = base === 'sat';
  const reachable = useImageryReachable(sat);
  const marbleTiles = useMemo(() => (sat ? tilesFor(projection, view, size.w, size.h, Z_MAX.marble) : []),
    [sat, projection, view, size.w, size.h]);
  const cloudTiles = useMemo(() => (sat && wx.has('clouds') ? tilesFor(projection, view, size.w, size.h, Z_MAX.clouds) : []),
    [sat, wx, projection, view, size.w, size.h]);
  const radarTiles = useMemo(() => (wx.has('rain') ? tilesFor(projection, view, size.w, size.h, Z_MAX.radar) : []),
    [wx, projection, view, size.w, size.h]);
  const clouds = sat && wx.has('clouds');
  const animated = wx.has('rain') || clouds;
  const radar = useRadarIndex(wx.has('rain'));

  // The frame clock: recomputed every ten minutes so a wall display creeps
  // forward with the imagery.
  const [clockTick, setClockTick] = useState(0);
  useEffect(() => {
    if (!animated) return;
    const id = window.setInterval(() => setClockTick((t) => t + 1), 10 * 60_000);
    return () => window.clearInterval(id);
  }, [animated]);
  const frames = useMemo(() => frameTimes(), [clockTick]); // eslint-disable-line react-hooks/exhaustive-deps
  const [fi, setFi] = useState(frames.length - 1);
  const [playing, setPlaying] = useState(() => !reducedMotion());
  useEffect(() => { setFi(frames.length - 1); }, [frames]);
  useEffect(() => {
    if (!animated || !playing) return;
    const id = window.setInterval(() => setFi((i) => (i + 1) % frames.length), FRAME_MS);
    return () => window.clearInterval(id);
  }, [animated, playing, frames.length]);
  const at = frames[Math.min(fi, frames.length - 1)];
  const radarPath = (t: number) => radar.data?.byTime.get(t) ?? null;

  // Warm every frame's tiles for the current viewport, so the loop's first
  // pass is not a slideshow of half-loaded images.
  useEffect(() => {
    if (!animated) return;
    const urls: string[] = [];
    for (const t of frames) {
      if (clouds) for (const tile of cloudTiles) urls.push(geoColorUrl('West', t, tile), geoColorUrl('East', t, tile));
      const p = radar.data && radarPath(t);
      if (p) for (const tile of radarTiles) urls.push(radarUrl(radar.data!.host, p, tile));
    }
    const id = window.setTimeout(() => prefetch(urls), 300);   // not while the wheel is still turning
    return () => window.clearTimeout(id);
  }, [animated, clouds, frames, cloudTiles, radarTiles, radar.data]); // eslint-disable-line react-hooks/exhaustive-deps

  // --- zoom and pan ---------------------------------------------------------
  const zoomAt = useCallback((factor: number, px: number, py: number) => {
    setView((v) => {
      const k = Math.max(K_MIN, Math.min(K_MAX, v.k * factor));
      if (k === v.k) return v;
      return { k, x: px - (px - v.x) * (k / v.k), y: py - (py - v.y) * (k / v.k) };
    });
  }, []);
  const local = (e: { clientX: number; clientY: number }) => {
    const r = svgRef.current?.getBoundingClientRect();
    return r ? [e.clientX - r.left, e.clientY - r.top] as const : [0, 0] as const;
  };
  // Wheel has to be non-passive to stop the page scrolling under the map.
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const [px, py] = local(e);
      zoomAt(Math.exp(-e.deltaY * 0.0015), px, py);
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
    // isLoading: the map is not in the DOM until the sites arrive, and the
    // listener has to attach to it once it is.
  }, [zoomAt, isLoading]);

  const onPointerDown = (e: React.PointerEvent<SVGSVGElement>) => {
    if (e.button !== 0) return;
    drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false };
  };
  const onPointerMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x, dy = e.clientY - d.y;
    if (!d.moved && Math.hypot(dx, dy) < 4) return;        // a click is not a drag
    if (!d.moved) { d.moved = true; e.currentTarget.setPointerCapture(e.pointerId); }
    setView((v) => ({ ...v, x: d.vx + dx, y: d.vy + dy }));
  };
  const onPointerUp = (e: React.PointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    drag.current = null;
    if (!d || d.moved || !placing) return;
    // A click while placing: where on Earth is that?
    const [px, py] = local(e);
    const ll = projection.invert?.([(px - view.x) / view.k, (py - view.y) / view.k]);
    if (!ll || !Number.isFinite(ll[0]) || !Number.isFinite(ll[1])) return;
    setPending({ lat: ll[1].toFixed(4), lon: ll[0].toFixed(4) });
    setSaveError(null);
  };
  const centre = () => [size.w / 2, size.h / 2] as const;
  const onKey = (e: React.KeyboardEvent<SVGSVGElement>) => {
    const [cx, cy] = centre();
    const step = 60;
    switch (e.key) {
      case '+': case '=': zoomAt(1.5, cx, cy); break;
      case '-': case '_': zoomAt(1 / 1.5, cx, cy); break;
      case '0': setView(IDENTITY); break;
      case 'ArrowLeft': setView((v) => ({ ...v, x: v.x + step })); break;
      case 'ArrowRight': setView((v) => ({ ...v, x: v.x - step })); break;
      case 'ArrowUp': setView((v) => ({ ...v, y: v.y + step })); break;
      case 'ArrowDown': setView((v) => ({ ...v, y: v.y - step })); break;
      case 'Escape': if (placing) { setPlacing(null); setPending(null); } break;
      default: return;
    }
    e.preventDefault();
  };
  const fitTo = (f: Fit) => { setFit(f); setView(IDENTITY); };
  const atRest = view.k === 1 && view.x === 0 && view.y === 0;

  // --- placing a site -----------------------------------------------------
  const placingSite = sites.find((s) => s.id === placing) ?? null;
  const save = useMutation({
    mutationFn: ({ id, lat, lon }: { id: string; lat: number; lon: number }) => api.setSiteLocation(id, lat, lon),
    onSuccess: () => {
      setPlacing(null); setPending(null); setSaveError(null);
      qc.invalidateQueries({ queryKey: ['sites-overview'] });
      // The warnings are looked up by the site's point, so a new position
      // means a new answer - fetch it now rather than at the next 5-min tick.
      qc.invalidateQueries({ queryKey: ['site-hazards'] });
    },
    onError: (e) => setSaveError(
      e instanceof ApiError && e.status === 403 ? 'Setting a position needs the operator role.'
        : e instanceof ApiError && e.status === 422 ? 'Latitude must be -90 to 90 and longitude -180 to 180.'
        : `The position was not saved: ${e instanceof Error ? e.message : String(e)}`),
  });
  const pendingLat = pending ? Number(pending.lat) : NaN;
  const pendingLon = pending ? Number(pending.lon) : NaN;
  const pendingValid = Number.isFinite(pendingLat) && Number.isFinite(pendingLon)
    && Math.abs(pendingLat) <= 90 && Math.abs(pendingLon) <= 180;
  const pendingXY = pendingValid ? projection([pendingLon, pendingLat]) : null;

  if (isLoading) return <p className="muted">Loading sites…</p>;
  if (error) return <p className="muted">Sites could not be loaded.</p>;

  const inv = 1 / view.k;
  // Labels read to the right of the dot; one that would run into a neighbour's
  // dot flips to the left. Screen space, so zooming in un-flips them.
  const screen = onMap.map((s) => ({ s, xy: projection([s.longitude, s.latitude]) }))
    .filter((p): p is { s: typeof p.s; xy: [number, number] } => !!p.xy)
    .map(({ s, xy }) => ({ id: s.id, x: xy[0] * view.k, y: xy[1] * view.k, w: 20 + s.code.length * 8 }));
  const flipLeft = new Set(screen.filter((a) => screen.some((b) =>
    b.id !== a.id && b.x > a.x - 4 && b.x - a.x < a.w && Math.abs(b.y - a.y) < 30)).map((a) => a.id));
  const tempOf = (s: SiteRow) => {
    const w = weather.get(s.id);
    return w?.available && w.dry_bulb_c != null ? fmtT(w.dry_bulb_c, 0) : null;
  };
  const withAlarms = sites.filter((s) => s.alarms.total > 0).length;
  const withWarnings = sites.filter((s) => alertsOf(s).length > 0).length;
  const offline = sat && reachable.data === false;

  return (
    <div className="stack world-page">
      <div className="world-head">
        <h2>World map</h2>
        <p className="muted world-sub">
          {plural(sites.length, 'site')}{withAlarms ? `, ${withAlarms} with open alarms` : ', no open alarms'}
          {withWarnings ? `, ${withWarnings} under an official weather warning` : ''}
          {unplaced.length ? `, ${unplaced.length} not yet placed` : ''}.
        </p>
      </div>

      <div className="world-body">
        <div className="asset-panel world-panel" ref={frame}>
          <div className="world-bar">
            <div className="world-bar-group">
              <div className="world-seg" role="group" aria-label="Basemap">
                <button type="button" className={!sat ? 'is-on' : undefined} aria-pressed={!sat}
                        onClick={() => setBase('map')} title="Vector map, drawn from data shipped with the app">Map</button>
                <button type="button" className={sat ? 'is-on' : undefined} aria-pressed={sat}
                        onClick={() => setBase('sat')} title="NASA Blue Marble imagery, fetched from the internet">Satellite</button>
              </div>
              <div className="world-layers" role="group" aria-label="Weather layers">
                <button type="button" aria-pressed={wx.has('rain')} onClick={() => toggleWx('rain')}
                        title="Ground weather radar composite (RainViewer), past 2 hours. Draws on a Mercator map.">
                  <span className="sw sw-rain" aria-hidden />Rain</button>
                <button type="button" aria-pressed={clouds} onClick={() => toggleWx('clouds')} disabled={!sat}
                        title={sat
                          ? 'GOES-East and GOES-West GeoColor, a frame every 20 minutes over the past 2 hours'
                          : 'Cloud imagery is a photograph of land and sea too, so it needs the Satellite basemap'}>
                  <span className="sw sw-clouds" aria-hidden />Clouds</button>
              </div>
            </div>
            <div className="world-bar-group">
              <div className="world-seg" role="group" aria-label="Framing">
                <button type="button" className={fit === 'sites' && atRest ? 'is-on' : undefined}
                        aria-pressed={fit === 'sites' && atRest}
                        onClick={() => fitTo('sites')} title="Frame every placed site">Fit sites</button>
                <button type="button" className={fit === 'world' && atRest ? 'is-on' : undefined}
                        aria-pressed={fit === 'world' && atRest}
                        onClick={() => fitTo('world')} title="Show the whole globe">Whole world</button>
              </div>
              <div className="world-zoom">
                <button type="button" aria-label="Zoom in" title="Zoom in (+)"
                        onClick={() => zoomAt(1.5, ...centre())} disabled={view.k >= K_MAX}><Icon kind="plus" /></button>
                <button type="button" aria-label="Zoom out" title="Zoom out (-)"
                        onClick={() => zoomAt(1 / 1.5, ...centre())} disabled={view.k <= K_MIN}><Icon kind="minus" /></button>
              </div>
            </div>
          </div>

          {placingSite && (
            <div className="world-placing" role="status">
              {!pending
                ? <>Click the map where <b>{placingSite.code}</b> stands. Zoom in for precision. <kbd>Esc</kbd> cancels.</>
                : (
                  <form className="world-confirm" onSubmit={(e) => {
                    e.preventDefault();
                    if (pendingValid) save.mutate({ id: placingSite.id, lat: pendingLat, lon: pendingLon });
                  }}>
                    <span>Set <b>{placingSite.code}</b> to</span>
                    <label>Latitude <input inputMode="decimal" value={pending.lat}
                                          onChange={(e) => setPending({ ...pending, lat: e.target.value })} /></label>
                    <label>Longitude <input inputMode="decimal" value={pending.lon}
                                           onChange={(e) => setPending({ ...pending, lon: e.target.value })} /></label>
                    <button type="submit" className="primary" disabled={!pendingValid || save.isPending}>
                      {save.isPending ? 'Saving…' : 'Save position'}</button>
                    <button type="button" onClick={() => { setPlacing(null); setPending(null); setSaveError(null); }}>Cancel</button>
                  </form>
                )}
              {saveError && <p className="world-error" role="alert">{saveError}</p>}
            </div>
          )}

          {offline && (
            <p className="world-offline-note" role="status">
              Satellite imagery needs internet access from this browser, and the NASA tile service did not answer.
              The sites are still drawn; switch to Map for the basemap.
            </p>
          )}

          <svg ref={svgRef} width={size.w} height={size.h}
               className={`world-svg${placing ? ' is-placing' : ''}${sat ? ' is-sat' : ''}`}
               role="application" tabIndex={0}
               aria-label={`World map of ${plural(onMap.length, 'site')}, coloured by worst open alarm. `
                 + 'Plus and minus zoom, arrow keys pan, 0 resets.'}
               onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}
               onPointerCancel={() => { drag.current = null; }} onKeyDown={onKey}>
            <g transform={`translate(${view.x},${view.y}) scale(${view.k})`}>
              <path d={path({ type: 'Sphere' }) ?? ''} className="world-sphere" />
              {sat ? (
                <>
                  <g className="world-tiles">
                    {marbleTiles.map((t) => (
                      <image key={`m${t.z}/${t.x}/${t.y}`} href={blueMarbleUrl(t)} x={t.px} y={t.py}
                             width={t.size} height={t.size} preserveAspectRatio="none" />))}
                  </g>
                  {clouds && (
                    <g className="world-tiles">
                      {(['West', 'East'] as const).map((s) => cloudTiles.map((t) => (
                        <image key={`c${s}${t.z}/${t.x}/${t.y}`} href={geoColorUrl(s, at, t)} x={t.px} y={t.py}
                               width={t.size} height={t.size} preserveAspectRatio="none" />)))}
                    </g>
                  )}
                  <path d={landPath} className="world-borders" />
                </>
              ) : (
                <>
                  <path d={path(geoGraticule10()) ?? ''} className="world-graticule" />
                  <path d={landPath} className="world-land" />
                </>
              )}
              {wx.has('rain') && radar.data && radarPath(at) && (
                <g className="world-tiles world-radar">
                  {radarTiles.map((t) => (
                    <image key={`r${t.z}/${t.x}/${t.y}`} href={radarUrl(radar.data!.host, radarPath(at)!, t)}
                           x={t.px} y={t.py} width={t.size} height={t.size} preserveAspectRatio="none" />))}
                </g>
              )}
              {onMap.map((s) => {
                const xy = projection([s.longitude, s.latitude]);
                if (!xy) return null;
                const t = tone(s);
                const approx = s.location_source !== 'manual';
                const temp = tempOf(s);
                const left = flipLeft.has(s.id);
                const warn = worstOf(alertsOf(s));
                return (
                  <g key={s.id} transform={`translate(${xy[0]},${xy[1]}) scale(${inv})`}
                     className={`world-site tone-${t}${selected === s.id ? ' is-selected' : ''}`}
                     role="button" tabIndex={placing ? -1 : 0}
                     aria-label={`${s.code}, ${TONE_LABEL[t]}${temp ? `, outdoor ${temp}` : ''}${warn ? `, ${warn.event} in effect` : ''}`}
                     onClick={(e) => { if (!placing) { e.stopPropagation(); setSelected(s.id === selected ? null : s.id); } }}
                     onKeyDown={(e) => {
                       if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); setSelected(s.id === selected ? null : s.id); }
                     }}
                     {...bind(<><b>{s.code}</b> {s.city ?? ''}{s.country ? `, ${s.country}` : ''}
                       {' · '}{TONE_LABEL[t]}{temp ? ` · outdoor ${temp}` : ''}
                       {warn ? ` · ${warn.event} (${warn.issuer ?? warn.source.toUpperCase()})` : ''}
                       {approx ? ' · position approximate' : ''}</>)}>
                    {warn && <circle r={15} className={`world-warnring sev-${warn.severity}`} />}
                    {approx && <circle r={12} className="world-approx" />}
                    <circle r={7} className="world-dot" />
                    <text x={left ? -11 : 11} y={-9} className="world-label" textAnchor={left ? 'end' : 'start'}>{s.code}</text>
                    {temp && <text x={left ? -11 : 11} y={4} className="world-temp" textAnchor={left ? 'end' : 'start'}>{temp}</text>}
                  </g>
                );
              })}
              {pendingXY && (
                <g transform={`translate(${pendingXY[0]},${pendingXY[1]}) scale(${inv})`} className="world-pin" aria-hidden>
                  <circle r={10} /><path d="M-15 0h8M7 0h8M0 -15v8M0 7v8" />
                </g>
              )}
            </g>
          </svg>
          {tipEl}

          {animated && (
            <div className="world-time">
              <button type="button" className="world-play" aria-pressed={playing} aria-label={playing ? 'Pause the loop' : 'Play the loop'}
                      onClick={() => setPlaying((p) => !p)}><Icon kind={playing ? 'pause' : 'play'} /></button>
              <input type="range" min={0} max={frames.length - 1} step={1} value={Math.min(fi, frames.length - 1)}
                     aria-label="Weather frame" aria-valuetext={utcClock(at)}
                     onChange={(e) => { setPlaying(false); setFi(Number(e.target.value)); }} />
              <span className="world-time-at"><b>{utcClock(at)}</b> <span className="muted">{ago(Date.now() / 1000 - at)}</span></span>
              <span className="muted world-time-note">
                {clouds && 'Clouds cover the GOES-East and GOES-West discs. '}
                {wx.has('rain') && (radar.isError ? 'Radar index unreachable. ' : 'Rain shows where ground radar reports. ')}
              </span>
            </div>
          )}

          <div className="world-foot">
            <ul className="world-legend" aria-label="Marker colours">
              {LEGEND.map((l) => <li key={l.tone}><span className={`sw tone-${l.tone}`} aria-hidden />{l.label}</li>)}
              <li><span className="sw sw-approx" aria-hidden />approximate position</li>
              <li><span className="sw sw-warn" aria-hidden />official warning in effect</li>
            </ul>
            <p className="muted world-note">
              {sat
                ? <>Imagery NASA GIBS: Blue Marble{clouds ? ', NOAA GOES GeoColor' : ''}{wx.has('rain') ? '; radar RainViewer' : ''}. Borders Natural Earth.</>
                : <>Natural Earth 1:{fine && land50 ? '50' : '110'}m{wx.has('rain') ? ', Mercator; radar RainViewer' : ''}. Scroll or + − to zoom, drag to move.</>}
              {hz.data?.available === false
                ? <> Warnings unavailable: {hz.data.note}.</>
                : hz.data ? <> Warnings: {hz.data.sources.filter((x) => x.ok).map((x) => x.name).join(', ') || 'none reachable'}.</> : null}
            </p>
          </div>
        </div>

        <aside className="asset-panel world-side">
          <h3>Sites <span className="total">{sites.length}</span></h3>
          <ul className="world-list">
            {sites.map((s) => {
              const open = selected === s.id;
              const w = weather.get(s.id);
              const t = tone(s);
              return (
                <li key={s.id} className={open ? 'is-open' : undefined}>
                  <button type="button" className="world-row" aria-expanded={open}
                          onClick={() => setSelected(open ? null : s.id)}>
                    <span className={`world-swatch tone-${t}`} aria-hidden />
                    <span className="world-row-main">
                      <span className="world-row-title"><b>{s.code}</b> <span className="muted">{[s.city, s.country].filter(Boolean).join(', ') || 'city not set'}</span></span>
                      <span className={`world-row-sub${t === 'ok' || t === 'unknown' ? ' muted' : ` is-${t}`}`}>
                        {alarmText(s)}
                        {s.offline_count > 0 && <span className="world-offline"> · {s.offline_count} offline</span>}
                      </span>
                    </span>
                    <span className="world-row-wx" aria-label={w?.available ? 'outdoor air' : undefined}>
                      {w?.available && w.dry_bulb_c != null
                        ? <><b>{fmtT(w.dry_bulb_c, 1)}</b><span className="muted">{w.humidity_pct != null ? `${Math.round(w.humidity_pct)} % RH` : '–'}</span></>
                        : <span className="muted world-row-nowx">{w ? 'no outdoor air' : '…'}</span>}
                    </span>
                    {(() => { const hw = worstOf(alertsOf(s)); return hw && (
                      <span className={`world-row-hz sev-${hw.severity}`}
                            title={`${hw.event}${alertsOf(s).length > 1 ? ` and ${alertsOf(s).length - 1} more` : ''} ${untilText(hw.ends)}`}>
                        <Icon kind="warning" />
                        <span className="world-row-hz-text">{hw.event}{alertsOf(s).length > 1 ? ` +${alertsOf(s).length - 1}` : ''}
                          <span className="muted"> {untilText(hw.ends)}</span></span>
                      </span>); })()}
                  </button>
                  {open && (
                    <SiteDetail site={s} weather={w} alerts={alertsOf(s)} covered={hz.data?.sites[s.id]?.covered ?? null}
                                feedsUp={hz.data?.available ?? null} placing={placing === s.id}
                                onPlace={() => { setPlacing(s.id); setPending(null); setSaveError(null); svgRef.current?.focus(); }} />
                  )}
                </li>
              );
            })}
          </ul>
          {!selected && <p className="muted world-hint">Select a site for its position, devices, outdoor air and links.</p>}

          {unplaced.length > 0 && (
            <p className="muted">
              Not on the map: {unplaced.map((s) => s.code).join(', ')}. No position is set and the city is not
              a known metro; select the site and set its position on the map.
            </p>
          )}
        </aside>
      </div>
    </div>
  );
}

function SiteDetail({ site, weather, alerts, covered, feedsUp, placing, onPlace }: {
  site: SiteRow; weather: SiteKpi['weather'] | undefined; alerts: HazardAlert[]; covered: boolean | null;
  feedsUp: boolean | null; placing: boolean; onPlace: () => void;
}) {
  const hall = site.rooms.find((r) => r.room_class === 'white_space') ?? site.rooms[0];
  const approx = site.location_source !== 'manual';
  const stale = weather?.age_s != null && weather.age_s > 900;
  return (
    <div className="world-detail" role="region" aria-label={`${site.code} details`}>
      <dl>
        <dt>Position</dt>
        <dd>
          {site.latitude != null && site.longitude != null
            ? `${site.latitude.toFixed(4)}, ${site.longitude.toFixed(4)}`
            : 'not set'}
          {approx && site.latitude != null && <span className="muted"> (city centre)</span>}
        </dd>
        <dt>Devices</dt>
        <dd>{site.online_count} online of {site.device_count - (site.passive_count ?? 0)} monitored
          {site.offline_count > 0 && <span className="muted"> · {site.offline_count} offline</span>}
          {(site.passive_count ?? 0) > 0 && (
            <span className="muted" title="Panelboards and other equipment with nothing to poll; their circuits are metered by the meters clamped onto them">
              {' · '}{plural(site.passive_count ?? 0, 'passive panel')}</span>
          )}</dd>
        <dt>Open alarms</dt>
        <dd>{site.alarms.critical} critical · {site.alarms.major} major · {site.alarms.minor} minor</dd>
        <dt>Rooms</dt>
        <dd>{site.rooms.length}</dd>
        <dt>Outdoor air</dt>
        <dd className={stale ? 'muted' : undefined}>
          {weather == null ? 'loading…'
            : !weather.available ? (weather.note ?? 'no reading')
            : <>
              {fmtT(weather.dry_bulb_c, 1)} dry bulb · {fmtT(weather.wet_bulb_c, 1)} wet bulb
              {weather.humidity_pct != null && (
                <> · {Math.round(weather.humidity_pct)} % RH<span className="muted" title={weather.humidity_note ?? undefined}> (derived)</span></>)}
              <span className="muted world-wx-src">
                {weather.source ?? 'site BMS'}{weather.age_s != null ? `, ${ago(weather.age_s)}` : ''}{stale ? ' - stale' : ''}
              </span>
            </>}
        </dd>
      </dl>
      <div className="world-hz">
        <h5>Official warnings</h5>
        {alerts.length > 0 ? (
          <ul>
            {alerts.map((a) => (
              <li key={a.id} className={`sev-${a.severity}`}>
                <span className="world-hz-head"><Icon kind="warning" /><b>{a.event}</b>
                  <span className="world-hz-sev">{a.severity}</span></span>
                <span className="world-hz-body">
                  {a.headline && a.headline !== a.event ? a.headline : a.area}
                  {a.distance_km != null ? ` · ${a.distance_km} km away` : ''}
                </span>
                <span className="muted world-hz-meta">
                  {a.issuer ?? a.source.toUpperCase()}{a.ends ? ` · ${untilText(a.ends)}` : ''}
                  {a.url && <> · <a href={a.url} target="_blank" rel="noreferrer noopener">report<Icon kind="open" /></a></>}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">
            {feedsUp == null ? 'checking…'
              : feedsUp === false ? 'warning feeds unreachable from the platform'
              : covered === false ? 'none from GDACS; the national feed does not cover this country'
              : 'none in effect'}
          </p>
        )}
      </div>
      <button type="button" className="world-place" onClick={onPlace} disabled={placing}>
        {placing ? 'Click the map…' : approx ? 'Set exact position' : 'Move position'}
      </button>
      <nav className="world-links" aria-label={`${site.code} pages`}>
        <Link to={`/floorplan?site=${site.id}`}><Icon kind="building" />Building</Link>
        {hall
          ? <Link to={`/floorplan?room=${hall.id}`}><Icon kind="floor" />Floor map</Link>
          : <span className="is-off" title="No room at this site yet"><Icon kind="floor" />Floor map</span>}
        <Link to="/connectivity"><Icon kind="network" />Network map</Link>
      </nav>
    </div>
  );
}

export default WorldMap;
