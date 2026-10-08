import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { geoEqualEarth, geoGraticule10, geoPath, type GeoPermissibleObjects } from 'd3-geo';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { feature } from 'topojson-client';
import type { GeometryCollection, Topology } from 'topojson-specification';
import countries110m from 'world-atlas/countries-110m.json';
import { api, ApiError, type SiteRow, type SitesOverview } from '../../api/client';
import { useHoverTip } from '../../components/HoverTip';
import './world.css';

/**
 * Every site on the globe, coloured by the worst open alarm it holds.
 *
 * Positions are asset data (migration 0100): set by an operator - here, by
 * clicking the map - or the centroid of the site's metro when only the city is
 * known, drawn with a dashed ring and called approximate. Nothing is geocoded
 * over the network, so the map draws on an air-gapped install.
 *
 * The map zooms and pans: wheel around the pointer, drag, the +/- buttons, or
 * the keyboard when it has focus. Markers keep their size at every zoom. The
 * basemap is Natural Earth 1:110m, swapped for 1:50m once zoomed in far enough
 * for the coarse coastline to show; both ship inside this route's chunk only.
 */

type Fit = 'sites' | 'world';
type Tone = 'critical' | 'major' | 'warn' | 'ok' | 'unknown';
interface View { k: number; x: number; y: number }

const K_MIN = 1;
const K_MAX = 40;
const DETAIL_AT = 3;          // zoom past which the 1:50m coastline loads
const IDENTITY: View = { k: 1, x: 0, y: 0 };

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

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function placed(s: SiteRow): s is SiteRow & { latitude: number; longitude: number } {
  return s.latitude != null && s.longitude != null;
}

function MapIcon({ kind }: { kind: 'plus' | 'minus' }) {
  return (
    <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden focusable="false">
      <path d={kind === 'plus' ? 'M10 4v12M4 10h12' : 'M4 10h12'} stroke="currentColor" strokeWidth="1.8"
            strokeLinecap="round" fill="none" />
    </svg>
  );
}

export function WorldMap() {
  const qc = useQueryClient();
  const [fit, setFit] = useState<Fit>('sites');
  const [view, setView] = useState<View>(IDENTITY);
  const [selected, setSelected] = useState<string | null>(null);
  const [placing, setPlacing] = useState<string | null>(null);         // site id being placed
  const [pending, setPending] = useState<{ lat: string; lon: string } | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [land50, setLand50] = useState<ReturnType<typeof toLand> | null>(null);
  const { bind, tipEl } = useHoverTip();
  const frame = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const drag = useRef<{ x: number; y: number; vx: number; vy: number; moved: boolean } | null>(null);
  const [size, setSize] = useState({ w: 960, h: 520 });

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

  // The finer coastline, only once somebody zooms in far enough to see the coarse one.
  useEffect(() => {
    if (view.k < DETAIL_AT || land50) return;
    let live = true;
    import('world-atlas/countries-50m.json').then((m) => { if (live) setLand50(toLand(m.default ?? m)); });
    return () => { live = false; };
  }, [view.k, land50]);

  const sites = useMemo(() => data?.sites ?? [], [data]);
  const onMap = useMemo(() => sites.filter(placed), [sites]);
  const unplaced = sites.filter((s) => !placed(s));

  const projection = useMemo(() => {
    const p = geoEqualEarth();
    const pad = 24;
    const extent: [[number, number], [number, number]] = [[pad, pad], [size.w - pad, size.h - pad]];
    if (fit === 'sites' && onMap.length) {
      // Frame the sites with a margin of a few degrees, so two sites in one
      // country do not fill the window and one site is not a single point.
      const lons = onMap.map((s) => s.longitude), lats = onMap.map((s) => s.latitude);
      const m = 8;
      const box: GeoPermissibleObjects = {
        type: 'Polygon',
        coordinates: [[
          [Math.min(...lons) - m, Math.min(...lats) - m], [Math.max(...lons) + m, Math.min(...lats) - m],
          [Math.max(...lons) + m, Math.max(...lats) + m], [Math.min(...lons) - m, Math.max(...lats) + m],
          [Math.min(...lons) - m, Math.min(...lats) - m],
        ]],
      };
      p.fitExtent(extent, box);
    } else {
      p.fitExtent(extent, { type: 'Sphere' });
    }
    return p;
  }, [fit, size.w, size.h, onMap]);
  const path = useMemo(() => geoPath(projection), [projection]);
  const landPath = useMemo(() => path((view.k >= DETAIL_AT && land50) || LAND_110) ?? '',
    [path, view.k >= DETAIL_AT, land50]); // eslint-disable-line react-hooks/exhaustive-deps

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
  const onKey = (e: React.KeyboardEvent) => {
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

  // --- placing a site -----------------------------------------------------
  const placingSite = sites.find((s) => s.id === placing) ?? null;
  const save = useMutation({
    mutationFn: ({ id, lat, lon }: { id: string; lat: number; lon: number }) => api.setSiteLocation(id, lat, lon),
    onSuccess: () => {
      setPlacing(null); setPending(null); setSaveError(null);
      qc.invalidateQueries({ queryKey: ['sites-overview'] });
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

  const chosen = sites.find((s) => s.id === selected) ?? null;

  if (isLoading) return <p className="muted">Loading sites…</p>;
  if (error) return <p className="muted">Sites could not be loaded.</p>;

  const inv = 1 / view.k;
  return (
    <div className="stack world-page">
      <div className="world-head">
        <h2>World map</h2>
      </div>

      <div className="world-body">
        <div className="asset-panel world-panel" ref={frame}>
          <div className="world-tools" role="toolbar" aria-label="Map view">
            <button type="button" className={fit === 'sites' && view === IDENTITY ? 'is-on' : undefined}
                    onClick={() => fitTo('sites')}>Fit sites</button>
            <button type="button" className={fit === 'world' && view === IDENTITY ? 'is-on' : undefined}
                    onClick={() => fitTo('world')}>Whole world</button>
            <span className="world-tools-sep" aria-hidden />
            <button type="button" aria-label="Zoom in" title="Zoom in (+)"
                    onClick={() => zoomAt(1.5, ...centre())} disabled={view.k >= K_MAX}><MapIcon kind="plus" /></button>
            <button type="button" aria-label="Zoom out" title="Zoom out (-)"
                    onClick={() => zoomAt(1 / 1.5, ...centre())} disabled={view.k <= K_MIN}><MapIcon kind="minus" /></button>
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

          <svg ref={svgRef} width={size.w} height={size.h}
               className={`world-svg${placing ? ' is-placing' : ''}`}
               role="application" tabIndex={0}
               aria-label={`World map of ${plural(onMap.length, 'site')}, coloured by worst open alarm. `
                 + 'Plus and minus zoom, arrow keys pan, 0 resets.'}
               onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}
               onPointerCancel={() => { drag.current = null; }} onKeyDown={onKey}>
            <g transform={`translate(${view.x},${view.y}) scale(${view.k})`}>
              <path d={path({ type: 'Sphere' }) ?? ''} className="world-sphere" />
              <path d={path(geoGraticule10()) ?? ''} className="world-graticule" />
              <path d={landPath} className="world-land" />
              {onMap.map((s) => {
                const xy = projection([s.longitude, s.latitude]);
                if (!xy) return null;
                const t = tone(s);
                const approx = s.location_source !== 'manual';
                return (
                  <g key={s.id} transform={`translate(${xy[0]},${xy[1]}) scale(${inv})`}
                     className={`world-site tone-${t}${selected === s.id ? ' is-selected' : ''}`}
                     role="button" tabIndex={placing ? -1 : 0} aria-label={`${s.code}, ${TONE_LABEL[t]}`}
                     onClick={(e) => { if (!placing) { e.stopPropagation(); setSelected(s.id === selected ? null : s.id); } }}
                     onKeyDown={(e) => {
                       if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); setSelected(s.id === selected ? null : s.id); }
                     }}
                     {...bind(<><b>{s.code}</b> {s.city ?? ''}{s.country ? `, ${s.country}` : ''}
                       {' · '}{TONE_LABEL[t]}{approx ? ' · position approximate' : ''}</>)}>
                    {approx && <circle r={12} className="world-approx" />}
                    <circle r={7} className="world-dot" />
                    <text x={11} y={-10} className="world-label">{s.code}</text>
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
          <p className="muted world-note">
            Natural Earth 1:{view.k >= DETAIL_AT && land50 ? '50' : '110'}m. Scroll or use + and − to zoom, drag to move.
            A dashed ring marks a position taken from the city's centre because the site's own was never set.
          </p>
        </div>

        <aside className="asset-panel world-side">
          <h3>Sites <span className="total">{sites.length}</span></h3>
          <ul className="world-list">
            {sites.map((s) => {
              const t = tone(s);
              return (
                <li key={s.id}>
                  <button type="button" className={selected === s.id ? 'is-current' : undefined}
                          onClick={() => setSelected(s.id === selected ? null : s.id)}>
                    <span className={`world-swatch tone-${t}`} aria-hidden />
                    <span className="code">{s.code}</span>
                    <span className="muted">{s.city ?? 'city not set'}</span>
                    <span className="count">
                      {plural(s.alarms.total, 'alarm')}
                      {s.offline_count > 0 && <span className="world-offline">{s.offline_count} offline</span>}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>

          {chosen && (
            <SiteCard site={chosen} placing={placing === chosen.id}
                      onPlace={() => { setPlacing(chosen.id); setPending(null); setSaveError(null); svgRef.current?.focus(); }} />
          )}

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

function SiteCard({ site, placing, onPlace }: { site: SiteRow; placing: boolean; onPlace: () => void }) {
  const hall = site.rooms.find((r) => r.room_class === 'white_space') ?? site.rooms[0];
  const approx = site.location_source !== 'manual';
  return (
    <div className="world-card">
      <h4>{site.name}</h4>
      <dl>
        <dt>Location</dt>
        <dd>{[site.city, site.country].filter(Boolean).join(', ') || 'not set'}</dd>
        <dt>Position</dt>
        <dd>
          {site.latitude != null && site.longitude != null
            ? `${site.latitude.toFixed(4)}, ${site.longitude.toFixed(4)}`
            : 'not set'}
          {approx && site.latitude != null && <span className="muted"> (city centre)</span>}
        </dd>
        <dt>Devices</dt>
        <dd>{site.online_count} online of {site.device_count}
          {site.offline_count > 0 && <span className="muted"> · {site.offline_count} offline</span>}</dd>
        <dt>Open alarms</dt>
        <dd>{site.alarms.critical} critical · {site.alarms.major} major · {site.alarms.minor} minor</dd>
        <dt>Rooms</dt>
        <dd>{site.rooms.length}</dd>
      </dl>
      <button type="button" className="world-place" onClick={onPlace} disabled={placing}>
        {placing ? 'Click the map…' : approx ? 'Set exact position' : 'Move position'}
      </button>
      <div className="world-links">
        <Link to={`/twin/sites/${site.id}`}>Building</Link>
        {hall && <Link to={`/floorplan?room=${hall.id}`}>Floor map</Link>}
        <Link to={`/thermal?site=${site.id}&scope=rooms`}>Thermal</Link>
        <Link to="/connectivity">Network map</Link>
      </div>
    </div>
  );
}

export default WorldMap;
