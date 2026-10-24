#!/usr/bin/env bash
# 本人名(大文字小文字を区別しない)が外から見える場所に無いことを確認する。
#   使い方: bash scripts/check_identifiers.sh [Organization 名] [リポ名] [Worker 名]
# パターンは文字クラスで書き、このスクリプト自身が一致しないようにしている。
set -u
cd "$(dirname "$0")/.."
PAT='h[y]u'
fail=0
echo "== git log の作者・コミッター"
n=$(git log --format='%an <%ae> / %cn <%ce>' | grep -ic "$PAT" || true)
echo "   一致 $n 件"; [ "$n" = "0" ] || fail=1
echo "== コード・設定・ページ・ワークフロー・README(data/ とローカル生成物は除く)"
if grep -rIin --exclude-dir=.git --exclude-dir=data --exclude-dir=node_modules --exclude-dir=.cache \
     --exclude-dir=.wrangler --exclude-dir=.venv --exclude-dir=__pycache__ --exclude-dir=.pytest_cache "$PAT" . ; then
  fail=1
else
  echo "   一致なし"
fi
echo "== Netlify への参照(デプロイ処理が無いこと)"
if grep -rIin --exclude-dir=.git --exclude-dir=data --exclude-dir=node_modules --exclude-dir=.cache \
     --exclude-dir=.wrangler --exclude-dir=.venv --exclude-dir=__pycache__ 'netl[i]fy' . | grep -v 'check_identifiers.sh' ; then
  echo "   (上の一致を確認。元ページの URL への言及だけなら問題ない)"
else
  echo "   一致なし"
fi
for id in "$@"; do
  printf '== 識別子 %s: ' "$id"
  if printf '%s' "$id" | grep -qi "$PAT"; then echo NG; fail=1; else echo OK; fi
done
[ "$fail" = 0 ] && echo "RESULT: OK" || echo "RESULT: NG"
exit $fail
