import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api, type Path, type TopologyNode } from '../../api/client';

/** Are these two independent of each other?
 *
 *  Asked when somebody is deciding whether two racks can take the same
 *  maintenance window, and it is not the same question as "is there a path
 *  between them". On a power layer two loads are leaves: any connection runs
 *  up from one and back down to the other, so a walk exists between almost any
 *  pair in a building and says almost nothing. What decides it is WHAT THEY
 *  BOTH HANG OFF, which is why the verdict here is the shared upstream and the
 *  walk is the supporting detail underneath it.
 *
 *  Sits on the redundancy tab rather than in a tab of its own: it is the same
 *  question as the audit above it, asked about a pair instead of about
 *  everything.
 */
export function Independence({ nodes, layer }: {
  nodes: TopologyNode[];
  layer: string;
}) {
  const [src, setSrc] = useState('');
  const [dst, setDst] = useState('');
  const [open, setOpen] = useState(false);

  // Rolled-up boxes are synthetic ids the endpoint would refuse, so the
  // pickers offer real devices only - which means that with grouping on, the
  // servers are not in the list. Said out loud below rather than left as a
  // list that mysteriously has no servers in it.
  const options = nodes
    .filter((n) => n.rolled_up === 0)
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name));
  const hidden = nodes.reduce((sum, n) => sum + n.rolled_up, 0);

  const q = useQuery<Path>({
    queryKey: ['path', src, dst, layer],
    queryFn: () => api.path(src, dst, layer),
    enabled: Boolean(src && dst && src !== dst),
    retry: false,
  });

  const pick = (value: string, onChange: (v: string) => void, label: string) => (
    <select value={value} aria-label={label}
            onChange={(e) => onChange(e.target.value)}>
      <option value="">{label}</option>
      {options.map((n) => (
        <option key={n.id} value={n.id}>
          {n.name} · {n.device_type.replace(/_/g, ' ')}
        </option>
      ))}
    </select>
  );

  return (
    // Folded away until it is asked. The audit below is what the tab is for
    // and the sheet is only so tall; an open pair-picker was costing the
    // findings table a third of the rows it could show, to answer a question
    // nobody had asked yet.
    <details className="conn-indep" open={open}
             onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary>Are two devices independent?</summary>
      <div className="conn-indep-pick">
        {pick(src, setSrc, 'First device')}
        <span className="muted">and</span>
        {pick(dst, setDst, 'Second device')}
      </div>

      {hidden > 0 && (
        <p className="k">
          {hidden} devices are grouped into racks and cannot be compared one to
          one. Switch the grouping above to “Every device separately” to pick
          them.
        </p>
      )}

      {src && dst && src === dst && (
        <p className="muted">Pick two different devices.</p>
      )}

      {q.isLoading && <p className="muted">Working out what they share…</p>}
      {q.isError && (
        <p className="muted">
          One of those is not on the {layer} layer, so they cannot be compared
          on it.
        </p>
      )}

      {q.data && (
        <>
          <p className={q.data.independent ? 'conn-verdict-ok' : 'conn-verdict-no'}>
            {q.data.independent
              ? <>Independent on the {layer} layer — nothing feeds both.</>
              : <>
                  <strong>Not independent.</strong> Both hang off{' '}
                  {q.data.shared_upstream.map((s, i) => (
                    <span key={s.id}>
                      {i > 0 && ', '}
                      <Link to={`/devices/${s.id}`}>{s.name}</Link>
                    </span>
                  ))}
                  {' '}— one window can take them both.
                </>}
          </p>

          {/* The walk is detail, not the verdict. It is printed because it
              shows HOW they are related once the answer is no, and because a
              disconnected pair is worth saying out loud rather than leaving as
              an empty space. */}
          {q.data.connected ? (
            <p className="k">
              Connected through {q.data.hops.length - 2 < 0 ? 0
                : q.data.hops.length - 2} device
              {q.data.hops.length - 2 === 1 ? '' : 's'}:{' '}
              {q.data.hops.map((h) => h.name).join(' → ')}
            </p>
          ) : (
            <p className="k">
              No connection between them on this layer at all.
            </p>
          )}
        </>
      )}
    </details>
  );
}
