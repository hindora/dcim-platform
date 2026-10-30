import { Link } from 'react-router-dom';
import type { RackPowerBlock } from '../../api/client';
import { StatusChip } from '../../components/StatusChip';
import { Fact } from './RackThermal';

function kw(v: number | null | undefined, digits = 2): string {
  return v == null ? '—' : `${v.toFixed(digits)} kW`;
}

function pctTone(pct: number | null | undefined, warn: number, crit: number): 'warn' | 'critical' | undefined {
  if (pct == null) return undefined;
  if (pct >= crit) return 'critical';
  if (pct >= warn) return 'warn';
  return undefined;
}

/** The feeds, as cards: one per strip, side first. A strip that is not
 *  answering is a feed nobody can see, and says so before its load does. */
export function Feeds({ p, compact = false }: { p: RackPowerBlock; compact?: boolean }) {
  const th = p.thresholds;
  if (!p.strips.length) {
    return <p className="dv-empty">No rack strip is recorded in this rack: its loads are fed from outside it, or the cabling is not on record.</p>;
  }
  return (
    <div className="rk-feeds">
      {p.strips.map((s) => {
        const pct = s.kw != null && s.rated_kw ? (100 * s.kw) / s.rated_kw : null;
        const tone = pctTone(pct, th.warn_pct, th.critical_pct);
        const legs = p.legs.find((l) => l.strip_id === s.id);
        return (
          <div key={s.id} className={`rk-feed${s.status !== 'ONLINE' ? ' is-dark' : ''}`}>
            <div className="rk-feed-head">
              <span className="rk-side">{s.side ?? '?'}</span>
              <Link to={`/devices/${s.id}`}>{s.name}</Link>
              <StatusChip status={s.status} />
            </div>
            <div className="rk-feed-load">
              <b className={tone}>{s.kw == null ? '—' : s.kw.toFixed(2)} kW</b>
              <span className="muted"> of {s.rated_kw ? `${s.rated_kw.toFixed(1)} kW` : 'an unrated strip'}
                {pct != null && <> · {Math.round(pct)} %</>}</span>
            </div>
            <div className="dv-meter">
              <div className="dv-meter-track">
                <div className={`dv-meter-fill${tone ? ` ${tone}` : ''}`}
                     style={{ width: `${Math.min(100, pct ?? 0)}%` }} />
              </div>
            </div>
            {!compact && legs && legs.legs.length > 0 && (
              <div className="rk-legs">
                {legs.legs.map((a, i) => (
                  <span key={i}><span className="k">L{i + 1}</span> {a.toFixed(1)} A</span>
                ))}
                {legs.imbalance_pct != null && (
                  <span className={legs.imbalance_pct >= th.leg_imbalance_warn_pct ? 'warn' : 'muted'}>
                    {legs.imbalance_pct.toFixed(1)} % imbalance
                  </span>
                )}
                {s.v_ln != null && <span className="muted">{Math.round(s.v_ln)} V L-N</span>}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** What the survivor carries when a side is lost: one sentence and a figure. */
export function Failover({ p }: { p: RackPowerBlock }) {
  const f = p.failover;
  if (!f) {
    return <p className="dv-empty">Not fed from two sides, or a side is not reporting its draw: there is no survivor to judge.</p>;
  }
  const cls = f.band === 'critical' ? 'critical' : f.band === 'warn' ? 'warn' : 'ok';
  return (
    <div className="rk-failover">
      <div className={`rk-failover-pct ${cls}`}>{f.pct.toFixed(0)} %</div>
      <div>
        <p className="rk-failover-words">
          Lose side <b>{f.lost_side}</b> and <b>{f.survivor}</b> carries {f.total_kw.toFixed(2)} kW
          {f.pct >= 100 ? ', past what it can hold' : f.band === 'warn' ? ', near its limit' : ' with room to spare'}.
        </p>
        <p className="dv-note">Judged at the {f.basis}: {f.detail}.{f.mirrored ? ' Every load is on both sides.' : ' Not every load is on both sides; one on the lost side alone goes dark instead.'}</p>
      </div>
    </div>
  );
}

export function RackPowerTab({ p }: { p: RackPowerBlock }) {
  const th = p.thresholds;
  const feeds = p.feeds;
  return (
    <>
      <div className="dv-grid">
        <section className="dv-card">
          <h3>Feeds <span className="lead">each strip, by side</span></h3>
          <Feeds p={p} />
          {feeds && (
            <div className="dv-facts" style={{ marginTop: 14 }}>
              <Fact k="Loads on both sides" v={String(feeds.dual_cord)} />
              <Fact k="On one cord" v={String(feeds.single_cord)} tone={feeds.single_cord ? 'warn' : undefined} />
              <Fact k="Fed by nothing on record" v={String(feeds.unfed)} tone={feeds.unfed ? 'warn' : undefined} />
              <Fact k="Feeds meet upstream at"
                    v={feeds.shared_upstream?.length ? feeds.shared_upstream.join(', ') : 'nothing shared'}
                    tone={feeds.shared_upstream?.length ? 'warn' : undefined} />
            </div>
          )}
          {p.psu_failed.length > 0 && (
            <p className="dv-note warn">Supplies reported failed: {p.psu_failed.join(', ')}.</p>
          )}
        </section>
        <section className="dv-card">
          <h3>If a side is lost</h3>
          <Failover p={p} />
        </section>
      </div>

      <section>
        <h3>Draw, rating and budget</h3>
        <div className="dv-facts">
          <Fact k="IT draw now" v={kw(p.it_ac_kw)} />
          <Fact k="Rated" v={p.rated_kw ? `${kw(p.rated_kw, 1)} · ${p.load_pct?.toFixed(0) ?? '—'} %` : 'no rating'}
                tone={pctTone(p.load_pct, th.warn_pct, th.critical_pct)} />
          <Fact k={`p95 over ${p.p95_days} days`} v={kw(p.p95_kw)} />
          <Fact k="Headroom" v={kw(p.headroom_kw)} tone={(p.headroom_kw ?? 0) < 0 ? 'critical' : undefined} />
          <Fact k="Allocated" v={kw(p.allocated_kw)} />
          <Fact k="Reserved" v={kw(p.reserved_kw)} />
          <Fact k="Stranded" v={kw(p.stranded_kw)} />
          <Fact k="Loads reporting" v={`${p.reporting ?? 0} of ${p.loads}${p.measured_at_strip ? ` · ${p.measured_at_strip} read at the strip` : ''}`} />
        </div>
        <p className="dv-note">
          Headroom is the rating minus the week's 95th-percentile draw, the planner's margin. Stranded is
          what was budgeted for this rack and is not being drawn: where the next server fits without a new feed.
        </p>
      </section>

      <div className="dv-grid">
        <section className="dv-card">
          <h3>Branch breakers <span className="lead">busiest first</span></h3>
          {p.banks.length === 0
            ? <p className="dv-empty">No strip here reports a bank against a breaker.</p>
            : (
              <table className="rk-table">
                <thead><tr><th>Strip</th><th>Bank</th><th className="num">Amps</th><th className="num">Breaker</th><th className="num">Load</th></tr></thead>
                <tbody>
                  {p.banks.map((b) => (
                    <tr key={`${b.strip}-${b.bank}`}>
                      <td>{b.strip}</td><td>{b.bank}</td>
                      <td className="num">{b.amps.toFixed(1)}</td>
                      <td className="num">{b.breaker_a.toFixed(0)} A</td>
                      <td className={`num ${pctTone(b.pct, th.bank_warn_pct, 100) ?? ''}`}>{b.pct.toFixed(0)} %</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
        </section>
        <section className="dv-card">
          <h3>Receptacles <span className="lead">where the strip meters each one</span></h3>
          {!p.outlets
            ? <p className="dv-empty">No strip in this rack meters its receptacles; loads are read at their own supplies.</p>
            : (
              <>
                <div className="dv-facts">
                  <Fact k="Strips metering outlets" v={`${p.outlets.strips_metered} of ${p.outlets.strips}`} />
                  <Fact k="Busiest receptacle" v={p.outlets.max_a != null ? `${p.outlets.max_a.toFixed(2)} A` : '—'} />
                  <Fact k={`Past ${th.outlet_warn_pct} %`} v={String(p.outlets.hot)} tone={p.outlets.hot ? 'warn' : undefined} />
                  <Fact k="Idle" v={String(p.outlets.idle)} />
                </div>
                {p.outlets.rows.length > 0 && (
                  <table className="rk-table" style={{ marginTop: 12 }}>
                    <thead><tr><th>Strip</th><th>Outlet</th><th>Load</th><th className="num">Amps</th><th className="num">Of rating</th></tr></thead>
                    <tbody>
                      {p.outlets.rows.slice(0, 8).map((r) => (
                        <tr key={`${r.strip}-${r.outlet}`}>
                          <td>{r.strip}</td><td>{r.outlet}</td><td>{r.load ?? <span className="dv-none">idle</span>}</td>
                          <td className="num">{r.amps.toFixed(2)}</td>
                          <td className={`num ${pctTone(r.pct, th.outlet_warn_pct, 100) ?? ''}`}>{r.pct != null ? `${r.pct.toFixed(0)} %` : '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </>
            )}
        </section>
      </div>
    </>
  );
}
