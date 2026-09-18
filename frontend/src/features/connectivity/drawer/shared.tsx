import { useEffect } from 'react';
import { Link } from 'react-router-dom';

/** What every drawer tab is handed by the shell.
 *
 *  `canvasIds` is the set of devices drawn as themselves on the canvas right
 *  now. A name in a list is a button that selects it there when it is, and a
 *  link to its device page when it is not - a click that silently does
 *  nothing because the device is rolled into a rack box, or two hops out of
 *  scope, is worse than a click that leaves the page.
 */
export interface DrawerCtx {
  canvasIds: Set<string>;
  onReveal: (id: string) => void;
  /** The footer's Export button exports whatever the open tab is showing.
   *  A tab registers its exporter here once its data is in, and withdraws it
   *  when it unmounts, so the button is never live over an empty tab. */
  setExport: (fn: (() => void) | null) => void;
}

export function DeviceRef({ id, name, ctx }: {
  id: string;
  name: string;
  ctx: DrawerCtx;
}) {
  return ctx.canvasIds.has(id) ? (
    <button type="button" className="cd-ref" title="Select on the canvas"
            onClick={() => ctx.onReveal(id)}>
      {name}
    </button>
  ) : (
    <Link to={`/devices/${id}`} title="Not drawn here — open the device">{name}</Link>
  );
}

/** Register the tab's exporter while `fn` is non-null. */
export function useExport(ctx: DrawerCtx, fn: (() => void) | null) {
  const { setExport } = ctx;
  useEffect(() => {
    setExport(fn);
    return () => setExport(null);
  }, [setExport, fn]);
}

/** Severity to the shared chip tone, worst first when sorting. */
export const SEVERITY_RANK: Record<string, number> = {
  CRITICAL: 0, MAJOR: 1, MINOR: 2, WARNING: 3, CLEAR: 9,
};

export function Loading({ h = 60 }: { h?: number }) {
  return <div className="asset-skeleton" style={{ height: h }} />;
}
