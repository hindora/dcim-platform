/** Whose estate this instance watches.
 *
 *  The home headline, the tab title and the login card all name the operator's
 *  organisation rather than the product. A DCIM shows somebody THEIR
 *  datacentres; the product name is identical on every install, and an
 *  operator signed into two instances needs to know which estate they are
 *  about to acknowledge an alarm on.
 *
 *  Served unauthenticated, because the login card needs it before there is a
 *  token, and cached for the session: it changes when the backend is
 *  redeployed, never between two renders.
 */

import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type Instance } from '../api/client';

export const PRODUCT_NAME = 'DCIM Platform';

export function useOrg(): string {
  const { data } = useQuery<Instance>({
    queryKey: ['instance'],
    queryFn: api.instance,
    staleTime: Infinity,
    retry: false,
  });
  return data?.org_name || PRODUCT_NAME;
}

/** Put the estate's name in the tab, so two open instances are tellable apart
 *  from the tab strip rather than by clicking into one. */
export function useOrgTitle(suffix?: string) {
  const org = useOrg();
  useEffect(() => {
    document.title = suffix ? `${suffix} · ${org}` : org;
  }, [org, suffix]);
}
