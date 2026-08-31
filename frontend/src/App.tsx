import { Fragment, useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate }
  from 'react-router-dom';
import { api, getToken, onAuthLost, setToken } from './api/client';
import { useOrg } from './lib/useOrg';
import { AlarmList } from './features/alarms/AlarmList';
import { Analytics } from './features/analytics/Analytics';
import { PlatformHealth } from './features/platform/PlatformHealth';
import { Appearance } from './features/settings/Appearance';
import { Collectors } from './features/settings/Collectors';
import { PollProfiles } from './features/settings/PollProfiles';
import { SettingsLayout } from './features/settings/SettingsLayout';
import { TrustBanner } from './components/TrustBanner';
import { UserMenu } from './components/UserMenu';
import { Home } from './features/home/Home';
import { Thermal } from './features/estate/Thermal';
import { Power } from './features/estate/Power';
import { Utilization } from './features/estate/Utilization';
import { DeviceDetail } from './features/devices/DeviceDetail';
import { RackElevationView } from './features/racks/RackElevation';
import { RackList } from './features/racks/RackList';
import { FloorPlanView } from './features/floorplan/FloorPlan';
import { TopologyView } from './features/topology/TopologyView';
import { DeviceList } from './features/devices/DeviceList';
import { useSocketStatus } from './ws/useSocket';

function Login({ onDone, returnTo }: { onDone: () => void; returnTo?: string }) {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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

  const org = useOrg();

  return (
    <form className="login" onSubmit={submit}>
      <h1>{org}</h1>
      <p className="muted" style={{ margin: 0, fontSize: 13 }}>
        {returnTo ? 'Session expired — sign in to continue where you were'
                  : 'Sign in to continue'}
      </p>
      <label htmlFor="u">Username</label>
      <input id="u" value={username} onChange={(e) => setUsername(e.target.value)} />
      <label htmlFor="p">Password</label>
      <input id="p" type="password" value={password}
             onChange={(e) => setPassword(e.target.value)} />
      <button className="primary" disabled={busy}>
        {busy ? 'Signing in…' : 'Sign in'}
      </button>
      {error && <div className="error">{error}</div>}
    </form>
  );
}

/**
 * Primary navigation.
 *
 * By operator task, not by device family. "Assets" is one page filtered, not
 * five nav entries for servers, switches, routers, firewalls and load
 * balancers - those are a query string, and putting them in the nav is how a
 * sidebar reaches sixty items nobody reads.
 */
const NAV = [
  { to: '/', label: 'HOME', end: true },
  { to: '/thermal', label: 'THERMAL' },
  { to: '/power', label: 'POWER' },
  { to: '/utilization', label: 'UTILIZATION' },
  { to: '/connectivity', label: 'CONNECTIVITY' },
  { to: '/assets', label: 'ASSETS' },
];

function TopBar({ onSignOut }: { onSignOut: () => void }) {
  return (
    <header className="topbar">
      <div className="brand">
        <span className="mark" aria-hidden />
        {/* The product, not the customer. Top-left is where a user looks to
            know WHICH TOOL they are in; the estate's name is the page's
            headline, and printing it in both places says neither. */}
        <span className="wordmark">DCIM</span>
      </div>

      <nav className="topnav" aria-label="Primary">
        {NAV.map((item, i) => (
          <Fragment key={item.to}>
            <NavLink to={item.to} end={item.end}>{item.label}</NavLink>
            {i < NAV.length - 1 && <span className="sep" aria-hidden>/</span>}
          </Fragment>
        ))}
      </nav>

      <div className="utilities">
        <NavLink to="/platform" style={{ padding: 0, border: 'none' }}>SYSTEM STATUS</NavLink>
        {/* Settings and the manual live behind the name rather than along the
            top row: they belong to the person signed in, not to the estate,
            and the row above is a set of views of the estate. */}
        <UserMenu username="admin" onSignOut={onSignOut} />
      </div>
    </header>
  );
}

function Footer() {
  const qc = useQueryClient();
  const status = useSocketStatus();
  const live = status === 'open';
  return (
    <footer className="home-footer">
      <span>{new Date().toLocaleString(undefined, {
        weekday: 'short', day: '2-digit', month: 'short', year: 'numeric',
        hour: '2-digit', minute: '2-digit',
      })}</span>
      <button onClick={() => qc.invalidateQueries()}>
        <svg width="17" height="17" viewBox="0 0 17 17" fill="none"
             stroke="currentColor" strokeWidth="1.6" aria-hidden>
          <path d="M15 8.5a6.5 6.5 0 1 1-1.9-4.6" strokeLinecap="round" />
          <path d="M13.4 1v3.2h-3.2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        Refresh
      </button>
      <span className="spacer" />
      <span className="live" style={{ color: live ? 'var(--ok)' : 'var(--warn)' }}>
        <span className="dot" style={{ background: 'currentColor' }} />
        {live ? 'Live feed connected' : `Live feed ${status}`}
      </span>
    </footer>
  );
}

/** Interior pages keep the padding `main` used to carry; Home is full-bleed. */
function Page({ children }: { children: React.ReactNode }) {
  return <div className="page">{children}</div>;
}

export default function App() {
  const [authed, setAuthed] = useState(Boolean(getToken()));
  const [returnTo, setReturnTo] = useState<string | null>(null);
  const location = useLocation();
  const navigate = useNavigate();

  // A 401 anywhere ends the session for everything, so the shell has to hear
  // about it. Without this the token was cleared and nothing else happened:
  // the app kept rendering over a cache it could no longer refresh, and each
  // page invented its own wording for the one cause - "Failed to load sites:
  // session expired", "Could not load collectors", eighteen in all, none of
  // them actionable, because the only fix was to sign in again.
  //
  // Where the user was is remembered and restored. Being thrown to the home
  // page is a second, smaller loss on top of the first.
  useEffect(() => onAuthLost(() => {
    setReturnTo(location.pathname + location.search);
    setAuthed(false);
  }), [location.pathname, location.search]);

  if (!authed) {
    return (
      <Login
        returnTo={returnTo ?? undefined}
        onDone={() => {
          setAuthed(true);
          if (returnTo) navigate(returnTo, { replace: true });
          setReturnTo(null);
        }}
      />
    );
  }

  return (
    <div className="shell">
      <TrustBanner />
      <TopBar onSignOut={() => { setToken(null); setAuthed(false); }} />
      <main>
        <Routes>
          <Route path="/" element={<Home />} />

          {/* The estate pages: every site and room at once, one question each.
              The older analytics section stays reachable from each of their
              sub-links, because it answers the follow-up - this page tells you
              WHICH room is hot, that one tells you why. */}
          <Route path="/thermal" element={<Page><Thermal /></Page>} />
          <Route path="/power" element={<Page><Power /></Page>} />
          <Route path="/utilization" element={<Page><Utilization /></Page>} />
          <Route path="/connectivity" element={<Page><PlatformHealth /></Page>} />
          <Route path="/assets" element={<Page><DeviceList /></Page>} />

          {/* Reached from rows, drill-downs and links rather than the nav. */}
          <Route path="/devices" element={<Page><DeviceList /></Page>} />
          <Route path="/devices/:id" element={<Page><DeviceDetail /></Page>} />
          <Route path="/racks" element={<Page><RackList /></Page>} />
          <Route path="/racks/:id" element={<Page><RackElevationView /></Page>} />
          <Route path="/floorplan" element={<Page><FloorPlanView /></Page>} />
          <Route path="/topology" element={<Page><TopologyView /></Page>} />
          <Route path="/alarms" element={<Page><AlarmList /></Page>} />
          <Route path="/analytics" element={<Page><Analytics /></Page>} />
          <Route path="/platform" element={<Page><PlatformHealth /></Page>} />
          {/* One shell, one section today. A nested route rather than a flat
              one so the left-hand list stays put as sections are added. */}
          <Route path="/settings" element={<Page><SettingsLayout /></Page>}>
            <Route index element={<Navigate to="poll-profiles" replace />} />
            <Route path="poll-profiles" element={<PollProfiles />} />
            <Route path="collectors" element={<Collectors />} />
            <Route path="appearance" element={<Appearance />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <Footer />
    </div>
  );
}
