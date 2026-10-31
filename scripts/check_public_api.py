#!/usr/bin/env python3
"""認証なしの GitHub API(誰でも見られる情報)に、本人のアカウント名などが出ていないことを確かめる。

  python3 scripts/check_public_api.py [--extra 文字列 ...]

既定で探すのは h[y]u(大文字小文字を区別しない)。本人のアカウント名など、リポに書きたくない文字列は --extra で渡す。
見る場所: Organization 情報(公開メール含む)・公開メンバー・Organization のイベント・リポ情報・リポのイベント・
Actions の実行一覧(actor / triggering_actor)・コミット(作者・コミッター)・デプロイ(作成者)・contributors。
各場所に現れたアカウント名の一覧も出す(App の bot と github-actions[bot] だけのはず)。一致が 1 つでもあれば終了コード 1。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

ORG, REPO = "perp-funding-lab", "variational-funding-monitor"
API = "https://api.github.com"


def _ctx():
    ctx = ssl.create_default_context()
    if os.path.exists("/etc/ssl/cert.pem"):
        try:
            ctx.load_verify_locations("/etc/ssl/cert.pem")
        except (OSError, ssl.SSLError):
            pass
    return ctx


CTX = _ctx()


def get(path: str):
    req = urllib.request.Request(API + path, headers={"Accept": "application/vnd.github+json", "User-Agent": "perp-funding-lab-check"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def logins(obj) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "login" and isinstance(v, str):
                out.add(v)
            else:
                out |= logins(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= logins(v)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extra", action="append", default=[], help="追加で探す文字列(大文字小文字を区別しない)")
    args = ap.parse_args()
    pats = [re.compile("h[y]u", re.I)] + [re.compile(re.escape(x), re.I) for x in args.extra if x]
    targets = [
        ("Organization 情報", f"/orgs/{ORG}"),
        ("公開メンバー", f"/orgs/{ORG}/public_members"),
        ("Organization のイベント", f"/orgs/{ORG}/events?per_page=100"),
        ("リポ情報", f"/repos/{ORG}/{REPO}"),
        ("リポのイベント", f"/repos/{ORG}/{REPO}/events?per_page=100"),
        ("Actions の実行一覧", f"/repos/{ORG}/{REPO}/actions/runs?per_page=100"),
        ("コミット", f"/repos/{ORG}/{REPO}/commits?per_page=100"),
        ("デプロイ", f"/repos/{ORG}/{REPO}/deployments?per_page=100"),
        ("contributors", f"/repos/{ORG}/{REPO}/contributors?anon=1&per_page=100"),
    ]
    bad = 0
    for label, path in targets:
        st, body = get(path)
        try:
            j = json.loads(body) if body.strip() else None
        except ValueError:
            j = None
        hits = sorted({m.group(0) for p in pats for m in p.finditer(body)})
        n = len(j) if isinstance(j, list) else (len(j.get("workflow_runs", [])) if isinstance(j, dict) and "workflow_runs" in j else "-")
        extra = ""
        if isinstance(j, dict) and label == "Organization 情報":
            extra = " " + json.dumps({k: j.get(k) for k in ("name", "email", "blog", "company", "location", "description", "twitter_username")},
                                     ensure_ascii=False)
        print(f"{'NG' if hits else 'OK'}  {label}: HTTP {st}, 件数 {n}, 現れたアカウント {sorted(logins(j))}{extra}"
              + (f"  一致 {len(hits)} 件" if hits else ""))
        bad += bool(hits)
    print("RESULT:", "OK" if not bad else "NG")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
