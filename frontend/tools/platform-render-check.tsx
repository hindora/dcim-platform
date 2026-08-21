import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderToStaticMarkup } from 'react-dom/server';
import { PlatformHealth } from '../src/features/platform/PlatformHealth';
declare const process: { argv: string[]; exit(c: number): never };
declare const console: { log(...a: unknown[]): void };
const [base, user, pass] = process.argv.slice(2);
const text = (h: string) => h.replace(/<[^>]+>/g, ' ').replace(/&[a-z#0-9]+;/g, ' ').replace(/\s+/g, ' ').trim();
async function main() {
  const l = await fetch(`${base}/api/v1/login`, { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: user, password: pass }) });
  const { token } = (await l.json()) as { token: string };
  const r = await fetch(`${base}/api/v1/collector/health`, { headers: { Authorization: `Bearer ${token}` } });
  if (!r.ok) { console.log('FAIL health', r.status); process.exit(1); }
  const data = await r.json();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  qc.setQueryData(['collector-health'], data);
  const html = renderToStaticMarkup(<QueryClientProvider client={qc}><PlatformHealth /></QueryClientProvider>);
  console.log(text(html).slice(0, 1100));
  process.exit(0);
}
main();
