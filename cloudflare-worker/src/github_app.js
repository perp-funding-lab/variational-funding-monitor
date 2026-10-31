/**
 * GitHub App の認証(Cloudflare Workers と Node の両方で動く。WebCrypto だけを使う)。
 *
 * 秘密鍵は PKCS#8(-----BEGIN PRIVATE KEY-----)。Worker の secret GITHUB_APP_PRIVATE_KEY に入れる
 * (scripts/gh_app.py pkcs8 | npx wrangler@4 secret put GITHUB_APP_PRIVATE_KEY)。改行が崩れても読めるよう空白は無視する。
 * JWT は scripts/gh_app.py の make_jwt と同じ文字列になる(RS256 は決定的なので tests で突き合わせている)。
 */
const enc = new TextEncoder();

export function b64url(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function pemToDer(pem) {
  const b64 = String(pem || "").replace(/-----[^-]+-----/g, "").replace(/\s+/g, "");
  if (!b64) throw new Error("GITHUB_APP_PRIVATE_KEY が空です");
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** App の JWT(iat は 60 秒前、exp は 9 分後。GitHub の上限は 10 分) */
export async function appJwt(pkcs8Pem, appId, nowSec) {
  const head = b64url(enc.encode(JSON.stringify({alg: "RS256", typ: "JWT"})));
  const body = b64url(enc.encode(JSON.stringify({iat: nowSec - 60, exp: nowSec + 540, iss: String(appId)})));
  const key = await crypto.subtle.importKey("pkcs8", pemToDer(pkcs8Pem), {name: "RSASSA-PKCS1-v1_5", hash: "SHA-256"}, false, ["sign"]);
  const sig = new Uint8Array(await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, enc.encode(`${head}.${body}`)));
  return `${head}.${body}.${b64url(sig)}`;
}

/** インストールのトークンを、1 つのリポと最小の権限に絞って発行する(有効 1 時間。使い捨て) */
export async function installationToken({privateKey, appId, installationId, repo, permissions, nowSec, userAgent, fetchImpl}) {
  const doFetch = fetchImpl || fetch;
  const jwt = await appJwt(privateKey, appId, nowSec);
  const res = await doFetch(`https://api.github.com/app/installations/${installationId}/access_tokens`, {
    method: "POST",
    headers: {
      // JWT はヘッダのみ。URL やログには載せない
      Authorization: `Bearer ${jwt}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": userAgent,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({repositories: [repo], permissions}),
  });
  if (res.status !== 201) {
    const text = (await res.text()).slice(0, 300);
    const hint = res.status === 401 ? " → 秘密鍵か App ID が違う、または時計のずれ"
      : res.status === 404 ? " → installation ID が違う、または App がアンインストールされた"
      : res.status === 422 ? " → リポ名か権限の指定が App の設定と合わない" : "";
    throw new Error(`installation token: HTTP ${res.status} ${text}${hint}`);
  }
  const j = await res.json();
  return {token: j.token, expiresAt: j.expires_at, permissions: j.permissions};
}
