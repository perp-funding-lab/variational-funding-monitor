#!/usr/bin/env python3
"""GitHub App(Organization 所有)の作成・認証・リポ操作をまとめたローカル用の道具(標準ライブラリのみ)。

秘密鍵は macOS の Keychain にだけ置く。画面・ログ・ファイル・コマンドライン引数には出さない
(Keychain への登録は `security -i` に標準入力で渡す。git push はトークンを環境変数経由の credential helper で渡す)。
App ID・installation ID・bot の ID は秘密ではないので config/github_app.json に記録する。

  python3 scripts/gh_app.py create             # manifest flow: ブラウザで作成画面を開き、戻りを受けて鍵を Keychain へ
  python3 scripts/gh_app.py wait-install       # インストールを待って installation_id を記録(最長 15 分)
  python3 scripts/gh_app.py bot                # bot ユーザーの ID と noreply アドレスを記録
  python3 scripts/gh_app.py check              # トークン発行の確認(期限と権限だけ表示)
  python3 scripts/gh_app.py create-repo        # Organization に公開リポを作る
  python3 scripts/gh_app.py push -- -u origin main
  python3 scripts/gh_app.py pages              # GitHub Pages を Actions デプロイで有効にする
  python3 scripts/gh_app.py dispatch collect.yml [--input scheduled=]
  python3 scripts/gh_app.py runs [--limit 10]
  python3 scripts/gh_app.py pkcs8 | npx wrangler@4 secret put GITHUB_APP_PRIVATE_KEY   # パイプ専用(端末には出さない)
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import html
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ORG = "perp-funding-lab"
REPO = "variational-funding-monitor"
DEFAULT_NAME = "perp-funding-lab-collector"
API = "https://api.github.com"
UA = "perp-funding-lab-setup"
ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "github_app.json"
KEYCHAIN_ACCOUNT = "github-app-private-key"
PERMISSIONS = {"administration": "write", "contents": "write", "actions": "write", "workflows": "write",
               "pages": "write", "metadata": "read"}
MAX_WAIT = 15 * 60


# ───────── HTTP ─────────
def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if os.path.exists("/etc/ssl/cert.pem"):   # python.org 版 Python(macOS)は既定の CA を持たないことがある
        try:
            ctx.load_verify_locations("/etc/ssl/cert.pem")
        except (OSError, ssl.SSLError):
            pass
    return ctx


CTX = _ssl_ctx()


def http(method: str, url: str, token: str | None = None, body=None, timeout: int = 30):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        j = json.loads(raw) if raw else None
    except ValueError:
        j = {"message": raw[:300].decode(errors="replace")}
    return status, j


def _msg(j) -> str:
    return (j or {}).get("message", "") if isinstance(j, dict) else ""


# ───────── 設定 ─────────
def load_cfg() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}


def save_cfg(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def need(cfg: dict, *keys: str) -> None:
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        raise SystemExit(f"config/github_app.json に {', '.join(missing)} がありません(先に create / wait-install / bot を実行)")


# ───────── Keychain ─────────
def keychain_store(service: str, account: str, value: str) -> None:
    if not value or any(c.isspace() or c in "\"'\\" for c in value):
        raise ValueError("Keychain に入れる値は空白・引用符を含まない 1 行にする")
    # 値を argv に載せない(ps で見えない)ように security の対話モードへ標準入力で渡す
    p = subprocess.run(["security", "-i"], input=f"add-generic-password -U -s {service} -a {account} -l {service} -w {value}\n",
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0 or keychain_load(service, account) != value:
        raise RuntimeError("Keychain への保存を確認できませんでした")


def keychain_load(service: str, account: str) -> str:
    r = subprocess.run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise SystemExit(f"Keychain に鍵がありません(サービス {service})")
    return r.stdout.strip()


def load_private_key_pem(cfg: dict) -> str:
    need(cfg, "keychain_service")
    return base64.b64decode(keychain_load(cfg["keychain_service"], cfg.get("keychain_account") or KEYCHAIN_ACCOUNT)).decode()


# ───────── RSA(標準ライブラリだけで PKCS#1 v1.5 / SHA-256 署名)─────────
def _der_read(buf: bytes, i: int):
    tag = buf[i]
    ln = buf[i + 1]
    i += 2
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(buf[i:i + n], "big")
        i += n
    return tag, buf[i:i + ln], i + ln


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _der(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(content)) + content


def pem_to_der(pem: str) -> tuple[str, bytes]:
    lines = [ln.strip() for ln in pem.strip().splitlines()]
    kind = lines[0].strip("-").replace("BEGIN ", "").strip()
    return kind, base64.b64decode("".join(ln for ln in lines if ln and not ln.startswith("-----")))


def pkcs1_der(pem: str) -> bytes:
    """PEM(PKCS#1 "RSA PRIVATE KEY" または PKCS#8 "PRIVATE KEY")→ PKCS#1 の DER。"""
    kind, der = pem_to_der(pem)
    if kind == "PRIVATE KEY":
        _, seq, _ = _der_read(der, 0)
        i = 0
        _, _, i = _der_read(seq, i)          # version
        _, _, i = _der_read(seq, i)          # AlgorithmIdentifier
        _, octet, _ = _der_read(seq, i)      # privateKey OCTET STRING
        return octet
    if kind != "RSA PRIVATE KEY":
        raise ValueError(f"unsupported key type {kind}")
    return der


def rsa_key(pem: str) -> dict:
    _, seq, _ = _der_read(pkcs1_der(pem), 0)
    vals, i = [], 0
    while i < len(seq):
        _, v, i = _der_read(seq, i)
        vals.append(int.from_bytes(v, "big"))
    _, n, e, d, p, q, dp, dq, qinv = vals[:9]
    return {"n": n, "e": e, "d": d, "p": p, "q": q, "dp": dp, "dq": dq, "qinv": qinv}


_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def rsa_sign_sha256(key: dict, msg: bytes) -> bytes:
    k = (key["n"].bit_length() + 7) // 8
    t = _SHA256_PREFIX + hashlib.sha256(msg).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    m = int.from_bytes(em, "big")
    m1, m2 = pow(m, key["dp"], key["p"]), pow(m, key["dq"], key["q"])
    s = m2 + ((key["qinv"] * (m1 - m2)) % key["p"]) * key["q"]
    if pow(s, key["e"], key["n"]) != m:   # 署名の自己検証(鍵の読み違いを早く見つける)
        raise ValueError("RSA self-check failed")
    return s.to_bytes(k, "big")


def to_pkcs8_pem(pem: str) -> str:
    algid = _der(0x30, bytes.fromhex("06092a864886f70d010101") + b"\x05\x00")   # rsaEncryption, NULL
    der = _der(0x30, _der(0x02, b"\x00") + algid + _der(0x04, pkcs1_der(pem)))
    b64 = base64.b64encode(der).decode()
    return "-----BEGIN PRIVATE KEY-----\n" + "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64)) + "\n-----END PRIVATE KEY-----\n"


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwt(pem: str, app_id, now: int | None = None) -> str:
    now = int(time.time()) if now is None else now
    head = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, separators=(",", ":")).encode())
    data = f"{head}.{body}".encode()
    return f"{head}.{body}.{b64url(rsa_sign_sha256(rsa_key(pem), data))}"


def app_jwt(cfg: dict) -> str:
    need(cfg, "app_id")
    return make_jwt(load_private_key_pem(cfg), cfg["app_id"])


def installation_token(cfg: dict, repositories=None, permissions=None) -> dict:
    need(cfg, "installation_id")
    body = {}
    if repositories:
        body["repositories"] = repositories
    if permissions:
        body["permissions"] = permissions
    st, j = http("POST", f"{API}/app/installations/{cfg['installation_id']}/access_tokens", token=app_jwt(cfg), body=body)
    if st != 201:
        raise SystemExit(f"installation token: HTTP {st} {_msg(j)}")
    return j


# ───────── コマンド ─────────
def _org_id() -> int:
    st, j = http("GET", f"{API}/orgs/{ORG}")
    if st != 200:
        raise SystemExit(f"Organization {ORG} を取得できません: HTTP {st}")
    return int(j["id"])


def _servers(handler, port: int = 0):
    """localhost が IPv4 / IPv6 のどちらに解決されても受けられるよう、同じポートで 127.0.0.1 と ::1 を待ち受ける。
    port を指定すると、そのポートで待ち受ける(時間切れの後、開いたままの作成画面から戻れるように)。"""
    class S6(HTTPServer):
        address_family = socket.AF_INET6
    for _ in range(1 if port else 20):
        s4 = HTTPServer(("127.0.0.1", port), handler)
        p = s4.server_address[1]
        try:
            return p, [s4, S6(("::1", p), handler)]
        except OSError:
            s4.server_close()
    s4 = HTTPServer(("127.0.0.1", port), handler)
    return s4.server_address[1], [s4]


def cmd_create(args) -> int:
    cfg = load_cfg()
    if cfg.get("app_id") and not args.force:
        print(f"作成済み: {cfg.get('slug')} (App ID {cfg['app_id']})")
        return 0
    org_id = _org_id()
    state = args.state or secrets.token_urlsafe(24)
    shared: dict = {}
    done = threading.Event()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):   # URL の code をログに出さない
            pass

        def _send(self, code: int, body: str, headers: dict | None = None):
            b = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/":
                self._send(200, shared["page"])
            elif u.path == "/callback":
                q = urllib.parse.parse_qs(u.query)
                code, st = (q.get("code") or [None])[0], (q.get("state") or [None])[0]
                if not code or st != state:
                    self._send(400, "<p>state が一致しません。</p>")
                    return
                with lock:
                    if done.is_set():
                        self._send(200, "<p>処理済みです。このタブは閉じて構いません。</p>")
                        return
                    status, j = http("POST", f"{API}/app-manifests/{urllib.parse.quote(code)}/conversions")
                    if status != 201 or not isinstance(j, dict) or not j.get("pem"):
                        shared["error"] = f"conversion HTTP {status} {_msg(j)}"
                        self._send(502, f"<p>App の作成結果を受け取れませんでした({html.escape(shared['error'])})。</p>")
                        done.set()
                        return
                    slug = j["slug"]
                    keychain_store(slug, KEYCHAIN_ACCOUNT, base64.b64encode(j["pem"].encode()).decode())
                    cfg.update({"org": ORG, "repo": REPO, "name": j.get("name"), "slug": slug, "app_id": j["id"],
                                "client_id": j.get("client_id"), "html_url": j.get("html_url"),
                                "owner": (j.get("owner") or {}).get("login"), "permissions": j.get("permissions"),
                                "keychain_service": slug, "keychain_account": KEYCHAIN_ACCOUNT,
                                "created_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
                    save_cfg(cfg)
                    shared["ok"] = True
                    dest = f"https://github.com/apps/{slug}/installations/new/permissions?target_id={org_id}"
                    self._send(302, f'<p><a href="{html.escape(dest)}">インストール画面へ</a></p>', {"Location": dest})
                    done.set()
            else:
                self._send(404, "<p>not found</p>")

    port, servers = _servers(Handler, args.port)
    manifest = {
        "name": args.name,
        "url": f"https://{ORG}.github.io/{REPO}/",
        "description": f"Triggers the collector and publishes the page of {ORG}/{REPO}.",
        "hook_attributes": {"url": f"https://{ORG}.github.io/{REPO}/", "active": False},
        "redirect_url": f"http://localhost:{port}/callback",
        "public": False,
        "default_permissions": PERMISSIONS,
        "default_events": [],
    }
    action = f"https://github.com/organizations/{ORG}/settings/apps/new?state={urllib.parse.quote(state)}"
    shared["page"] = (
        "<!doctype html><meta charset='utf-8'><title>GitHub App の作成</title>"
        f"<form id='f' method='post' action='{html.escape(action)}'>"
        f"<input type='hidden' name='manifest' value='{html.escape(json.dumps(manifest), quote=True)}'>"
        f"<p>GitHub の作成画面(GitHub App「{html.escape(args.name)}」)へ移動します…</p>"
        "<button type='submit'>GitHub で作成画面を開く</button></form>"
        "<script>document.getElementById('f').submit()</script>")
    threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True) for s in servers]
    for t in threads:
        t.start()
    if not args.no_open:
        subprocess.run(["open", f"http://localhost:{port}/"], check=False)
    # state は CSRF 対策の使い捨ての値(秘密情報ではない)。時間切れ後に同じ作成画面から戻れるよう表示しておく
    print(f"作成画面を開きました(待ち受け localhost:{port}、最長 {args.timeout} 秒)。再待ち受け: --port {port} --state {state} --no-open", flush=True)
    finished = done.wait(timeout=min(args.timeout, MAX_WAIT))
    time.sleep(1.0)   # 302 の応答を送り切る
    for s in servers:
        s.shutdown()
        s.server_close()
    if not finished:
        print("時間切れ(作成画面での操作がありませんでした)")
        return 3
    if not shared.get("ok"):
        print("失敗: " + shared.get("error", "unknown"))
        return 1
    print(json.dumps({k: cfg[k] for k in ("slug", "app_id", "client_id", "owner", "html_url", "keychain_service")}, ensure_ascii=False))
    return 0


def cmd_wait_install(args) -> int:
    cfg = load_cfg()
    need(cfg, "app_id")
    deadline = time.time() + min(args.timeout, MAX_WAIT)
    last = None
    while time.time() < deadline:
        st, j = http("GET", f"{API}/app/installations", token=app_jwt(cfg))
        last = st
        if st == 200:
            for inst in j:
                if (inst.get("account") or {}).get("login", "").lower() == ORG:
                    cfg.update({"installation_id": inst["id"], "repository_selection": inst.get("repository_selection"),
                                "installation_permissions": inst.get("permissions")})
                    save_cfg(cfg)
                    print(json.dumps({"installation_id": inst["id"], "repository_selection": inst.get("repository_selection"),
                                      "permissions": inst.get("permissions")}, ensure_ascii=False))
                    return 0 if inst.get("repository_selection") == "all" else 4
        time.sleep(5)
    print(f"時間切れ(インストールが見つかりません。最後の HTTP {last})")
    return 3


def cmd_bot(args) -> int:
    cfg = load_cfg()
    need(cfg, "slug")
    login = f"{cfg['slug']}[bot]"
    st, j = http("GET", f"{API}/users/{urllib.parse.quote(login)}")
    if st != 200:
        raise SystemExit(f"bot ユーザーを取得できません: HTTP {st} {_msg(j)}")
    cfg.update({"bot_login": j["login"], "bot_id": j["id"], "bot_email": f"{j['id']}+{j['login']}@users.noreply.github.com"})
    save_cfg(cfg)
    print(json.dumps({k: cfg[k] for k in ("bot_login", "bot_id", "bot_email")}, ensure_ascii=False))
    return 0


def cmd_check(args) -> int:
    cfg = load_cfg()
    t = installation_token(cfg, repositories=[REPO] if args.repo else None, permissions={"actions": "write"} if args.repo else None)
    print(json.dumps({"expires_at": t.get("expires_at"), "permissions": t.get("permissions"),
                      "repository_selection": t.get("repository_selection")}, ensure_ascii=False))
    return 0


def cmd_create_repo(args) -> int:
    cfg = load_cfg()
    tok = installation_token(cfg)["token"]
    st, j = http("POST", f"{API}/orgs/{ORG}/repos", token=tok, body={
        "name": REPO, "description": "Variational Omni の無期限(perp)全銘柄のファンディングレートを記録し続ける静的ページ(非公式)",
        "homepage": f"https://{ORG}.github.io/{REPO}/", "private": False, "visibility": "public",
        "has_issues": False, "has_projects": False, "has_wiki": False, "has_discussions": False, "auto_init": False})
    if st == 201:
        print(json.dumps({"created": j["full_name"], "html_url": j["html_url"], "owner": j["owner"]["login"]}, ensure_ascii=False))
        return 0
    if st == 422 and "already exists" in json.dumps(j):
        print("既にあります")
        return 0
    print(f"リポを作れませんでした: HTTP {st} {_msg(j)}")
    return 1


def cmd_push(args) -> int:
    cfg = load_cfg()
    tok = installation_token(cfg, repositories=[REPO], permissions={"contents": "write", "workflows": "write"})["token"]
    helper = '!f() { test "$1" = get || exit 0; echo username=x-access-token; echo "password=$VFM_APP_TOKEN"; }; f'
    env = dict(os.environ, VFM_APP_TOKEN=tok, GIT_TERMINAL_PROMPT="0")
    env.pop("GIT_ASKPASS", None)
    # credential.helper を空にして既存の helper(osxkeychain など)を外す: App のトークンを保存させない
    cmd = ["git", "-c", "credential.helper=", "-c", f"credential.helper={helper}", "push", *args.git_args]
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


def cmd_pages(args) -> int:
    cfg = load_cfg()
    tok = installation_token(cfg, repositories=[REPO], permissions={"pages": "write", "administration": "write"})["token"]
    st, j = http("POST", f"{API}/repos/{ORG}/{REPO}/pages", token=tok, body={"build_type": "workflow"})
    if st not in (201, 409):
        print(f"Pages を有効にできませんでした: HTTP {st} {_msg(j)}")
        return 1
    if st == 409:
        http("PUT", f"{API}/repos/{ORG}/{REPO}/pages", token=tok, body={"build_type": "workflow"})
    st, j = http("GET", f"{API}/repos/{ORG}/{REPO}/pages", token=tok)
    print(json.dumps({k: (j or {}).get(k) for k in ("html_url", "build_type", "status", "https_enforced")}, ensure_ascii=False))
    return 0 if st == 200 else 1


def cmd_dispatch(args) -> int:
    cfg = load_cfg()
    tok = installation_token(cfg, repositories=[REPO], permissions={"actions": "write"})["token"]
    inputs = dict(kv.split("=", 1) for kv in (args.input or []))
    st, j = http("POST", f"{API}/repos/{ORG}/{REPO}/actions/workflows/{args.workflow}/dispatches", token=tok,
                 body={"ref": args.ref, "inputs": inputs})
    print(f"dispatch {args.workflow}: HTTP {st} {_msg(j)}")
    return 0 if st in (200, 204) else 1


def cmd_runs(args) -> int:
    st, j = http("GET", f"{API}/repos/{ORG}/{REPO}/actions/runs?per_page={args.limit}")
    if st != 200:
        print(f"HTTP {st} {_msg(j)}")
        return 1
    for r in j.get("workflow_runs", []):
        print(f"{r['id']}  {r['name']:<10} {r['event']:<17} {r['status']:<10} {str(r['conclusion']):<9} "
              f"actor={(r.get('actor') or {}).get('login')} trig={(r.get('triggering_actor') or {}).get('login')} "
              f"created={r['created_at']} started={r.get('run_started_at')}")
    return 0


def cmd_pkcs8(args) -> int:
    if sys.stdout.isatty():
        print("パイプ専用です(例: python3 scripts/gh_app.py pkcs8 | npx wrangler@4 secret put GITHUB_APP_PRIVATE_KEY)", file=sys.stderr)
        return 2
    sys.stdout.write(to_pkcs8_pem(load_private_key_pem(load_cfg())))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gh_app")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--name", default=DEFAULT_NAME)
    c.add_argument("--timeout", type=int, default=MAX_WAIT)
    c.add_argument("--force", action="store_true")
    c.add_argument("--no-open", action="store_true")
    c.add_argument("--port", type=int, default=0)
    c.add_argument("--state")
    c.set_defaults(fn=cmd_create)
    w = sub.add_parser("wait-install")
    w.add_argument("--timeout", type=int, default=MAX_WAIT)
    w.set_defaults(fn=cmd_wait_install)
    sub.add_parser("bot").set_defaults(fn=cmd_bot)
    k = sub.add_parser("check")
    k.add_argument("--repo", action="store_true", help="このリポ限定・actions: write のトークンで確認")
    k.set_defaults(fn=cmd_check)
    sub.add_parser("create-repo").set_defaults(fn=cmd_create_repo)
    p = sub.add_parser("push")
    p.add_argument("git_args", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_push)
    sub.add_parser("pages").set_defaults(fn=cmd_pages)
    d = sub.add_parser("dispatch")
    d.add_argument("workflow")
    d.add_argument("--ref", default="main")
    d.add_argument("--input", action="append")
    d.set_defaults(fn=cmd_dispatch)
    r = sub.add_parser("runs")
    r.add_argument("--limit", type=int, default=10)
    r.set_defaults(fn=cmd_runs)
    sub.add_parser("pkcs8").set_defaults(fn=cmd_pkcs8)
    args = ap.parse_args(argv)
    if getattr(args, "git_args", None) and args.git_args[:1] == ["--"]:
        args.git_args = args.git_args[1:]
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
