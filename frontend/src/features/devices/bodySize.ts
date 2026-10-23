import { humanise } from '../../lib/format';

/** How a device is installed and how big it is, in the terms that fit it.
 *
 *  Rack units describe rack-mounted bodies only. A chiller, an RPP or a CRAH
 *  stands on the floor and is measured in metres; a vertical rack PDU takes no
 *  U at all; a probe hangs on a door. The platform's u_height column cannot be
 *  empty and reads 1 for all of those, so it is only trusted for `rack`. */
export interface BodyLike {
  u_height: number;
  mount?: string | null;
  footprint_w_m?: number | null;
  footprint_d_m?: number | null;
  height_m?: number | null;
  footprint_basis?: string | null;
  location?: { u_start?: number | null };
}

const PLACES: Record<string, string> = {
  rack_front: 'on the rack front', rack_rear: 'on the rack rear',
  wall: 'wall-mounted', pipe: 'on a pipe', ceiling: 'ceiling-mounted',
  underfloor: 'under the raised floor', duct: 'in a duct', panel: 'in a panel',
};

const m = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(2).replace(/0$/, ''));

export function bodySize(d: BodyLike): { short: string | null; long: string | null; label: string } {
  const mount = d.mount ?? (d.location?.u_start ? 'rack' : null);
  if (mount === 'rack') return { short: `${d.u_height}U`, long: `${d.u_height}U`, label: 'Height' };
  if (mount === 'zero_u') return { short: '0U', long: '0U, mounted vertically in the rack', label: 'Height' };
  if (mount === 'floor') {
    const dims = d.footprint_w_m != null && d.footprint_d_m != null && d.height_m != null
      ? `${m(d.footprint_w_m)} × ${m(d.footprint_d_m)} × ${m(d.height_m)} m` : null;
    return {
      label: 'Size',
      short: 'floor-standing',
      long: dims ? `${dims} (W × D × H), floor-standing${d.footprint_basis === 'class' ? ', estimated for its class' : ''}`
        : 'floor-standing',
    };
  }
  if (mount) {
    const where = PLACES[mount] ?? humanise(mount).toLowerCase();
    return { short: null, long: `${where}, no rack units`, label: 'Mounting' };
  }
  return { short: null, long: null, label: 'Height' };
}
