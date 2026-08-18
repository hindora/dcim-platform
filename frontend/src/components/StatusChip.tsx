import { statusClass } from '../lib/format';

// Status is never conveyed by colour alone: the chip always carries the text.
export function StatusChip({ status }: { status: string }) {
  return (
    <span className={`chip ${statusClass(status)}`}>
      <span className="dot" aria-hidden="true" />
      {status}
    </span>
  );
}
