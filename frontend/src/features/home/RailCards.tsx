/**
 * The three navigation cards down the right of the home page.
 *
 * The reference design used rendered 3D estate imagery. There is no render
 * pipeline here, and a stock photo of a datacenter tells an operator nothing,
 * so each card carries a schematic of the thing it links to instead: an inlet
 * map, a one-line power diagram, a capacity bar chart.
 *
 * These are illustrations, not live data, and are deliberately unlabelled with
 * values so nobody reads a number off them.
 */

import { Link } from 'react-router-dom';

function ThermalVisual() {
  // Mostly in-band with a few hot cells: what a healthy hall actually looks
  // like. A rainbow grid would misrepresent the normal case.
  const hot: Record<string, string> = {
    '2,7': 'critical', '1,7': 'major', '2,8': 'major',
    '0,3': 'warn', '3,4': 'warn', '1,2': 'warn',
  };
  const cells = [];
  for (let r = 0; r < 4; r++) {
    for (let c = 0; c < 10; c++) {
      const tone = hot[`${r},${c}`];
      cells.push(
        <rect key={`${r}-${c}`} x={24 + c * 35} y={20 + r * 26} width={30} height={22} rx={2}
              fill={`var(--${tone ?? 'ok'})`} opacity={tone ? 0.9 : 0.4} />,
      );
    }
  }
  return <svg viewBox="0 0 398 132" preserveAspectRatio="xMidYMid meet" aria-hidden>{cells}</svg>;
}

function PowerVisual() {
  const stages = ['GRID', 'ATS', 'UPS', 'PDU', 'RACK'];
  return (
    <svg viewBox="0 0 398 132" preserveAspectRatio="xMidYMid meet" aria-hidden>
      <text x={22} y={26} fontSize={9} fill="var(--ok)">N+1 · both feeds live</text>
      {stages.map((s, i) => (
        <g key={s}>
          <rect x={22 + i * 72} y={48} width={52} height={34} rx={3}
                fill="var(--bg-raised)" stroke={i < 3 ? 'var(--ok)' : 'var(--accent)'}
                strokeOpacity={0.7} />
          {i < stages.length - 1 && (
            <line x1={74 + i * 72} y1={65} x2={94 + i * 72} y2={65}
                  stroke="var(--ok)" strokeOpacity={0.55} strokeWidth={2} />
          )}
          <text x={48 + i * 72} y={98} fontSize={8} textAnchor="middle"
                fill="var(--text-faint)" letterSpacing="1">{s}</text>
        </g>
      ))}
    </svg>
  );
}

function UtilisationVisual() {
  const vals = [0.42, 0.68, 0.81, 0.55, 0.34, 0.72, 0.91, 0.48, 0.61, 0.27];
  return (
    <svg viewBox="0 0 398 132" preserveAspectRatio="xMidYMid meet" aria-hidden>
      {vals.map((v, i) => {
        const h = Math.round(96 * v);
        const tone = v > 0.85 ? 'critical' : v > 0.65 ? 'warn' : 'accent';
        return (
          <g key={i}>
            <rect x={26 + i * 35} y={22} width={24} height={96} rx={2} fill="var(--bg-raised)" />
            <rect x={26 + i * 35} y={22 + (96 - h)} width={24} height={h} rx={2}
                  fill={`var(--${tone})`} opacity={0.85} />
          </g>
        );
      })}
    </svg>
  );
}

const CARDS = [
  {
    to: '/analytics?view=thermal', title: 'Thermal Data', Visual: ThermalVisual,
    body: 'Inlet, exhaust and ΔT across every rack, with ASHRAE band compliance and hot-spot ranking.',
  },
  {
    to: '/analytics?view=power', title: 'Power Data', Visual: PowerVisual,
    body: 'Draw and redundancy from utility through switchgear, UPS and PDU down to the outlet.',
  },
  {
    to: '/analytics?view=capacity', title: 'Utilisation Data', Visual: UtilisationVisual,
    body: 'Space, power, cooling and port headroom per hall — where the next rack can actually land.',
  },
];

export function RailCards() {
  return (
    <>
      {CARDS.map(({ to, title, body, Visual }) => (
        <Link key={title} className="rail-card" to={to}>
          <div className="visual"><Visual /></div>
          <div className="body">
            <h3>{title}</h3>
            <p>{body}</p>
            <span className="go" aria-hidden>→</span>
          </div>
        </Link>
      ))}
    </>
  );
}
