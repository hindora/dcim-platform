import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { apply, getMode, watchSystem } from './lib/theme';
import './index.css';
import './home.css';
import './estate.css';
// The chart furniture - .asset-panel, .asset-vcols, .asset-max, the pager -
// is the house set, reused by every chart in the product, so it is loaded
// globally rather than by whichever page happens to be the one it grew up on.
//
// It used to be imported only by AssetWorkspace, which meant the alarm trend
// on the HOME page rendered unstyled for anybody who had not visited Assets
// first in that session.
import './features/assets/assets.css';

// Before anything renders: a light-mode machine should not be shown the dark
// palette for a frame on its way in, and a wall display left on "sync with
// system" has to keep following it without anybody reloading the page.
apply(getMode());
watchSystem(() => {});

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // REST is the source of truth on mount. Phase 2 adds a WebSocket that
      // applies deltas on top; it will never be the source of truth after a
      // reconnect gap.
      staleTime: 5_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

const root = document.getElementById('root');
if (!root) throw new Error('#root is missing from index.html');

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
