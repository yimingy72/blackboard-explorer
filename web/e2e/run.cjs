const { createServer } = require('node:http');
const { spawn } = require('node:child_process');

// Chromium downloads can bypass browser interception; never proxy E2E traffic to a real API.
const downloads = new Map();
const unexpected = [];
const server = createServer(async (request, response) => {
  const url = new URL(request.url, 'http://localhost');
  if (request.method === 'POST' && url.pathname === '/__e2e/download') {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const record = JSON.parse(Buffer.concat(chunks).toString());
    downloads.set(record.uri, record);
    response.writeHead(204).end();
    return;
  }
  if (request.method === 'GET' && url.pathname === '/api/evidence') {
    const record = downloads.get(url.searchParams.get('uri'));
    if (record) {
      response.writeHead(200, {
        'Content-Type': record.contentType,
        'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent(record.filename)}`,
        'X-Content-Type-Options': 'nosniff',
      });
      response.end(Buffer.from(record.body, 'base64'));
      return;
    }
  }
  unexpected.push(`${request.method} ${url.pathname}`);
  response.writeHead(501, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify({ detail: 'Unmocked E2E API request' }));
});

server.listen(0, '127.0.0.1', () => {
  const address = server.address();
  const mockUrl = `http://127.0.0.1:${address.port}`;
  const child = spawn('pnpm', ['exec', 'playwright', 'test', ...process.argv.slice(2)], {
    stdio: 'inherit',
    env: { ...process.env, BBX_API_URL: mockUrl, BBX_E2E_MOCK_API_URL: mockUrl },
  });
  child.on('error', (error) => { console.error(error.message); server.close(() => { process.exitCode = 1; }); });
  child.on('exit', (code) => {
    if (unexpected.length) console.error('Unmocked E2E API requests:', unexpected);
    server.close(() => { process.exitCode = code === 0 && !unexpected.length ? 0 : code ?? 1; });
  });
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
});
