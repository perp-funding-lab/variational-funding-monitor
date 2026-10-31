"""scripts/gh_app.py(GitHub App の道具)の署名・鍵の変換と、Worker の JWT との一致。鍵はテスト中に使い捨てで作る。"""
import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gh_app", ROOT / "scripts" / "gh_app.py")
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)
OPENSSL, NODE = shutil.which("openssl"), shutil.which("node")


@pytest.fixture(scope="module")
def key(tmp_path_factory):
    if not OPENSSL:
        pytest.skip("openssl がない")
    d = tmp_path_factory.mktemp("key")
    subprocess.run([OPENSSL, "genrsa", "-out", str(d / "k.pem"), "2048"], check=True, capture_output=True)
    return d, (d / "k.pem").read_text()


def test_rsa_signature_is_identical_to_openssl(key):
    d, pem = key
    (d / "m.txt").write_bytes(b"header.payload")
    subprocess.run([OPENSSL, "dgst", "-sha256", "-sign", str(d / "k.pem"), "-out", str(d / "s.bin"), str(d / "m.txt")], check=True)
    assert g.rsa_sign_sha256(g.rsa_key(pem), b"header.payload") == (d / "s.bin").read_bytes()


def test_pkcs8_conversion_is_identical_to_openssl(key):
    d, pem = key
    ref = subprocess.run([OPENSSL, "pkcs8", "-topk8", "-nocrypt", "-in", str(d / "k.pem")], check=True, capture_output=True, text=True).stdout
    mine = g.to_pkcs8_pem(pem)
    assert g.pem_to_der(mine)[1] == g.pem_to_der(ref)[1]
    assert g.rsa_key(mine) == g.rsa_key(pem)
    assert mine.startswith("-----BEGIN PRIVATE KEY-----\n") and all(len(x) <= 64 for x in mine.splitlines())


def test_python_jwt_equals_worker_jwt(key):
    if not NODE:
        pytest.skip("node がない")
    _, pem = key
    mod = (ROOT / "cloudflare-worker" / "src" / "github_app.js").as_uri()
    code = (f"import {{appJwt}} from '{mod}'; let s = ''; process.stdin.on('data', c => s += c)"
            ".on('end', async () => process.stdout.write(await appJwt(s, 5553022, 1793434080)));")
    out = subprocess.run([NODE, "--input-type=module", "-e", code], input=g.to_pkcs8_pem(pem), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout == g.make_jwt(pem, 5553022, now=1793434080)


def test_push_passes_token_by_env_only_and_resets_credential_helpers(monkeypatch):
    seen = {}
    monkeypatch.setattr(g, "load_cfg", lambda: {"installation_id": 1})
    monkeypatch.setattr(g, "installation_token", lambda cfg, **kw: {"token": "ghs_SECRET"})

    class R:
        returncode = 0

    def fake_run(cmd, cwd=None, env=None):
        seen.update(cmd=cmd, env=env)
        return R()

    monkeypatch.setattr(g.subprocess, "run", fake_run)
    assert g.cmd_push(argparse.Namespace(git_args=["-u", "origin", "main"])) == 0
    assert all("ghs_SECRET" not in c for c in seen["cmd"])
    assert seen["cmd"][:3] == ["git", "-c", "credential.helper="] and seen["cmd"][-3:] == ["-u", "origin", "main"]
    assert seen["env"]["VFM_APP_TOKEN"] == "ghs_SECRET" and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_pkcs8_command_refuses_a_terminal(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert g.cmd_pkcs8(argparse.Namespace()) == 2


def test_keychain_store_rejects_values_that_could_break_the_security_command():
    for bad in ("", "a b", 'a"b', "a\nb"):
        with pytest.raises(ValueError):
            g.keychain_store("svc", "acct", bad)
