/* Cloudflare Worker(perp-funding-lab-dispatch)の GitHub App 認証と起動の流れ。鍵はテスト中に使い捨てで作る。 */
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const {pathToFileURL} = require("node:url");

const SRC = path.join(__dirname, "..", "..", "cloudflare-worker", "src");
const load = f => import(pathToFileURL(path.join(SRC, f)).href);
const ENV = pem => ({GITHUB_APP_PRIVATE_KEY: pem, GITHUB_APP_ID: "5553022", GITHUB_APP_INSTALLATION_ID: "123",
                     GITHUB_REPO: "perp-funding-lab/variational-funding-monitor", GITHUB_REF: "main"});

async function makeKey() {
  const kp = await crypto.subtle.generateKey({name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]),
                                              hash: "SHA-256"}, true, ["sign", "verify"]);
  const der = Buffer.from(await crypto.subtle.exportKey("pkcs8", kp.privateKey));
  const pem = "-----BEGIN PRIVATE KEY-----\n" + der.toString("base64").match(/.{1,64}/g).join("\n") + "\n-----END PRIVATE KEY-----\n";
  return {pem, publicKey: kp.publicKey};
}

test("appJwt: RS256 で署名され、iat は 60 秒前・exp は 9 分後・iss は App ID", async () => {
  const {appJwt} = await load("github_app.js");
  const {pem, publicKey} = await makeKey();
  const jwt = await appJwt(pem, 5553022, 1793434080);
  const [h, b, s] = jwt.split(".");
  assert.deepEqual(JSON.parse(Buffer.from(h, "base64url")), {alg: "RS256", typ: "JWT"});
  assert.deepEqual(JSON.parse(Buffer.from(b, "base64url")), {iat: 1793434020, exp: 1793434620, iss: "5553022"});
  assert.ok(await crypto.subtle.verify("RSASSA-PKCS1-v1_5", publicKey, Buffer.from(s, "base64url"), new TextEncoder().encode(`${h}.${b}`)));
  assert.equal(await appJwt(pem.replace(/\n/g, " "), 5553022, 1793434080), jwt);   // 改行が崩れても同じ
  await assert.rejects(appJwt("", 1, 1), /空/);
});

test("installationToken: このリポ限定・指定の権限だけを要求し、201 以外はエラー", async () => {
  const {installationToken} = await load("github_app.js");
  const {pem} = await makeKey();
  const calls = [];
  const ok = async (url, init) => { calls.push({url, init}); return new Response(JSON.stringify({token: "ghs_x", expires_at: "2026-10-31T09:08:00Z", permissions: {actions: "write"}}), {status: 201}); };
  const t = await installationToken({privateKey: pem, appId: "5553022", installationId: "123", repo: "variational-funding-monitor",
                                     permissions: {actions: "write"}, nowSec: 1793434080, userAgent: "ua", fetchImpl: ok});
  assert.equal(t.token, "ghs_x");
  assert.equal(calls[0].url, "https://api.github.com/app/installations/123/access_tokens");
  assert.equal(calls[0].init.method, "POST");
  assert.match(calls[0].init.headers.Authorization, /^Bearer [\w-]+\.[\w-]+\.[\w-]+$/);
  assert.deepEqual(JSON.parse(calls[0].init.body), {repositories: ["variational-funding-monitor"], permissions: {actions: "write"}});
  const ng = async () => new Response("Bad credentials", {status: 401});
  await assert.rejects(installationToken({privateKey: pem, appId: "1", installationId: "1", repo: "r", permissions: {}, nowSec: 1, userAgent: "ua", fetchImpl: ng}), /HTTP 401/);
});

test("scheduled: cron → トークン発行 → workflow_dispatch(inputs.scheduled は予定時刻)", async () => {
  const worker = (await load("index.js")).default;
  const {pem} = await makeKey();
  const calls = [], waits = [], orig = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({url, init});
    return url.endsWith("/access_tokens") ? new Response(JSON.stringify({token: "ghs_test"}), {status: 201}) : new Response(null, {status: 204});
  };
  try {
    await worker.scheduled({cron: "8,18,28,38,48,58 * * * *", scheduledTime: Date.UTC(2026, 9, 31, 8, 8)}, ENV(pem), {waitUntil: p => waits.push(p)});
    assert.equal(await Promise.all(waits).then(r => r[0]), 204);
  } finally { globalThis.fetch = orig; }
  assert.equal(calls.length, 2);
  assert.equal(calls[1].url, "https://api.github.com/repos/perp-funding-lab/variational-funding-monitor/actions/workflows/collect.yml/dispatches");
  assert.equal(calls[1].init.headers.Authorization, "Bearer ghs_test");
  assert.deepEqual(JSON.parse(calls[1].init.body), {ref: "main", inputs: {scheduled: "2026-10-31T08:08:00Z"}});
});

test("scheduled: 未知の cron は失敗にする、dispatch の 4xx は再試行しない", async () => {
  const mod = await load("index.js");
  await assert.rejects(mod.default.scheduled({cron: "* * * * *"}, {}, {waitUntil() {}}), /no workflow mapped/);
  const {pem} = await makeKey();
  let n = 0;
  const f = async (url) => url.endsWith("/access_tokens") ? new Response(JSON.stringify({token: "t"}), {status: 201}) : (n++, new Response("nope", {status: 422}));
  await assert.rejects(mod.dispatchWorkflow(ENV(pem), "collect.yml", "2026-10-31T08:08:00Z", 1793434080, f), /HTTP 422/);
  assert.equal(n, 1);
});

test("wrangler.toml の cron と CRON_WORKFLOW が 1 文字違わず一致し、公開 URL を作らない", async () => {
  const {CRON_WORKFLOW} = await load("index.js");
  const toml = fs.readFileSync(path.join(SRC, "..", "wrangler.toml"), "utf8");
  const crons = [...toml.matchAll(/^\s*"([^"]+)",?\s*(#.*)?$/gm)].map(m => m[1]).filter(s => s.split(" ").length === 5);
  assert.deepEqual(crons.sort(), Object.keys(CRON_WORKFLOW).sort());
  assert.match(toml, /^workers_dev = false$/m);
  assert.match(toml, /^name = "perp-funding-lab-dispatch"$/m);
});
