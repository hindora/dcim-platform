/**
 * Site KPI drawer.
 *
 * Opens over the home page when an operator clicks KPIs on a row. Everything
 * here comes from one call to `/sites/{id}/kpi`.
 *
 * The rule this panel exists to keep: a metric the platform cannot compute
 * renders as an em dash WITH THE REASON, never as a plausible number. Guessing
 * is how an invented figure ends up in a sustainability report.
 *
 * Its twin, for the metrics that DO compute: every tile shows how it was
 * arrived at. PUE and CER fall out of the load split; WUE is integrated off
 * the cooling-tower makeup meters; CUE is PUE times a published grid emission
 * factor, because carbon intensity is not something a site can measure;
 * outdoor humidity is DERIVED from the dry/wet bulb pair, not read off a
 * hygrometer. A reader must be able to tell those four apart at a glance,
 * which is why `method` rides beside the value and never gets dropped.
 */

import { useEffect } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  api,
  type AlarmCategory,
  type MaybeMetric,
  type SiteKpi,
  type SiteRow,
  type Utilisation,
} from '../../api/client';
import { CategoryGlyph, type GlyphKind } from '../../components/CategoryGlyph';
import { Tip } from '../../components/HoverTip';
import { ALL_CATEGORIES, AlarmTrend, maxOpen } from './AlarmTrend';
import { relativeTime } from '../../lib/format';

function Tile({ value, unit, caption, note, absent, bar }: {
  value: string; unit?: string; caption: string; note?: string | null;
  absent?: boolean; bar?: string;
}) {
  return (
    <div className={`kpi-tile ${absent ? 'absent' : ''}`}>
      {bar && <span className="bar" style={{ background: `var(--${bar})` }} />}
      <div>
        <div className="v">
          <span className="n">{value}</span>
          {unit && !absent && <span className="u">{unit}</span>}
        </div>
        <div className="cap">{caption}</div>
        {note && <div className="note">{note}</div>}
      </div>
    </div>
  );
}

function metricTile(m: MaybeMetric, caption: string,
                    { digits = 2, unit }: { digits?: number; unit?: string } = {}) {
  const absent = m.value === null || m.value === undefined;
  // Method and reason are DIFFERENT claims and the tile needs both. Showing
  // only the note loses how the figure was arrived at, which for a derived
  // ratio like CUE is the whole of its credibility; showing only the method
  // loses why a missing one is missing.
  const method = m.method
    ? (m.category != null ? `${m.method} method · category ${m.category}` : m.method)
    : null;
  const note = [method, m.note].filter(Boolean).join(' · ') || null;
  return (
    <Tile absent={absent} caption={caption} unit={unit}
          value={absent ? '—' : m.value!.toFixed(digits)}
          note={note} />
  );
}

function utilTile(u: Utilisation, caption: string) {
  const absent = u.pct === null || u.pct === undefined;
  // Colour follows headroom, not aesthetics: past 85% a constraint is close
  // enough to binding that it should be reading as a warning.
  const bar = absent ? 'unknown' : u.pct! >= 85 ? 'critical' : u.pct! >= 70 ? 'warn' : 'ok';
  // Basis AND note, not one or the other. The basis is the denominator; the
  // note is how that denominator was arrived at and what is wrong with it -
  // a plant with two chillers whose nameplate is unknown reads HIGH, and a
  // capacity percentage nobody can explain is one nobody should plan against.
  const note = [absent ? null : u.basis, u.note].filter(Boolean).join(' · ') || null;
  return (
    <Tile absent={absent} bar={bar} caption={caption}
          value={absent ? '—' : String(u.pct)} unit="%"
          note={note} />
  );
}

/** The drawer summarises by OWNER, not by category.
 *
 *  Eight chips would not fit the panel, and the question this drawer answers -
 *  "who do I call about this site" - is the grouping's question anyway. The
 *  eight-column breakdown is one click away on the row behind it. */
const CHIPS: { key: string; categories: AlarmCategory[] | null;
               glyph: GlyphKind; label: string; tone: string }[] = [
  { key: 'power', categories: ['power'], glyph: 'power', label: 'PWR', tone: 'pwr' },
  { key: 'cooling_env', categories: ['cooling', 'environmental'],
    glyph: 'cooling', label: 'CLG', tone: 'cool' },
  // Every open alarm at this site, all categories.
  { key: 'total', categories: null, glyph: 'alarms', label: 'ALM', tone: 'alarms' },
  { key: 'it_network', categories: ['it_equipment', 'network'],
    glyph: 'it_equipment', label: 'IT', tone: 'it' },
  { key: 'visibility', categories: ['visibility'], glyph: 'visibility',
    label: 'VIS', tone: 'vis' },
  { key: 'capacity', categories: ['capacity'], glyph: 'capacity',
    label: 'CAP', tone: 'cap' },
];

export function SiteDrawer({ site, onClose }: { site: SiteRow; onClose: () => void }) {
  // Escape closes. A panel that covers the page and can only be dismissed with
  // the mouse is a panel that traps a keyboard user.
  useEffect(() => {
    // Not while a maximized chart is up: that Escape is the modal's.
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !maxOpen()) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const { data, error, isLoading } = useQuery<SiteKpi>({
    queryKey: ['site-kpi', site.id],
    queryFn: () => api.siteKpi(site.id),
    refetchInterval: 30_000,
  });

  return (
    <>
      <button className="drawer-scrim" onClick={onClose} aria-label="Close site KPIs" />
      <aside className="drawer" role="dialog" aria-modal="true"
             aria-label={`Site KPIs for ${site.code}`}>
        <button className="close" onClick={onClose} aria-label="Close">✕</button>

        <div className="drawer-id">
          <svg className="glyph" width="24" height="24" viewBox="0 0 24 24" aria-hidden
               fill="none" stroke="currentColor" strokeWidth="1.4">
            <rect x="2" y="4" width="9" height="18" /><rect x="13" y="9" width="9" height="13" />
          </svg>
          <div>
            <div className="cap">SITE</div>
            <div className="val">{site.code}</div>
            {site.name !== site.code && <div className="sub">{site.name}</div>}
          </div>
        </div>

        <div className="drawer-id">
          <svg className="glyph" width="24" height="24" viewBox="0 0 24 24" aria-hidden
               fill="none" stroke="currentColor" strokeWidth="1.4">
            <circle cx="12" cy="10" r="6" /><line x1="12" y1="16" x2="12" y2="22" />
          </svg>
          <div>
            <div className="cap">LOCATION</div>
            <div className="val">
              {site.city || site.country
                ? [site.city, site.country].filter(Boolean).join(', ')
                : <span className="unset">not set</span>}
            </div>
            <div className="sub">{site.timezone}</div>
          </div>
        </div>

        <div className="drawer-id">
          <svg className="glyph" width="24" height="24" viewBox="0 0 24 24" aria-hidden
               fill="none" stroke="currentColor" strokeWidth="1.4">
            <rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" />
            <rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" />
          </svg>
          <div>
            <div className="cap">ITEMS MONITORED</div>
            <div className="val">{data ? `${data.monitored.devices} devices` : '—'}</div>
            {data && (
              <div className="sub">
                {data.monitored.endpoints} endpoints across {data.monitored.protocols} protocols
                {' · '}{data.monitored.racks} racks
              </div>
            )}
          </div>
        </div>

        {/* A <button> with no handler, which is what this was, looks exactly
            like a working one and silently does nothing. Same destination as
            the site row's ENTER. */}
        <Link className="primary enter" to={`/devices?datacenter=${site.code}`}
              style={{ display: 'flex', alignItems: 'center', justifyContent: 'center',
                       textDecoration: 'none' }}>
          ENTER
        </Link>

        <div className="drawer-head">
          <h3>Live Data</h3>
          <span className="as-of">
            {data ? `as of ${new Date(data.as_of).toLocaleTimeString()} · ${relativeTime(data.as_of)}` : ''}
          </span>
        </div>

        {error && <div className="banner" style={{ margin: '0 26px 16px' }}>
          Failed to load KPIs: {String(error)}
        </div>}
        {isLoading && <p className="muted" style={{ padding: '0 26px 20px' }}>Loading…</p>}

        {data && (
          <>
            <section className="drawer-section">
              <div className="title">SITE KPI</div>
              <div className="drawer-grid">
                {metricTile(data.efficiency.pue, 'Site PUE (Power)')}
                {metricTile(data.efficiency.cer, 'Site CER (Cooling)')}
                {metricTile(data.efficiency.wue, 'Site WUE (Water)',
                            { unit: 'L/kWh' })}
                {metricTile(data.efficiency.cue, 'Site CUE (Carbon)',
                            { unit: 'kgCO₂e/kWh' })}
              </div>
            </section>

            <section className="drawer-section">
              <div className="title">SITE POWER</div>
              <div className="drawer-grid">
                <Tile value={data.power.total_kw.toFixed(1)} unit="kW" caption="Site Total" />
                <Tile value={data.power.it_load_kw.toFixed(1)} unit="kW" caption="IT Load"
                      note={`${data.power.reporting_devices} devices reporting`} />
                <Tile value={data.power.cooling_kw.toFixed(1)} unit="kW" caption="Cooling" />
                <Tile value={data.power.facility_other_kw.toFixed(1)} unit="kW"
                      caption="Facility Other" />
              </div>
            </section>

            <section className="drawer-section">
              <div className="title">SITE UTILISATION</div>
              <div className="drawer-grid three">
                {utilTile(data.utilisation.power, 'Site Power')}
                {utilTile(data.utilisation.space, 'Rack Space (U)')}
                {utilTile(data.utilisation.cooling, 'Cooling')}
              </div>
            </section>

            <section className="drawer-section">
              <div className="title">
                OUTDOOR AIR
                {!data.weather.available && <span className="why">{data.weather.note}</span>}
                {data.weather.available && data.weather.age_s != null
                  && data.weather.age_s > 1800 && (
                  <span className="why">
                    last read {Math.round(data.weather.age_s / 60)} min ago
                  </span>
                )}
              </div>
              <div className="drawer-grid three">
                <Tile absent={data.weather.dry_bulb_c === null} caption="Dry Bulb"
                      unit="°C"
                      value={data.weather.dry_bulb_c?.toFixed(1) ?? '—'}
                      note={data.weather.available ? data.weather.source : null} />
                {/* Wet bulb, not "max temp": tower approach is set by wet bulb, so
                    it is the number that predicts whether the plant can hold its
                    condenser setpoint. */}
                <Tile absent={data.weather.wet_bulb_c === null} caption="Wet Bulb"
                      unit="°C"
                      value={data.weather.wet_bulb_c?.toFixed(1) ?? '—'}
                      note={data.weather.wet_bulb_c === null
                        ? null : 'sets cooling-tower approach'} />
                {/* Derived, and it says so on the tile. The pair of
                    thermometers beside it IS a psychrometer, so the moisture
                    is genuinely carried in the reading - but a derivation
                    printed like an instrument reading is how an assumption
                    ends up quoted as a measurement. */}
                <Tile absent={data.weather.humidity_pct === null}
                      caption="Humidity" unit="%"
                      value={data.weather.humidity_pct?.toFixed(0) ?? '—'}
                      note={data.weather.humidity_note} />
              </div>
            </section>

            <div className="drawer-chips">
              {CHIPS.map((c) => {
                const n = c.categories === null
                  ? data.alarms.total
                  : c.categories.reduce(
                      (t, k) => t + (data.alarms.by_category?.[k] ?? 0), 0);
                return (
                  <Tip key={c.key} className={`ind ${c.tone} ${n > 0 ? 'on' : ''}`}
                       tip={c.categories === null
                         ? <><b>{n}</b> open alarm{n === 1 ? '' : 's'} at this site</>
                         : <><b>{c.label}</b>: {n}</>}>
                    <span>{n}</span>
                    <CategoryGlyph kind={c.glyph} />
                    <span>{c.label}</span>
                  </Tip>
                );
              })}
            </div>

            {/* Whether now is normal for this site: what it has raised over
                time, every domain at once, across all its rooms. The tiles
                above are the present; this is the run-up to it. */}
            <section className="drawer-section">
              <div className="title">ALARMS RAISED</div>
              <AlarmTrend categories={ALL_CATEGORIES}
                          scope={{ kind: 'site', id: site.id, label: site.code }} />
            </section>

            <div className="drawer-conn">
              <span style={{ color: data.monitored.devices_offline === 0
                ? 'var(--ok)' : 'var(--critical)' }}>
                <CategoryGlyph kind="visibility" />
              </span>
              <span style={{ color: data.monitored.devices_offline === 0
                ? 'var(--ok)' : 'var(--critical)' }}>
                {data.monitored.devices_offline === 0
                  ? `Connected · all ${data.monitored.devices_online} devices reporting`
                  : `${data.monitored.devices_offline} of ${data.monitored.devices} devices are not reporting`}
              </span>
            </div>
          </>
        )}
      </aside>
    </>
  );
}
