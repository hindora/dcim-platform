import { useQuery } from '@tanstack/react-query';
import type { GeoProjection } from 'd3-geo';

/**
 * Satellite view: Web-Mercator raster tiles drawn under the sites, and two
 * live weather layers drawn over them.
 *
 * Everything here is fetched by the BROWSER from public services, never by
 * the platform - the plain map stays air-gap safe, and satellite view says so
 * when the services cannot be reached.
 *
 *  - Basemap: NASA Blue Marble (shaded relief + bathymetry), static, public
 *    domain, via the GIBS tile service.
 *  - Clouds: NOAA GOES-East and GOES-West "GeoColor" full-disc imagery, a new
 *    frame every ten minutes, also via GIBS. True colour by day; at night the
 *    product blends infrared cloud with a city-lights layer. Coverage is the
 *    two satellites' discs - the Americas, the Atlantic and the Pacific -
 *    because GIBS does not carry Meteosat or Himawari GeoColor.
 *  - Rain: the RainViewer composite of ground weather radar, one frame every
 *    ten minutes for the past two hours, where a national radar network
 *    reports (North America, Europe, Japan, Australia, parts of Asia and
 *    South America). No radar means no rain drawn, not "no rain".
 *
 * Why this is on a DCIM map at all: a NOC overlays public radar on its site
 * map to see a storm bearing down on a utility feed before the ATS does. The
 * sites' own weather (dry/wet bulb off the cooling-tower controller) is a
 * different thing and is shown as numbers, not pictures.
 */

export const GIBS = 'https://gibs.earthdata.nasa.gov/wmts/epsg3857/best';
export const RAINVIEWER_INDEX = 'https://api.rainviewer.com/public/weather-maps.json';

export interface View { k: number; x: number; y: number }
export interface Tile { z: number; x: number; y: number; px: number; py: number; size: number }

/** The deepest tile level each source publishes; past it, tiles are scaled up. */
export const Z_MAX = { marble: 8, clouds: 7, radar: 7 } as const;
const TILE_PX = 256;

/**
 * Which Mercator tiles cover the viewport, in projection pixels (the SVG's
 * zoomed group applies `view` on top). The level is chosen so a tile paints
 * at roughly 180-360 screen pixels.
 */
export function tilesFor(proj: GeoProjection, view: View, w: number, h: number, maxZ: number): Tile[] {
  const W = proj.scale() * 2 * Math.PI;               // the world square, projection px
  const [tx, ty] = proj.translate();
  const left = tx - W / 2, top = ty - W / 2;
  const z = Math.max(0, Math.min(maxZ, Math.round(Math.log2((W * view.k) / TILE_PX))));
  const n = 2 ** z, size = W / n;
  const x0 = -view.x / view.k, x1 = (w - view.x) / view.k;
  const y0 = -view.y / view.k, y1 = (h - view.y) / view.k;
  const i0 = Math.max(0, Math.floor((x0 - left) / size)), i1 = Math.min(n - 1, Math.floor((x1 - left) / size));
  const j0 = Math.max(0, Math.floor((y0 - top) / size)), j1 = Math.min(n - 1, Math.floor((y1 - top) / size));
  const out: Tile[] = [];
  for (let j = j0; j <= j1; j++) {
    for (let i = i0; i <= i1; i++) out.push({ z, x: i, y: j, px: left + i * size, py: top + j * size, size });
  }
  return out;
}

/** GIBS wants an exact ten-minute timestamp: `2026-10-08T11:10:00Z`. */
export const isoMinute = (t: number) => new Date(t * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');

export const blueMarbleUrl = (t: Tile) =>
  `${GIBS}/BlueMarble_ShadedRelief_Bathymetry/default/default/GoogleMapsCompatible_Level8/${t.z}/${t.y}/${t.x}.jpeg`;
export const geoColorUrl = (sat: 'East' | 'West', time: number, t: Tile) =>
  `${GIBS}/GOES-${sat}_ABI_GeoColor/default/${isoMinute(time)}/GoogleMapsCompatible_Level7/${t.z}/${t.y}/${t.x}.png`;
/** Colour scheme 2 ("universal blue"), smoothed, snow shown. */
export const radarUrl = (host: string, path: string, t: Tile) => `${host}${path}/256/${t.z}/${t.x}/${t.y}/2/1_1.png`;

/* --------------------------------------------------------------- the clock
 * One clock for both layers. Twenty-minute steps over two hours is seven
 * frames: a cloud tile is ~120 KB, so a ten-minute loop of both satellites
 * would be twice the download for motion nobody can see at this scale. The
 * newest frame sits ~35 min behind the wall clock, which is how long the
 * geostationary product takes to reach GIBS; radar is a little fresher and
 * simply has that frame too. */
export const FRAME_STEP_S = 20 * 60;
export const FRAME_COUNT = 7;
export const FRAME_LAG_S = 35 * 60;

export function frameTimes(nowMs = Date.now()): number[] {
  const end = Math.floor((nowMs / 1000 - FRAME_LAG_S) / 600) * 600;
  return Array.from({ length: FRAME_COUNT }, (_, i) => end - (FRAME_COUNT - 1 - i) * FRAME_STEP_S);
}

/* --------------------------------------------------------------- radar index
 * RainViewer publishes which frames exist and where; a tile path is opaque
 * and changes per frame, so the index is needed before any radar is drawn. */
export interface RadarIndex { host: string; byTime: Map<number, string> }

interface RainViewerJson { host: string; radar: { past: { time: number; path: string }[]; nowcast?: { time: number; path: string }[] } }

export function useRadarIndex(enabled: boolean) {
  return useQuery<RadarIndex>({
    queryKey: ['rainviewer-index'],
    queryFn: async () => {
      const r = await fetch(RAINVIEWER_INDEX, { cache: 'no-store' });
      if (!r.ok) throw new Error(`RainViewer ${r.status}`);
      const j = (await r.json()) as RainViewerJson;
      return { host: j.host, byTime: new Map(j.radar.past.map((f) => [f.time, f.path])) };
    },
    enabled,
    staleTime: 5 * 60_000,
    refetchInterval: 10 * 60_000,
    retry: 1,
  });
}

/* ------------------------------------------------------------ reachability
 * One small tile tells whether the browser can see the imagery service at
 * all. An air-gapped NOC gets a sentence, not a blank black map. */
export function useImageryReachable(enabled: boolean) {
  return useQuery<boolean>({
    queryKey: ['gibs-reachable'],
    queryFn: () => loadImage(blueMarbleUrl({ z: 0, x: 0, y: 0, px: 0, py: 0, size: 0 })),
    enabled,
    staleTime: 10 * 60_000,
    retry: false,
  });
}

function loadImage(url: string): Promise<boolean> {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(true);
    img.onerror = () => resolve(false);
    img.src = url;
  });
}

/* ----------------------------------------------------------------- prefetch
 * The frames not on screen are warmed in the browser cache so the loop does
 * not stutter through its first pass. Each URL is fetched once per page. */
const warmed = new Set<string>();
export function prefetch(urls: string[]): void {
  for (const u of urls) {
    if (warmed.has(u)) continue;
    warmed.add(u);
    const img = new Image();
    img.decoding = 'async';
    img.src = u;
  }
}

/** "just now", "46 min ago", "1 h 20 min ago". */
export function ago(seconds: number): string {
  if (seconds < 90) return 'just now';
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60), r = m % 60;
  return r ? `${h} h ${r} min ago` : `${h} h ago`;
}

/** "11:10 UTC" */
export const utcClock = (t: number) => {
  const d = new Date(t * 1000);
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')} UTC`;
};
