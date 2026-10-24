/**
 * variational-funding-monitor 外部トリガー(Cloudflare Workers Cron Triggers)
 *
 * GitHub Actions の schedule 実行は大幅に間引かれる・遅れることがあるため、Cron Triggers から
 * workflow_dispatch を叩いて 10 分ごとの収集時刻を安定させる。Actions 側の schedule(30 分ごと)は保険。
 * 58 分の起動は、支払時刻(正時)の直前の最終観測のために必要(funding.last は窓の見込み値で動くため)。
 *
 * PAT は Workers の Secret (GITHUB_TOKEN) から読む。コードには持たない。
 * デプロイ手順(secret put の後は必ず deploy し直す。cron が新バージョンに引き継がれないことがある):
 *   cd cloudflare-worker
 *   npx wrangler@4 deploy
 *   bash ../scripts/set_worker_secret.sh   # PAT を secret put して deploy し直す
 *
 * HTTP は受け付けない(workers_dev = false。届いても 404)。
 */

// wrangler.toml の [triggers] crons と1文字違わず一致させること
const CRON_WORKFLOW = {
  "8,18,28,38,48,58 * * * *": "collect.yml",
};

const GITHUB_API_VERSION = "2022-11-28";
const USER_AGENT = "variational-funding-monitor-cron";
const MAX_ATTEMPTS = 2;
const RETRY_DELAY_MS = 3000; // 一時的な 5xx / ネットワーク断は少し待ってから再試行
// GitHub が PAT 利用時に返す有効期限ヘッダ(期限なしトークンでは返らない)
const EXPIRY_HEADER = "github-authentication-token-expiration";
const PAT_EXPIRY_WARN_DAYS = 14;

// 401 の原因で多いのが貼り付け時の空白・改行混入。使う直前に必ず落とす。
function secret(env, name) {
  return String(env[name] || "").trim();
}

/** controller.scheduledTime(ms)→ '2026-10-25T00:08:00Z'(秒まで)。collect.yml の inputs.scheduled に渡す */
function isoSeconds(ms) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

async function dispatchWorkflow(env, workflow, scheduledIso) {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${workflow}/dispatches`;
  const token = secret(env, "GITHUB_TOKEN");
  if (!token) {
    throw new Error("GITHUB_TOKEN が未設定です。`bash scripts/set_worker_secret.sh` で登録してください");
  }
  let lastError;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    let res;
    try {
      res = await fetch(url, {
        method: "POST",
        headers: {
          // トークンはヘッダのみ。URL やログには決して載せない
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": GITHUB_API_VERSION,
          "User-Agent": USER_AGENT,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: env.GITHUB_REF, inputs: { scheduled: scheduledIso } }),
      });
    } catch (e) {
      lastError = `network error: ${e.message}`;
      if (attempt < MAX_ATTEMPTS) await new Promise((r) => setTimeout(r, RETRY_DELAY_MS));
      continue;
    }
    // 204(API バージョン 2022-11-28)と 200 の両方が成功
    if (res.ok) {
      console.log(`dispatched ${workflow} scheduled=${scheduledIso} (attempt ${attempt}, HTTP ${res.status})`);
      return res;
    }
    const body = (await res.text()).slice(0, 300);
    const hint =
      res.status === 401
        ? " → PAT が無効(期限切れ/失効/貼り付け誤り)。新しい PAT を発行し scripts/set_worker_secret.sh で再登録"
        : res.status === 403
          ? " → PAT の権限不足。fine-grained なら対象リポジトリに Actions: Read and write が必要"
          : res.status === 404
            ? ` → リポジトリ(${env.GITHUB_REPO})かワークフロー(${workflow})が見つからない`
            : res.status === 422
              ? " → inputs がワークフロー定義と不一致(collect.yml の scheduled 入力を確認)"
              : "";
    lastError = `HTTP ${res.status} ${body}${hint}`;
    if (res.status >= 400 && res.status < 500) break; // 4xx は再試行しても同じ
    if (attempt < MAX_ATTEMPTS) await new Promise((r) => setTimeout(r, RETRY_DELAY_MS));
  }
  throw new Error(`dispatch ${workflow} failed: ${lastError}`);
}

/** dispatch 応答のヘッダから PAT の残り日数をログに出す(通知先は持たない。wrangler tail / ダッシュボードで見る) */
function logPatExpiry(res, nowMs) {
  const raw = res && res.headers ? res.headers.get(EXPIRY_HEADER) : null;
  if (!raw) {
    console.log("PAT expiry header absent — 期限監視は不可");
    return;
  }
  const expiresAt = Date.parse(raw);
  if (Number.isNaN(expiresAt)) return;
  const daysLeft = Math.floor((expiresAt - nowMs) / 86400000);
  console.log(`PAT expires in ${daysLeft} day(s) (${raw})`);
  if (daysLeft <= PAT_EXPIRY_WARN_DAYS) console.warn(`PAT の有効期限が近い(残り ${daysLeft} 日)。再発行して set_worker_secret.sh で更新すること`);
}

export default {
  async scheduled(controller, env, ctx) {
    const workflow = CRON_WORKFLOW[controller.cron];
    if (!workflow) {
      // wrangler.toml と CRON_WORKFLOW がずれると全起動が無言で失われるため、握りつぶさず失敗として記録する
      throw new Error(`no workflow mapped for cron ${JSON.stringify(controller.cron)}`);
    }
    ctx.waitUntil(
      (async () => {
        const now = controller.scheduledTime || Date.now();
        const res = await dispatchWorkflow(env, workflow, isoSeconds(now));
        logPatExpiry(res, now);
      })()
    );
  },

  async fetch() {
    return new Response("Not Found", { status: 404 });
  },
};
