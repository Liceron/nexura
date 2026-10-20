import { spawn, ChildProcess } from 'node:child_process';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export interface TestCluster {
  backendPort: number;
  frontendPort: number;
  backendUrl: string;
  frontendUrl: string;
  /** The backend's per-session token, read the way Studio reads it (see `readSessionToken`). */
  sessionToken: string;
  /** Headers for direct API calls (`page.request.post(url, { headers: cluster.apiHeaders })`). */
  apiHeaders: Record<string, string>;
  stop: () => Promise<void>;
}

/** Header the local server requires on every state-changing request. */
export const SESSION_HEADER = 'X-Nexura-Session';

/**
 * Stop a server started with `detached: true` and everything it spawned. `npx vite` and
 * `uv run python` start child processes; signalling only the direct child left those running
 * (one orphaned Vite dev server per test exhausted memory in full runs).
 */
export function killProcessTree(proc: ChildProcess, signal: NodeJS.Signals = 'SIGTERM'): void {
  if (proc.pid === undefined) return;
  try {
    process.kill(-proc.pid, signal);
  } catch {
    try {
      proc.kill(signal);
    } catch {
      // already gone
    }
  }
}

/** SIGTERM the whole group, then SIGKILL whatever is still running after `graceMs`. */
export async function stopProcessTree(proc: ChildProcess, graceMs = 1000): Promise<void> {
  killProcessTree(proc, 'SIGTERM');
  await new Promise<void>((res) => {
    if (proc.exitCode !== null || proc.signalCode !== null) return res();
    proc.once('exit', () => res());
    setTimeout(res, graceMs);
  });
  killProcessTree(proc, 'SIGKILL');
}

/**
 * Read the backend's per-session token exactly as Studio does: `GET /api/v1/local-session` on
 * the Studio origin (the Vite dev server proxies `/api` to the backend). The server only hands
 * the token to its own origin and to local non-browser clients.
 */
export async function readSessionToken(studioOrigin: string): Promise<string> {
  const res = await fetch(`${studioOrigin}/api/v1/local-session`);
  if (!res.ok) {
    throw new Error(`Could not read the session token from ${studioOrigin}: HTTP ${res.status}`);
  }
  const body = (await res.json()) as { token?: string };
  if (!body.token) throw new Error('The session-token response has no token');
  return body.token;
}

/**
 * Send the session token on every request of the page's context, including `page.request`
 * calls made directly to the backend by a spec.
 */
export async function authorizeApiRequests(
  page: { context: () => { setExtraHTTPHeaders: (h: Record<string, string>) => Promise<void> } },
  cluster: TestCluster,
): Promise<void> {
  await page.context().setExtraHTTPHeaders(cluster.apiHeaders);
}

// Startup polls every 100 ms. Cold Vite starts on shared CI runners take well over the old
// 8 s, which failed whole spec files with "Frontend failed to start".
const STARTUP_POLLS = 450;

let portCounter = Math.floor(Math.random() * 200);

export async function startTestCluster(): Promise<TestCluster> {
  portCounter += 1;
  const backendPort = 14000 + (portCounter * 4);
  const frontendPort = 15000 + (portCounter * 4);
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'nexura-e2e-'));
  const dbPath = path.join(tmpDir, 'e2e.sqlite3');
  const projectRoot = path.resolve(__dirname, '../../../');

  let backendStderr = '';
  // 1. Start Python backend
  const pyCode = `import uvicorn; from nexura_runtime.service.api.app import create_api_app; app = create_api_app(db_path='${dbPath}', profile='local-owner'); uvicorn.run(app, host='127.0.0.1', port=${backendPort}, log_level='error')`;
  const backendProc: ChildProcess = spawn(
    'uv',
    ['run', '--frozen', 'python', '-c', pyCode],
    {
      cwd: projectRoot,
      stdio: ['pipe', 'pipe', 'pipe'],
      detached: true,
    }
  );
  backendProc.stderr?.on('data', (d) => {
    backendStderr += d.toString();
  });

  // Wait for backend to be ready
  const backendUrl = `http://127.0.0.1:${backendPort}`;
  let backendReady = false;
  for (let i = 0; i < STARTUP_POLLS; i++) {
    if (backendProc.exitCode !== null) {
      throw new Error(`Backend process exited early with code ${backendProc.exitCode}: ${backendStderr}`);
    }
    try {
      const res = await fetch(`${backendUrl}/readyz`);
      if (res.ok) {
        backendReady = true;
        break;
      }
    } catch {
      // wait
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  if (!backendReady) {
    killProcessTree(backendProc);
    throw new Error(`Backend failed to start on port ${backendPort}: ${backendStderr}`);
  }

  // 2. Start Vite server for frontend
  const frontendUrl = `http://127.0.0.1:${frontendPort}`;
  const studioWebDir = path.resolve(__dirname, '../../');
  const frontendProc: ChildProcess = spawn(
    'npx',
    ['vite', '--port', String(frontendPort), '--strictPort'],
    {
      cwd: studioWebDir,
      env: {
        ...process.env,
        VITE_BACKEND_URL: backendUrl,
      },
      stdio: 'pipe',
      detached: true,
    }
  );

  // Wait for frontend to be ready
  let frontendReady = false;
  for (let i = 0; i < STARTUP_POLLS; i++) {
    if (frontendProc.exitCode !== null) {
      throw new Error(`Frontend process exited early with code ${frontendProc.exitCode}`);
    }
    try {
      const res = await fetch(frontendUrl);
      if (res.ok) {
        frontendReady = true;
        break;
      }
    } catch {
      // wait
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  if (!frontendReady) {
    killProcessTree(frontendProc);
    killProcessTree(backendProc);
    throw new Error(`Frontend failed to start on port ${frontendPort}`);
  }

  // 3. Read the per-session token through the Studio origin, as Studio itself does
  let sessionToken: string;
  try {
    sessionToken = await readSessionToken(frontendUrl);
  } catch (err) {
    killProcessTree(frontendProc);
    killProcessTree(backendProc);
    throw err;
  }

  return {
    backendPort,
    frontendPort,
    backendUrl,
    frontendUrl,
    sessionToken,
    apiHeaders: { [SESSION_HEADER]: sessionToken },
    stop: async () => {
      await Promise.all([stopProcessTree(frontendProc), stopProcessTree(backendProc)]);
      try {
        fs.rmSync(tmpDir, { recursive: true, force: true });
      } catch {
        // ignore
      }
    },
  };
}
