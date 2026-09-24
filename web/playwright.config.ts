import { createServer } from 'node:net';
import { defineConfig, devices } from '@playwright/test';

async function freePort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Could not allocate an E2E port');
  await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  return address.port;
}

const requestedPort = process.env.BBX_E2E_PORT;
const port = requestedPort ? Number(requestedPort) : await freePort();
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error('BBX_E2E_PORT must be a valid TCP port');
}
process.env.BBX_E2E_PORT = String(port);
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  use: { ...devices['Desktop Chrome'], baseURL, trace: 'retain-on-failure' },
  webServer: {
    command: `pnpm exec vite --host 127.0.0.1 --port ${port} --strictPort`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
