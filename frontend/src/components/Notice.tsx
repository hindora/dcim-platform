/**
 * A message inside a page: a load that failed, an action that was refused, a
 * caveat on the figures above, a one-time instruction.
 *
 * It replaces the `.banner` class, which had two tones - a red edge and an
 * amber one - for four different jobs. A failed save, a pre-flight blocker, a
 * "this token is shown once" and "this pool is collecting" all reached for one
 * of the two, so red meant everything from "the request failed" to "here is a
 * rule you should know", and success was faked with an inline border colour.
 * And the only thing that told them apart was the colour of a 3 px edge.
 *
 * The tones, and what each one is for:
 *
 *   error   something failed or is broken: a load, a save, a device going
 *           dark. Announced to assistive tech the moment it appears.
 *   warn    nothing failed, but going on has a cost or is blocked: a freeze,
 *           a blocker, a caveat that changes how a figure reads.
 *   info    something worth knowing before acting: a token shown once, a
 *           gateway in the path, a state that will change by itself.
 *   ok      the thing asked for is done and working.
 *
 * Every tone carries its own glyph shape and a spoken label, so the meaning
 * survives without colour. This is for messages INSIDE a page; whether the
 * platform's own numbers can be trusted is components/TrustBanner, and an
 * error about one form field belongs beside that field, not here.
 */

import type { HTMLAttributes, ReactNode } from 'react';

export type NoticeTone = 'error' | 'warn' | 'info' | 'ok';

const SPOKEN: Record<NoticeTone, string> = {
  error: 'Problem',
  warn: 'Warning',
  info: 'Note',
  ok: 'Done',
};

export function NoticeIcon({ tone }: { tone: NoticeTone }) {
  const common = {
    className: 'notice-icon', width: 16, height: 16, viewBox: '0 0 16 16', fill: 'none',
    stroke: 'currentColor', strokeWidth: 1.6, strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const, 'aria-hidden': true, focusable: false,
  };
  switch (tone) {
    case 'error':
      return (
        <svg {...common}>
          <path d="M5.2 1.8h5.6l3.4 3.4v5.6l-3.4 3.4H5.2l-3.4-3.4V5.2z" />
          <path d="M8 4.8v3.8M8 11.2v.01" />
        </svg>
      );
    case 'warn':
      return (
        <svg {...common}>
          <path d="M8 1.8L14.6 13.6H1.4z" />
          <path d="M8 6.2v3.4M8 11.6v.01" />
        </svg>
      );
    case 'ok':
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="6.2" />
          <path d="M5.2 8.2l1.9 1.9 3.7-3.9" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="6.2" />
          <path d="M8 7.2v4M8 4.8v.01" />
        </svg>
      );
  }
}

interface NoticeProps extends Omit<HTMLAttributes<HTMLDivElement>, 'title'> {
  tone?: NoticeTone;
  /** A short lead in bold, when the message has one. */
  heading?: ReactNode;
  /** A control that resolves the message: Retry, Approve, Copy. */
  action?: ReactNode;
}

export function Notice({
  tone = 'error', heading, action, className, children, role, ...rest
}: NoticeProps) {
  // An error is announced as it appears; the others are read in page order.
  // A caller that knows better (a status that updates in place) passes role.
  const announce = role ?? (tone === 'error' ? 'alert' : undefined);
  return (
    <div className={`notice notice-${tone}${className ? ` ${className}` : ''}`} role={announce} {...rest}>
      <NoticeIcon tone={tone} />
      <div className="notice-body">
        <span className="sr-only">{SPOKEN[tone]}: </span>
        {heading && <strong className="notice-heading">{heading}</strong>}
        {children}
      </div>
      {action && <div className="notice-action">{action}</div>}
    </div>
  );
}
