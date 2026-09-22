import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type AlarmTicket } from '../../api/client';
import { Tip } from '../../components/HoverTip';
import { oneLine } from '../../lib/format';

/** The ticket for one condition, or the way to open one.
 *
 *  Only ever rendered for the alarm somebody has selected. A chip per row
 *  would be one request per row, and the answer is almost always "no ticket" -
 *  which is the correct default, because the policy is deliberately narrow.
 *
 *  The button is the escape hatch that lets the policy STAY narrow: an
 *  operator who wants a ticket for a MINOR gets one in a click, so nobody has
 *  to widen a policy to cover a judgement call, and widening a policy to cover
 *  one case is how a service desk ends up with four hundred tickets a day.
 */
export function TicketChip({ alarmId }: { alarmId: string }) {
  const qc = useQueryClient();
  const link = useQuery<AlarmTicket>({
    queryKey: ['alarm-ticket', alarmId],
    queryFn: () => api.alarmTicket(alarmId),
    // No integration configured is a 200 with linked:false, so a failure here
    // is a real one and worth one retry rather than a spinner that never ends.
    retry: 1,
  });

  const open = useMutation({
    mutationFn: () => api.ticketAlarm(alarmId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['alarm-ticket', alarmId] }),
  });

  if (link.isLoading) return <span className="muted">…</span>;

  if (link.data?.linked) {
    const closed = link.data.status_category === 'done';
    return (
      <a className={`ticket-chip ${closed ? 'closed' : ''}`}
         href={link.data.url} target="_blank" rel="noreferrer">
        {link.data.issue_key}
        {link.data.status && <span className="s">{link.data.status}</span>}
      </a>
    );
  }

  if (open.isSuccess && open.data?.queued) {
    return (
      <Tip className="muted" tip={oneLine(`The ticket is created by the
              dispatcher, not by this click, so the key appears once Jira has
              answered - usually within a few seconds.`)}>
        queued for {open.data.integration}
      </Tip>
    );
  }

  return (
    <>
      <button onClick={() => open.mutate()} disabled={open.isPending}>
        {open.isPending ? 'Raising…' : 'Create ticket'}
      </button>
      {open.error && (
        <span className="detail warn">
          {String((open.error as Error).message)}
        </span>
      )}
    </>
  );
}
