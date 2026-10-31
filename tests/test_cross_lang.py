"""tests/fixtures/cross_lang.json(JS のテストが使う Python の答え)が、今の Python の計算と一致していること。
一致しなければ python3 tests/fixtures/make_cross_lang.py で作り直す(その後 JS のテストも通ること)。"""
import importlib.util
import json
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures"


def test_cross_lang_golden_is_up_to_date():
    spec = importlib.util.spec_from_file_location("make_cross_lang", FIX / "make_cross_lang.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    committed = json.loads((FIX / "cross_lang.json").read_text(encoding="utf-8"))
    assert json.loads(json.dumps(mod.build(), ensure_ascii=False)) == committed


def test_cross_lang_golden_covers_the_edge_cases():
    committed = json.loads((FIX / "cross_lang.json").read_text(encoding="utf-8"))
    cases = {c["name"]: c for c in committed["cases"]}
    assert len(cases) >= 8
    assert all(v is None for v in cases["すべて欠測"]["expected"]["cum"].values())
    assert cases["4h・2 日だけ(※)・途中に欠測"]["expected"]["cum"]["7"]["partial"] is True
    assert cases["8h・400 日(1 年の列が埋まる)"]["expected"]["cum"]["365"]["partial"] is False
    assert any(len(p) == 4 for p in cases["支払間隔の変更 8h→4h(古い支払いに間隔が付く)"]["payments"])
    edge = cases["now が支払時刻ちょうど(境目の支払いの扱い)"]
    now = edge["now_ms"] // 1000
    assert any(p[0] == now for p in edge["payments"]) and any(p[0] == now - 7 * 86400 for p in edge["payments"])
    assert edge["expected"]["cum"]["7"]["n"] + edge["expected"]["cum"]["7"]["n_missing"] == 21   # (now − 7 日, now] の 8h 支払いはちょうど 21 回
