import type { Impact, ImpactNode, Trace } from '../../api/client';

/**
 * A path or impact answer shaped for the room viewer: which devices are
 * involved and in what role, and which hops to draw as cable runs. The
 * answers come from /topology/trace and /topology/impact; nothing is
 * computed here, only re-keyed by device id.
 */

export type PathKind = 'power' | 'cooling' | 'impact';
export type Role = 'anchor' | 'path' | 'cut_off' | 'degraded';

export interface PathHop { from: string; to: string; side: string | null; state: string }
export interface PathChain {
  side: string | null; verdict: string;
  /** Source first. */
  nodes: ImpactNode[];
}
export interface PathOverlay {
  kind: PathKind;
  anchors: string[];
  roles: Map<string, Role>;
  hops: PathHop[];
  chains: PathChain[];
  cutOff: ImpactNode[];
  degraded: ImpactNode[];
  notes: string[];
}

export function fromTraces(kind: 'power' | 'cooling', traces: Trace[]): PathOverlay {
  const roles = new Map<string, Role>();
  const hops: PathHop[] = [];
  const chains: PathChain[] = [];
  const notes: string[] = [];
  const seen = new Set<string>();
  for (const t of traces) {
    roles.set(t.device.id, 'anchor');
    if (t.is_source) notes.push(`${t.device.name} is a source on this layer: nothing feeds it.`);
    if (t.truncated) notes.push('The walk hit its bound; the chain may be longer than shown.');
    if (t.asymmetric) notes.push('The A and B sides are different lengths.');
    for (const p of t.paths) {
      const nodes: ImpactNode[] = [];
      for (const h of p.hops) {
        const key = `${h.up.id}>${h.down.id}`;
        if (!seen.has(key)) {
          seen.add(key);
          hops.push({ from: h.up.id, to: h.down.id, side: h.redundancy_side ?? p.side ?? null, state: h.oper_state });
        }
        if (!roles.has(h.up.id)) roles.set(h.up.id, 'path');
        if (!roles.has(h.down.id)) roles.set(h.down.id, 'path');
      }
      if (p.hops.length) {
        nodes.push(p.hops[0].up);
        for (const h of p.hops) nodes.push(h.down);
      }
      chains.push({ side: p.side ?? null, verdict: p.verdict, nodes });
    }
  }
  return { kind, anchors: traces.map((t) => t.device.id), roles, hops, chains, cutOff: [], degraded: [], notes };
}

export function fromImpact(i: Impact): PathOverlay {
  const roles = new Map<string, Role>([[i.device.id, 'anchor']]);
  const cutOff: ImpactNode[] = [], degraded: ImpactNode[] = [];
  const notes: string[] = [];
  for (const l of i.layers) {
    for (const n of l.cut_off) { if (!roles.has(n.id)) { roles.set(n.id, 'cut_off'); cutOff.push(n); } }
    for (const n of l.degraded) { if (!roles.has(n.id)) { roles.set(n.id, 'degraded'); degraded.push(n); } }
    if (l.dependents) notes.push(`${l.layer}: ${l.effect}`);
  }
  return { kind: 'impact', anchors: [i.device.id], roles, hops: [], chains: [], cutOff, degraded, notes };
}
