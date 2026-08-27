import { NavLink, Outlet } from 'react-router-dom';

/** The settings shell: a list of sections on the left, the chosen one beside it.
 *
 *  One entry today. The panel exists anyway because the alternative - a single
 *  page that grows a second tab later - moves the navigation after people have
 *  learned where things are, and a settings area with one item still has to
 *  tell the reader what else lives here. */
export const SETTINGS_NAV = [
  {
    to: 'poll-profiles',
    label: 'Poll profiles',
    blurb: 'How often each endpoint is asked, and for what',
  },
  {
    to: 'collectors',
    label: 'Collectors',
    blurb: 'Which planes each collector runs, and where it listens',
  },
  {
    to: 'appearance',
    label: 'Appearance',
    blurb: 'Light or dark, for this browser',
  },
] as const;

export function SettingsLayout() {
  return (
    <div className="settings">
      <nav className="settings-nav" aria-label="Settings sections">
        <h2>Settings</h2>
        <ul>
          {SETTINGS_NAV.map((item) => (
            <li key={item.to}>
              <NavLink to={item.to}>
                <span className="label">{item.label}</span>
                <span className="blurb">{item.blurb}</span>
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <div className="settings-body">
        <Outlet />
      </div>
    </div>
  );
}
