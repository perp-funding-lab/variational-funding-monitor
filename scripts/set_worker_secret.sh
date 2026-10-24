#!/usr/bin/env bash
# fine-grained PAT を Cloudflare Worker の secret(GITHUB_TOKEN)に登録し、Worker をデプロイし直す。
# PAT は端末で貼り付ける。画面・履歴・ファイル・チャットには出さない(確認は先頭 8 文字と末尾 4 文字だけ)。
#   使い方: bash scripts/set_worker_secret.sh [--dispatch]
#   --dispatch を付けると、登録前に PAT で collect.yml を 1 回 workflow_dispatch して疎通を確かめる。
set -euo pipefail
cd "$(dirname "$0")/../cloudflare-worker"
REPO=$(grep -E '^GITHUB_REPO' wrangler.toml | sed -E 's/.*"([^"]+)"/\1/')
WORKFLOW=$(grep -E '^WORKFLOW_FILE' wrangler.toml | sed -E 's/.*"([^"]+)"/\1/')
echo "対象: $REPO / $WORKFLOW"
printf 'fine-grained PAT を貼り付けて Enter(入力は表示されません): '
IFS= read -r -s PAT; echo
PAT=$(printf '%s' "$PAT" | tr -d '[:space:]')
if [ "${#PAT}" -lt 20 ]; then echo "短すぎます(貼り付けに失敗?)"; exit 1; fi
echo "確認: ${PAT:0:8}...${PAT: -4}(長さ ${#PAT})"

HDR=$(mktemp); trap 'rm -f "$HDR"' EXIT
CODE=$(curl -sS -m 20 -o /dev/null -D "$HDR" -w '%{http_code}' \
  -H "Authorization: Bearer $PAT" -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/repos/$REPO/actions/workflows")
echo "GitHub API(ワークフロー一覧): HTTP $CODE"
EXP=$(grep -i '^github-authentication-token-expiration:' "$HDR" | sed -E 's/^[^:]+: *//' | tr -d '\r' || true)
if [ -n "$EXP" ]; then echo "PAT の期限: $EXP"; else echo "PAT の期限: (ヘッダなし)"; fi
if [ "$CODE" != "200" ]; then
  echo "この PAT では $REPO のワークフロー一覧が取れません。リソース所有者=Organization、対象=このリポのみ、Actions: Read and write を確認してください"; exit 1
fi
if [ "${1:-}" = "--dispatch" ]; then
  CODE=$(curl -sS -m 20 -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $PAT" -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/$REPO/actions/workflows/$WORKFLOW/dispatches" \
    -d '{"ref":"main","inputs":{"scheduled":""}}')
  echo "試し dispatch: HTTP $CODE(204 なら成功)"
  [ "$CODE" = "204" ] || { echo "dispatch できません(Actions: Read and write が無い?)"; exit 1; }
fi
printf '%s' "$PAT" | npx wrangler@4 secret put GITHUB_TOKEN
# secret put で作られた新バージョンに cron が引き継がれないことがあるので deploy し直す
npx wrangler@4 deploy
echo "完了。PAT の期限: ${EXP:-不明}"
