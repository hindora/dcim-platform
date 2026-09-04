import { humanise } from '../../../lib/format';

/** Lifecycle state as a chip.
 *
 *  Module-local rather than a new prop on the shared StatusChip: StatusChip is
 *  mounted by Home, the Devices pages and the rack elevation, and those are out
 *  of scope (docs/22 §1). A fork costs a small duplication and buys the
 *  guarantee that none of them can change.
 *
 *  Colour is never the only encoding - the chip always carries its label, so it
 *  reads for a colour-blind operator and in a printed runbook.
 */
export function LifecycleChip({ state }: { state?: string | null }) {
  const value = state ?? 'in_service';
  // No tooltip: the chip already carries its label as text, and a balloon
  // repeating it verbatim is noise.
  return (
    <span className={`asset-life is-${value}`}>
      {humanise(value)}
    </span>
  );
}
