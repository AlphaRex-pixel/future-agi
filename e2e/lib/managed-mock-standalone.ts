// Standalone counterpart of inspectManagedMock (lib/managed-mock.ts), used only
// when E2E_STACK=standalone (bin/e2e targets docker-compose.yml +
// e2e/stack/docker-compose.standalone-e2e.yml, project futureagi-e2e-standalone).
//
// The Distributed inspection pins one container per service (backend, worker,
// agentcc-gateway, frontend) and runs managed-mock-background.py inside the
// backend and worker. In Standalone those processes all live in the single
// `app` container, so the same safety contract is re-expressed here:
//   - localhost endpoints and an explicit local Docker context only;
//   - the four Standalone services run in the managed project on one bridge network;
//   - the harness ports are published by `app`, `postgres` and `clickhouse`;
//   - the in-container gateway reads e2e/stack/gateway.e2e.yaml (read-only bind),
//     which validateMockRouting proves routes every model to mock-llm only;
//   - mock-llm runs e2e/stack/mock-llm/server.mjs, unchanged since startup;
//   - `app` carries no real provider key, licence, notification credential or
//     Google credential file, and telemetry is off.
// Not re-expressed: the Python constructor attestation of the background eval
// client (managed-mock-background.py pins Distributed hostnames such as
// temporal:7233 and agentcc-gateway:8080). evalBackground therefore adds only
// the Standalone environment pins below.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync, realpathSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { E2E } from './env';
import { validateMockRouting, type MockReceipt } from './managed-mock';

const root = fileURLToPath(new URL('../../', import.meta.url));
const gatewayFile = `${root}e2e/stack/gateway.e2e.yaml`;
const mockFile = `${root}e2e/stack/mock-llm/server.mjs`;
const MOCK_KEY = 'local-dev-only-shared-secret-replace-me';
const hash = (value: string | Buffer) => createHash('sha256').update(value).digest('hex');
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const requireSafe = (ok: unknown, reason: string): void => {
  if (!ok) throw new Error(`STOP: managed mock (standalone) ${reason}`);
};

interface Container {
  Id: string; Image: string; State: { Running: boolean; StartedAt: string };
  Config: { Labels: Record<string, string>; Env: string[]; Cmd: string[] };
  HostConfig: { ExtraHosts: string[] | null; Dns?: string[]; DnsSearch?: string[] };
  Mounts: { Type: string; Source: string; Destination: string; RW: boolean }[];
  NetworkSettings: { Networks: Record<string, { NetworkID: string; Aliases: string[] }>;
    Ports: Record<string, { HostIp: string; HostPort: string }[] | null> };
}

export function validateStandaloneAppEnvironment(env: Record<string, string>, evalBackground: boolean): void {
  for (const [key, value] of Object.entries(env)) {
    if (!value) continue;
    requireSafe(!/^(https?_proxy|all_proxy|node_options|ld_preload|pythonpath|pythonstartup)$/i.test(key),
      'app has a proxy/preload override');
    if (/_API_KEY$/.test(key) || ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'].includes(key)) {
      requireSafe(value === MOCK_KEY || value === 'e2e-mock', `app has a non-mock provider key (${key})`);
    }
    requireSafe(!/^(EE_LICENSE_KEY|SENTRY_DSN|SLACK_.*|DEPLOYMENT_TELEMETRY_SLACK_WEBHOOK|ERROR_LOGS_WEBHOOK|MIX_PANEL_TOKEN|MAILGUN_API_KEY|SMTP_.*|SENDGRID_.*|RESEND_.*|AWS_SESSION_TOKEN|GOOGLE_APPLICATION_CREDENTIALS|DAYTONA_API_KEY|E2B_API_KEY)$/.test(key),
      `app has a license, notification or credential override (${key})`);
  }
  const required: Record<string, string> = {
    FUTURE_AGI_TELEMETRY_DISABLED: 'true', AGENTCC_INTERNAL_API_KEY: MOCK_KEY,
    AGENTCC_INTERNAL_URL: 'http://127.0.0.1:8080', AGENTCC_GATEWAY_INTERNAL_URL: 'http://127.0.0.1:8080',
  };
  if (evalBackground) Object.assign(required, { MODEL_SERVING_URL: 'http://mock-llm:8080', ENV_TYPE: 'local',
    TEMPORAL_HOST: '127.0.0.1:7233', TEMPORAL_NAMESPACE: 'default', OTEL_ENABLED: 'false',
    DJANGO_SETTINGS_MODULE: 'tfc.settings.settings', NO_STARTUP_DB_MUTATIONS: 'true' });
  for (const [key, value] of Object.entries(required)) requireSafe(env[key] === value, `required app ${key} mismatch`);
}

export function inspectStandaloneMock({ evalBackground = false }: { evalBackground?: boolean } = {}): MockReceipt {
  for (const endpoint of [E2E.appUrl, E2E.apiUrl, E2E.gatewayUrl, E2E.pgUrl, E2E.chUrl]) {
    requireSafe(new URL(endpoint).hostname === 'localhost', 'requires localhost endpoints');
  }
  const context = process.env.DOCKER_CONTEXT || (process.env.CI ? 'default' : '');
  requireSafe(context, 'requires an explicit Docker context outside CI');
  requireSafe(!process.env.DOCKER_HOST, 'DOCKER_HOST override is not supported');
  const run = (file: string, args: string[]) => execFileSync(file, args, { encoding: 'utf8', timeout: 30_000,
    maxBuffer: 2 * 1024 * 1024, env: { ...process.env, DOCKER_CONTEXT: context, E2E_STACK: 'standalone' },
    stdio: ['pipe', 'pipe', 'pipe'] });
  const docker = (...args: string[]) => run('docker', ['--context', context, ...args]);
  const [daemon] = JSON.parse(docker('context', 'inspect', context));
  requireSafe(daemon.Endpoints?.docker?.Host?.startsWith('unix://'), 'daemon is not local');
  const compose = (...args: string[]) => run(`${root}bin/e2e`, ['compose', ...args]);
  const config = JSON.parse(compose('config', '--format', 'json'));
  requireSafe(config.name === 'futureagi-e2e-standalone', 'not the standalone E2E project');
  const serviceNames = ['app', 'mock-llm', 'postgres', 'clickhouse'];
  const ids = compose('ps', '-q', ...serviceNames).trim().split(/\s+/);
  requireSafe(ids.length === serviceNames.length && ids.every(id => /^[a-f0-9]{64}$/.test(id)), 'services missing or ambiguous');
  const containers: Container[] = JSON.parse(docker('inspect', ...ids));
  const network = config.networks.default.name;
  const selected: Record<string, Container> = {};
  for (const service of serviceNames) {
    const matches = containers.filter(c => c.Config.Labels['com.docker.compose.service'] === service);
    requireSafe(matches.length === 1, `service ${service} is ambiguous`);
    const c = selected[service] = matches[0];
    requireSafe(c.State.Running && c.Config.Labels['com.docker.compose.project'] === config.name,
      `service ${service} is not running in the managed project`);
    requireSafe(same(Object.keys(c.NetworkSettings.Networks), [network]) &&
      c.NetworkSettings.Networks[network].Aliases.includes(service) && !c.HostConfig.Dns?.length &&
      !c.HostConfig.DnsSearch?.length, `service ${service} network or DNS override`);
    // docker-compose.yml maps code-executor to the in-app sandbox; the overlay adds
    // the agentcc-gateway alias for flows that register the Distributed gateway URL.
    const hosts = [...(c.HostConfig.ExtraHosts ?? [])].sort();
    requireSafe(service === 'app' ? same(hosts, ['agentcc-gateway:127.0.0.1', 'code-executor:127.0.0.1']) : !hosts.length,
      `service ${service} extra_hosts override`);
  }
  const networkId = selected['mock-llm'].NetworkSettings.Networks[network].NetworkID;
  requireSafe(containers.every(c => c.NetworkSettings.Networks[network].NetworkID === networkId), 'network identity mismatch');
  for (const [service, port, endpoint] of [
    ['app', '3000/tcp', E2E.appUrl], ['app', '8000/tcp', E2E.apiUrl], ['app', '8080/tcp', E2E.gatewayUrl],
    ['postgres', '5432/tcp', E2E.pgUrl], ['clickhouse', '8123/tcp', E2E.chUrl],
  ]) {
    requireSafe(selected[service].NetworkSettings.Ports[port]?.some(p =>
      p.HostPort === new URL(endpoint).port && ['127.0.0.1', '0.0.0.0', '::'].includes(p.HostIp)),
    `endpoint is not published by ${service}`);
  }
  for (const [service, destination, source] of [
    ['app', '/etc/futureagi/secrets/agentcc.yaml', gatewayFile],
    ['mock-llm', '/srv/server.mjs', mockFile],
  ] as const) {
    const c = selected[service];
    const mounts = c.Mounts.filter(m => m.Destination === destination);
    requireSafe(mounts.length === 1 && mounts[0].Type === 'bind' && !mounts[0].RW &&
      realpathSync(mounts[0].Source) === realpathSync(source), `unexpected ${service} source mount`);
    requireSafe(statSync(source).mtimeMs <= Date.parse(c.State.StartedAt), `${service} source changed after startup`);
  }
  const vertex = selected.app.Mounts.filter(m => m.Destination === '/etc/futureagi/secrets/vertex.json');
  requireSafe(vertex.length === 1 && vertex[0].Source === '/dev/null' && !vertex[0].RW,
    'app Google credential mount must be the read-only /dev/null suppression');
  requireSafe(same(selected['mock-llm'].Config.Cmd, ['node', '/srv/server.mjs']), 'unexpected mock-llm command');
  validateMockRouting(readFileSync(gatewayFile, 'utf8'), evalBackground);
  const env = Object.fromEntries(selected.app.Config.Env.map(s => { const i = s.indexOf('='); return [s.slice(0, i), s.slice(i + 1)]; }));
  validateStandaloneAppEnvironment(env, evalBackground);
  return { context, project: config.name, networkId, gatewaySha: hash(readFileSync(gatewayFile)),
    mockSha: hash(readFileSync(mockFile)), services: serviceNames.map(service => ({ service,
      id: selected[service].Id, image: selected[service].Image, startedAt: selected[service].State.StartedAt })) };
}
