import { useState } from 'react';
import { NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { api, getToken, setToken } from './api/client';
import { Dashboard } from './features/dashboard/Dashboard';
import { DeviceDetail } from './features/devices/DeviceDetail';
import { DeviceList } from './features/devices/DeviceList';

function Login({ onDone }: { onDone: () => void }) {
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

  return (
    <form className="login" onSubmit={submit}>
      <h1>DCIM Platform</h1>
      <p className="muted" style={{ margin: 0, fontSize: 13 }}>Sign in to continue</p>
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

function Sidebar({ onSignOut }: { onSignOut: () => void }) {
  return (
    <aside className="sidebar">
      <h1>DCIM Platform</h1>
      <nav>
        <NavLink to="/" end>Dashboard</NavLink>
        <div className="section">Infrastructure</div>
        <NavLink to="/devices">Devices</NavLink>
      </nav>
      <div className="section">Session</div>
      <nav>
        {/* Phase 2 adds alarms and the live WebSocket feed; phase 4 adds racks,
            topology and the floor plan. */}
        <a href="#" onClick={(e) => { e.preventDefault(); onSignOut(); }}>Sign out</a>
      </nav>
    </aside>
  );
}

export default function App() {
  const [authed, setAuthed] = useState(Boolean(getToken()));

  if (!authed) return <Login onDone={() => setAuthed(true)} />;

  return (
    <div className="app">
      <Sidebar onSignOut={() => { setToken(null); setAuthed(false); }} />
      <main>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/devices" element={<DeviceList />} />
          <Route path="/devices/:id" element={<DeviceDetail />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
