import type { Impact, TopologyNode } from '../../api/client';

/** The impact answer, projected onto the boxes the canvas is drawing.
 *
 *  Two mismatches have to be bridged, and both are the kind that produce a
 *  confident wrong picture rather than an error.
 *
 *  The layer vocabulary. The impact endpoint answers for every layer at once
 *  and names them with the enum's words; the canvas shows one layer and the
 *  page calls the data plane 'network'. Reading the wrong entry would paint a
 *  correct answer about the wrong graph.
 *
 *  The identity. Impact is keyed by DEVICE id. Under a roll-up a box is a
 *  synthetic rack id that matches nothing in the answer, so it is classified
 *  through its membership - and a rack whose members are split is reported as
 *  split, because "8 of 18 go dark" is the true answer and both "dark" and
 *  "fine" are lies about it.
 */

/** API layer name -> the name the impact response uses. */
const ENUM_LAYER: Record<string, string> = {
  network: 'production',
  production: 'production',
  management: 'management',
  power: 'power',
  cooling: 'cooling',
  fieldbus: 'fieldbus',
};

export type Verdict = 'cut' | 'degraded' | 'partial' | null;

export interface ImpactView {
  /** The device whose removal is being simulated. */
  candidate: string;
  /** Plain words for this layer: "loses power", "loses monitoring". */
  effect: string;
  cutOff: Set<string>;
  degraded: Set<string>;
  /** Totals for THIS layer, which is what the diagram is showing. A device
   *  that also loses monitoring is a different sentence on a different tab. */
  cutCount: number;
  degradedCount: number;
  /** True when the device has no dependents on the layer being drawn. The
   *  overlay then has nothing to paint, and saying so is the answer. */
  empty: boolean;
}

export function projectImpact(impact: Impact, layer: string): ImpactView {
  const want = ENUM_LAYER[layer] ?? layer;
  const found = impact.layers.find((l) => l.layer === want);
  return {
    candidate: impact.device.id,
    effect: found?.effect ?? 'is removed',
    cutOff: new Set(found?.cut_off.map((n) => n.id) ?? []),
    degraded: new Set(found?.degraded.map((n) => n.id) ?? []),
    cutCount: found?.cut_off.length ?? 0,
    degradedCount: found?.degraded.length ?? 0,
    empty: !found,
  };
}

/** How one box on the canvas is affected.
 *
 *  `partial` exists only for a rolled-up rack. A device is cut off or it is
 *  not; a rack of eighteen servers with eight on the failing side is neither,
 *  and rounding it either way is how a change gets approved against a number
 *  nobody checked.
 */
export function verdictFor(node: TopologyNode, view: ImpactView): Verdict {
  if (node.rolled_up > 0) {
    const cut = node.member_ids.filter((id) => view.cutOff.has(id)).length;
    if (cut === node.member_ids.length && cut > 0) return 'cut';
    if (cut > 0) return 'partial';
    return node.member_ids.some((id) => view.degraded.has(id)) ? 'degraded' : null;
  }
  if (view.cutOff.has(node.id)) return 'cut';
  if (view.degraded.has(node.id)) return 'degraded';
  return null;
}

/** How many real devices behind this box go dark. Used for the "8 of 18"
 *  label, so a partial rack says which part. */
export function cutWithin(node: TopologyNode, view: ImpactView): number {
  if (node.rolled_up > 0) {
    return node.member_ids.filter((id) => view.cutOff.has(id)).length;
  }
  return view.cutOff.has(node.id) ? 1 : 0;
}
