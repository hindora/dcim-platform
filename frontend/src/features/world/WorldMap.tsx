import { useQuery } from '@tanstack/react-query';
import { geoEqualEarth, geoGraticule10, geoPath, type GeoPermissibleObjects } from 'd3-geo';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { feature } from 'topojson-client';
import type { GeometryCollection, Topology } from 'topojson-specification';
import countries110m from 'world-atlas/countries-110m.json';
import { api, type SiteRow, type SitesOverview } from '../../api/client';
import { useHoverTip } from '../../components/HoverTip';
import { Seg } from '../../components/estate';
import './world.css';

/**
 * Every site on the globe, coloured by the worst open alarm it holds.
 *
 * Positions are asset data (migration 0100): set by an administrator, or the
 * centroid of the site's metro when only the city is known - the second kind is
 * drawn with a dashed ring and says "approximate". Nothing is geocoded over the
 * network, so the map draws on an air-gapped install. The basemap is Natural
 * Earth 1:110m, bundled into this route's chunk only.
 */

type Fit = 'sites' | 'world';
type Tone = 'critical' | 'major' | 'warn' | 'ok' | 'unknown';

const LAND = feature(
  countries110m as unknown as Topology,
  (countries110m as unknown as Topology).objects.countries as GeometryCollection,
);

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

function placed(s: SiteRow): s is SiteRow & { latitude: number; longitude: number } {
  return s.latitude != null && s.longitude != null;
}

export function WorldMap() {
  const [fit, setFit] = useState<Fit>('sites');
  const [selected, setSelected] = useState<string | null>(null);
  const { bind, tipEl } = useHoverTip();
  const frame = useRef<HTMLDivElement>(null);
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
  }, []);

  const sites = data?.sites ?? [];
  const onMap = sites.filter(placed);
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
  const chosen = sites.find((s) => s.id === selected) ?? null;

  if (isLoading) return <p className="muted">Loading sites…</p>;
  if (error) return <p className="muted">Sites could not be loaded.</p>;

  return (
    <div className="stack world-page">
      <div className="world-head">
        <h2>World map</h2>
        <Seg label="Map extent" value={fit} onChange={setFit}
             options={[{ key: 'sites', label: 'Sites' }, { key: 'world', label: 'World' }]} />
      </div>

      <div className="world-body">
        <div className="asset-panel world-panel" ref={frame}>
          <svg width={size.w} height={size.h} className="world-svg" role="img"
               aria-label={`World map of ${onMap.length} site${onMap.length === 1 ? '' : 's'}, coloured by worst open alarm`}>
            <path d={path({ type: 'Sphere' }) ?? ''} className="world-sphere" />
            <path d={path(geoGraticule10()) ?? ''} className="world-graticule" />
            <path d={path(LAND) ?? ''} className="world-land" />
            {onMap.map((s) => {
              const xy = projection([s.longitude, s.latitude]);
              if (!xy) return null;
              const t = tone(s);
              const approx = s.location_source !== 'manual';
              return (
                <g key={s.id} transform={`translate(${xy[0]},${xy[1]})`}
                   className={`world-site tone-${t}${selected === s.id ? ' is-selected' : ''}`}
                   role="button" tabIndex={0} aria-label={`${s.code}, ${TONE_LABEL[t]}`}
                   onClick={() => setSelected(s.id === selected ? null : s.id)}
                   onKeyDown={(e) => {
                     if (e.key === 'Enter' || e.key === ' ') setSelected(s.id === selected ? null : s.id);
                   }}
                   {...bind(<><b>{s.code}</b> {s.city ?? ''}{s.country ? `, ${s.country}` : ''}
                     {' · '}{TONE_LABEL[t]}{approx ? ' · position approximate' : ''}</>)}>
                  {approx && <circle r={12} className="world-approx" />}
                  <circle r={7} className="world-dot" />
                  <text x={11} y={-10} className="world-label">{s.code}</text>
                </g>
              );
            })}
          </svg>
          {tipEl}
          <p className="muted world-note">
            Natural Earth 1:110m. A dashed ring marks a position taken from the
            city's centre because the site's own was never set.
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
                    <span className="count">{s.alarms.total}</span>
                  </button>
                </li>
              );
            })}
          </ul>

          {chosen && <SiteCard site={chosen} />}

          {unplaced.length > 0 && (
            <p className="muted">
              Not on the map: {unplaced.map((s) => s.code).join(', ')} - no position
              set and the city is not a known metro.
            </p>
          )}
        </aside>
      </div>
    </div>
  );
}

function SiteCard({ site }: { site: SiteRow }) {
  const hall = site.rooms.find((r) => r.room_class === 'white_space') ?? site.rooms[0];
  return (
    <div className="world-card">
      <h4>{site.name}</h4>
      <dl>
        <dt>Location</dt>
        <dd>{[site.city, site.country].filter(Boolean).join(', ') || 'not set'}</dd>
        <dt>Position</dt>
        <dd>
          {site.latitude != null && site.longitude != null
            ? `${site.latitude.toFixed(2)}, ${site.longitude.toFixed(2)}`
            : 'not set'}
          {site.location_source === 'city centroid' && <span className="muted"> (city centre)</span>}
        </dd>
        <dt>Devices</dt>
        <dd>{site.online_count} online of {site.device_count}
          {site.offline_count > 0 && <span className="muted"> · {site.offline_count} offline</span>}</dd>
        <dt>Open alarms</dt>
        <dd>{site.alarms.critical} critical · {site.alarms.major} major · {site.alarms.minor} minor</dd>
        <dt>Rooms</dt>
        <dd>{site.rooms.length}</dd>
      </dl>
      <div className="world-links">
        {hall && <Link to={`/floorplan?room=${hall.id}`}>Floor map</Link>}
        <Link to={`/thermal?site=${site.id}&scope=rooms`}>Thermal</Link>
        <Link to="/connectivity">Network map</Link>
      </div>
    </div>
  );
}

export default WorldMap;
