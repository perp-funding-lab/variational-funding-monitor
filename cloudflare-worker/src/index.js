/**
 * perp-funding-lab-dispatch: Cloudflare Workers Cron Triggers から GitHub Actions(collect.yml)を起動する。
 *
 * GitHub Actions の schedule は間引かれたり遅れたりするので、Cron Triggers から workflow_dispatch を叩いて
 * 10 分ごとの収集時刻を安定させる。Actions 側の schedule(30 分ごと、直前に成功していれば何もしない)は保険。
 * 58 分の起動は支払時刻(正時)の直前の観測のため(funding.last は窓の中で動く見込み値なので)。
 *
 * 認証は Organization 所有の GitHub App(個人の PAT は使わない)。dispatch のたびに、このリポ限定・actions: write だけの
 * installation token を発行する。Actions の一覧には「perp-funding-lab-collector[bot]」が起動したと出る。期限切れはない。
 *   secret  GITHUB_APP_PRIVATE_KEY … App の秘密鍵(PKCS#8)。scripts/gh_app.py pkcs8 | npx wrangler@4 secret put GITHUB_APP_PRIVATE_KEY
 *   vars    GITHUB_APP_ID / GITHUB_APP_INSTALLATION_ID / GITHUB_REPO / GITHUB_REF / WORKFLOW_FILE(wrangler.toml)
 * secret put の後は必ず npx wrangler@4 deploy し直す(cron が新しいバージョンに引き継がれないことがある)。
 * HTTP は受け付けない(workers_dev = false。届いても 404)。
 */
import {installationToken} from "./github_app.js";

// wrangler.toml の [triggers] crons と 1 文字違わず一致させること
export const CRON_WORKFLOW = {
  "8,18,28,38,48,58 * * * *": "collect.yml",
};

const GITHUB_API_VERSION = "2022-11-28";
const USER_AGENT = "perp-funding-lab-dispatch";
const MAX_ATTEMPTS = 2;
const RETRY_DELAY_MS = 3000;   // 一時的な 5xx / ネットワーク断は少し待ってから再試行

function secret(env, name) {
  return String(env[name] || "").trim();
}

/** controller.scheduledTime(ms)→ '2026-10-25T00:08:00Z'(秒まで)。collect.yml の inputs.scheduled に渡す */
export function isoSeconds(ms) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

export async function dispatchWorkflow(env, workflow, scheduledIso, nowSec, fetchImpl) {
  const doFetch = fetchImpl || fetch;
  const repo = String(env.GITHUB_REPO || "");
  const repoName = repo.split("/")[1];
  if (!repoName) throw new Error("GITHUB_REPO が未設定です(owner/name)");
  const {token} = await installationToken({
    privateKey: secret(env, "GITHUB_APP_PRIVATE_KEY"), appId: secret(env, "GITHUB_APP_ID"),
    installationId: secret(env, "GITHUB_APP_INSTALLATION_ID"), repo: repoName, permissions: {actions: "write"},
    nowSec, userAgent: USER_AGENT, fetchImpl: doFetch,
  });
  const url = `https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`;
  let lastError;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    let res;
    try {
      res = await doFetch(url, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,   // トークンはヘッダのみ。ログには出さない
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": GITHUB_API_VERSION,
          "User-Agent": USER_AGENT,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ref: env.GITHUB_REF || "main", inputs: {scheduled: scheduledIso}}),
      });
    } catch (e) {
      lastError = `network error: ${e.message}`;
      if (attempt < MAX_ATTEMPTS) await new Promise((r) => setTimeout(r, RETRY_DELAY_MS));
      continue;
    }
    if (res.ok) {   // 204(2022-11-28)/ 200
      console.log(`dispatched ${workflow} scheduled=${scheduledIso} (attempt ${attempt}, HTTP ${res.status})`);
      return res.status;
    }
    const body = (await res.text()).slice(0, 300);
    const hint = res.status === 403 ? " → App の権限不足(actions: write)"
      : res.status === 404 ? ` → リポ(${repo})かワークフロー(${workflow})が見つからない`
      : res.status === 422 ? " → inputs がワークフロー定義と不一致(collect.yml の scheduled 入力)" : "";
    lastError = `HTTP ${res.status} ${body}${hint}`;
    if (res.status >= 400 && res.status < 500) break;   // 4xx は再試行しても同じ
    if (attempt < MAX_ATTEMPTS) await new Promise((r) => setTimeout(r, RETRY_DELAY_MS));
  }
  throw new Error(`dispatch ${workflow} failed: ${lastError}`);
}

export default {
  async scheduled(controller, env, ctx) {
    const workflow = CRON_WORKFLOW[controller.cron];
    if (!workflow) {
      // wrangler.toml と CRON_WORKFLOW がずれると全起動が無言で失われるため、握りつぶさず失敗として記録する
      throw new Error(`no workflow mapped for cron ${JSON.stringify(controller.cron)}`);
    }
    const now = controller.scheduledTime || Date.now();
    ctx.waitUntil(dispatchWorkflow(env, workflow, isoSeconds(now), Math.floor(Date.now() / 1000)));
  },

  async fetch() {
    return new Response("Not Found", {status: 404});
  },
};
