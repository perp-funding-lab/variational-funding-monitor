#!/usr/bin/env bash
# Organization の下に公開リポを作り、push し、GitHub Pages(Actions デプロイ)を有効にする。
#   使い方: bash scripts/bootstrap_github.sh <Organization>
set -euo pipefail
ORG=${1:?Organization 名}
REPO=variational-funding-monitor
cd "$(dirname "$0")/.."
printf '%s' "$ORG" | grep -qi 'h[y]u' && { echo "Organization 名に本人名が含まれています"; exit 1; }
# プレースホルダを置き換える(Worker の設定と README の URL)
sed -i.bak "s#__ORG__#${ORG}#g" cloudflare-worker/wrangler.toml README.md   # BSD/GNU 両対応
rm -f cloudflare-worker/wrangler.toml.bak README.md.bak
git add cloudflare-worker/wrangler.toml README.md
git -c commit.gpgsign=false diff --cached --quiet || git -c commit.gpgsign=false commit -q -m "Organization 名を設定

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
if ! gh repo view "$ORG/$REPO" >/dev/null 2>&1; then
  gh repo create "$ORG/$REPO" --public --description "Variational Omni perpetual funding rate monitor (static page on GitHub Pages)" --disable-wiki
fi
git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$ORG/$REPO.git"
git push -u origin main
# Pages: カスタム Actions ワークフローでデプロイ
gh api -X POST "repos/$ORG/$REPO/pages" -f build_type=workflow >/dev/null 2>&1 || gh api -X PUT "repos/$ORG/$REPO/pages" -f build_type=workflow >/dev/null
gh api "repos/$ORG/$REPO/pages" --jq '{html_url: .html_url, build_type: .build_type, status: .status}'
echo "done: https://github.com/$ORG/$REPO"
