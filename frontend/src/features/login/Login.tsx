/** The sign-in page.
 *
 *  A full-bleed photograph of the thing this product watches - a row of racks
 *  in the dark, status lights on - with the estate's name laid over it and a
 *  frosted panel for the form. The panel keeps the product's tokens, so it is
 *  dark frost on the dark theme and light frost on the light one, while the
 *  photograph stays as it is: a datacenter floor is dark whichever theme the
 *  operator picked.
 *
 *  Photograph: Taylor Vick on Unsplash (Unsplash licence, no attribution
 *  required). Kept in the repo rather than hot-linked, so the login page does
 *  not depend on a third party being up.
 */

import { useId, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, setToken, type Instance } from '../../api/client';
import { Notice } from '../../components/Notice';
import { useOrg } from '../../lib/useOrg';
import racks from '../../assets/login/racks.jpg';
import './login.css';

type Glyph = 'user' | 'lock' | 'eye' | 'eye-off' | 'chevron';

/** Same 16-unit grid and line weight as ActionGlyph, so the field icons and
 *  the button's chevron are visibly the product's family. */
function LoginGlyph({ kind }: { kind: Glyph }) {
  const common = {
    width: 18, height: 18, viewBox: '0 0 16 16', fill: 'none',
    stroke: 'currentColor', strokeWidth: 1.4,
    strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
    'aria-hidden': true, focusable: false as const,
  };
  switch (kind) {
    case 'user':
      return (
        <svg {...common}>
          <circle cx="8" cy="5.4" r="2.8" />
          <path d="M2.8 14c.6-2.9 2.6-4.3 5.2-4.3s4.6 1.4 5.2 4.3" />
        </svg>
      );
    case 'lock':
      return (
        <svg {...common}>
          <rect x="3.2" y="7" width="9.6" height="7" rx="1.4" />
          <path d="M5.2 7V5a2.8 2.8 0 0 1 5.6 0v2" />
        </svg>
      );
    case 'eye':
      return (
        <svg {...common}>
          <path d="M1.6 8s2.4-4.4 6.4-4.4S14.4 8 14.4 8s-2.4 4.4-6.4 4.4S1.6 8 1.6 8z" />
          <circle cx="8" cy="8" r="2.1" />
        </svg>
      );
    case 'eye-off':
      return (
        <svg {...common}>
          <path d="M2.4 2.4l11.2 11.2" />
          <path d="M6.3 4.1A6.9 6.9 0 0 1 8 3.6c4 0 6.4 4.4 6.4 4.4a11 11 0 0 1-2.1 2.6" />
          <path d="M4 5.4A11.4 11.4 0 0 0 1.6 8s2.4 4.4 6.4 4.4c1 0 1.9-.3 2.7-.7" />
          <path d="M6.5 6.5a2.1 2.1 0 0 0 3 3" />
        </svg>
      );
    case 'chevron':
      return (
        <svg {...common} width={14} height={14}>
          <path d="M6 3.5L10.5 8 6 12.5" />
        </svg>
      );
  }
}

export function Login({ onDone, returnTo }: { onDone: () => void; returnTo?: string }) {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ids = { user: useId(), pass: useId() };

  const org = useOrg();
  // Shares the cache useOrg fills; only the environment is read from it here.
  const { data: instance } = useQuery<Instance>({
    queryKey: ['instance'], queryFn: api.instance, staleTime: Infinity, retry: false,
  });
  const environment = (instance?.environment || '').trim();

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api.login(username, password);
      setToken(res.token);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'login failed');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-page">
      {/* React 18 passes the lowercase spelling through; the camel-cased one
          it does not know and warns about. */}
      <img className="login-photo" src={racks} alt="" width={1600} height={900}
           decoding="async" {...{ fetchpriority: 'high' }} />
      <div className="login-scrim" aria-hidden="true" />

      <section className="login-lead">
        <h1>{org}</h1>
        <p>
          Power, cooling and connectivity for every site, watched in one place.
        </p>
        {environment && environment.toLowerCase() !== 'production' && (
          <p className="login-env">This is the {environment} instance.</p>
        )}
      </section>

      <form className="login-panel" onSubmit={submit} noValidate>
        <h2>Sign in</h2>
        <p className="login-sub">
          {returnTo ? 'Your session expired. Sign in to continue where you were.'
                    : 'Use the account your administrator gave you.'}
        </p>

        <div className="login-field">
          <label htmlFor={ids.user}>Username</label>
          <input id={ids.user} name="username" autoComplete="username"
                 autoCapitalize="none" spellCheck={false} required
                 value={username} onChange={(e) => setUsername(e.target.value)} />
          <span className="login-field-glyph"><LoginGlyph kind="user" /></span>
        </div>

        <div className="login-field">
          <label htmlFor={ids.pass}>Password</label>
          <input id={ids.pass} name="password" type={showPassword ? 'text' : 'password'}
                 autoComplete="current-password" required
                 value={password} onChange={(e) => setPassword(e.target.value)} />
          <button type="button" className="login-field-toggle"
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  aria-pressed={showPassword}
                  onClick={() => setShowPassword((v) => !v)}>
            <LoginGlyph kind={showPassword ? 'eye-off' : 'eye'} />
          </button>
        </div>

        <div className="login-actions">
          <button type="submit" className="login-submit" disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
            {!busy && <LoginGlyph kind="chevron" />}
          </button>
          <span className="login-aside">No account? Ask your administrator.</span>
        </div>

        {error && <Notice tone="error" className="login-error">{error}</Notice>}
      </form>
    </div>
  );
}
