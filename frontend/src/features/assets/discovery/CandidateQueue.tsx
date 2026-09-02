import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api,
  type AssetSummary,
  type DiscoveryCandidate,
} from '../../../api/client';
import { humanise, relativeTime } from '../../../lib/format';

/** The discovery queue.
 *
 *  The subsystem behind this has existed since migration 0012 - runs,
 *  candidates, an identity blob, suggested type and vendor, promote and ignore.
 *  What was missing was a screen (docs/19 B2). This is it.
 *
 *  Read-only in phase 1. Promote needs placement and a name, which discovery
 *  cannot know, and that form is phase 2 work alongside the identity fix.
 */
export function CandidateQueue() {
  const { data, isLoading, error } = useQuery<{ items: DiscoveryCandidate[] }>({
    queryKey: ['discovery-candidates'],
    queryFn: () => api.discoveryCandidates({ limit: '500' }),
    refetchInterval: 60_000,
  });

  // Serial matching is the primary key discovery is supposed to use. It cannot
  // be used while no asset carries one, and a screen that quietly falls back to
  // a weaker key than the operator assumes is worse than one that admits it.
  const { data: summary } = useQuery<AssetSummary>({
    queryKey: ['asset-summary'],
    queryFn: api.assetSummary,
  });
  const serialMatchingBlind = summary && summary.identity.with_serial === 0;

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;

  const items = data?.items ?? [];
  const unmatched = items.filter((c) => !c.matched_device_id);
  const matched = items.filter((c) => c.matched_device_id);

  return (
    <>
      <h2>Discovery</h2>
      <p className="subtitle">
        What answered on the network, and whether inventory already claims it.
      </p>

      {serialMatchingBlind && (
        <div className="banner">
          Serial-number matching is unavailable: no asset carries a serial.
          Candidates are matched by management IP only, so a device that has
          been re-addressed will appear as new.
        </div>
      )}

      {isLoading && <p className="muted">Loading…</p>}

      {!isLoading && items.length === 0 && (
        <div className="asset-empty">
          No discovery candidates. Queue a sweep to look for responders the
          inventory does not know about.
        </div>
      )}

      {unmatched.length > 0 && (
        <>
          <h3>Unmatched — {unmatched.length}</h3>
          <p className="muted">Nothing in inventory claims these responders.</p>
          <CandidateTable rows={unmatched} />
        </>
      )}

      {matched.length > 0 && (
        <>
          <h3 style={{ marginTop: 24 }}>Already known — {matched.length}</h3>
          {/* Shown with a denominator on purpose: "the sweep saw 900 and 894
              were expected" is more useful than a list of six surprises with
              nothing to compare them against. */}
          <p className="muted">
            {matched.length} of {items.length} responders were expected.
          </p>
          <CandidateTable rows={matched} />
        </>
      )}
    </>
  );
}

function CandidateTable({ rows }: { rows: DiscoveryCandidate[] }) {
  return (
    <div className="asset-scroll">
      <table>
        <thead>
          <tr>
            <th>Address</th><th>Protocol</th><th>Suggested</th>
            <th>Identity</th><th>Matched</th><th>Last seen</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.id}>
              <td className="asset-tag">{c.address ?? '—'}</td>
              <td className="muted">{c.protocol.toUpperCase()}</td>
              <td className="muted">
                {[c.suggested_vendor,
                  c.suggested_device_type ? humanise(c.suggested_device_type) : null,
                  c.suggested_model].filter(Boolean).join(' · ') || '—'}
              </td>
              <td className="muted" style={{ maxWidth: 320 }}>
                {String(c.identity?.sysDescr ?? c.identity?.sysName ?? '—')
                  .slice(0, 90)}
              </td>
              <td>
                {c.matched_device_id ? (
                  <Link to={`/assets/inventory/${c.matched_device_id}`}>
                    {c.matched_device_name ?? 'known'}
                  </Link>
                ) : (
                  <span className="asset-none">new</span>
                )}
              </td>
              <td className="muted">{relativeTime(c.last_seen)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
