"""fetch: 429 の再試行、その他の失敗、User-Agent、retry_after の読み取り。ネットワークは使わない。"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from vfm import fetch as fx
from vfm.fetch import FetchError, FetchResult, fetch_stats, retry_after_from_body

BODY_429 = json.dumps({"retry_after_seconds": 3}).encode()


class FakeOpener:
    """用意した応答を順に返す(例外なら投げる)。呼び出しを記録する。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _sleeper():
    calls: list[float] = []
    return calls, calls.append


def test_200_on_first_try_has_one_attempt_and_no_sleep():
    op = FakeOpener([(200, b'{"ok":1}')])
    sleeps, sleep = _sleeper()
    res = fetch_stats("http://x", opener=op, sleep=sleep)
    assert isinstance(res, FetchResult)
    assert (res.status, res.body, res.attempts, res.waited_seconds) == (200, b'{"ok":1}', 1, 0.0)
    assert sleeps == []
    assert op.calls == [("http://x", fx.TIMEOUT)]


def test_429_twice_then_200_sleeps_retry_after_each_time():
    op = FakeOpener([(429, BODY_429), (429, BODY_429), (200, b"ok")])
    sleeps, sleep = _sleeper()
    res = fetch_stats("http://x", opener=op, sleep=sleep)
    assert res.status == 200 and res.body == b"ok"
    assert res.attempts == 3
    assert sleeps == [3.0, 3.0]
    assert res.waited_seconds == 6.0


def test_429_four_times_gives_up_with_status_and_attempts():
    op = FakeOpener([(429, BODY_429)] * 4)
    sleeps, sleep = _sleeper()
    with pytest.raises(FetchError) as ei:
        fetch_stats("http://x", opener=op, sleep=sleep, max_retries=3)
    assert ei.value.status == 429
    assert ei.value.attempts == 4           # max_retries=3 → 合計 4 回
    assert len(op.calls) == 4
    assert sleeps == [3.0, 3.0, 3.0]
    assert "HTTP 429" in str(ei.value)


def test_max_retries_zero_means_single_attempt():
    op = FakeOpener([(429, BODY_429), (200, b"ok")])
    sleeps, sleep = _sleeper()
    with pytest.raises(FetchError) as ei:
        fetch_stats("http://x", opener=op, sleep=sleep, max_retries=0)
    assert ei.value.attempts == 1 and sleeps == []


def test_500_fails_immediately_without_retry():
    op = FakeOpener([(500, b"boom"), (200, b"never")])
    sleeps, sleep = _sleeper()
    with pytest.raises(FetchError) as ei:
        fetch_stats("http://x", opener=op, sleep=sleep)
    assert ei.value.status == 500 and ei.value.attempts == 1
    assert sleeps == [] and len(op.calls) == 1
    assert str(ei.value) == "HTTP 500"


def test_403_fails_immediately_without_retry():
    op = FakeOpener([(403, b"forbidden")])
    with pytest.raises(FetchError) as ei:
        fetch_stats("http://x", opener=op, sleep=lambda s: None)
    assert ei.value.status == 403 and ei.value.attempts == 1


def test_opener_exception_becomes_fetch_error_with_cause():
    op = FakeOpener([ConnectionResetError("reset by peer")])
    sleeps, sleep = _sleeper()
    with pytest.raises(FetchError) as ei:
        fetch_stats("http://x", opener=op, sleep=sleep)
    assert ei.value.status is None and ei.value.attempts == 1
    assert "network error" in str(ei.value)
    assert isinstance(ei.value.__cause__, ConnectionResetError)
    assert sleeps == []


def test_exception_after_a_429_reports_attempts_so_far():
    op = FakeOpener([(429, BODY_429), TimeoutError("timed out")])
    with pytest.raises(FetchError) as ei:
        fetch_stats("http://x", opener=op, sleep=lambda s: None)
    assert ei.value.attempts == 2 and ei.value.status is None


def test_user_agent_is_not_pythons_default():
    assert not fx.USER_AGENT.lower().startswith("python-urllib")
    assert "variational-funding-monitor" in fx.USER_AGENT


def test_default_open_sends_custom_user_agent_and_accept(monkeypatch):
    seen = {}

    class Resp:
        status = 200

        def read(self):
            return b"body"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen["req"], seen["timeout"] = req, timeout
        return Resp()

    monkeypatch.setattr(fx.urllib.request, "urlopen", fake_urlopen)
    assert fx._default_open("http://x/stats", 7) == (200, b"body")
    req = seen["req"]
    assert req.full_url == "http://x/stats"
    assert req.get_header("User-agent") == fx.USER_AGENT
    assert req.get_header("Accept") == "application/json"
    assert seen["timeout"] == 7


def test_default_open_returns_http_error_status_and_body(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b'{"retry_after_seconds": 2}'))

    monkeypatch.setattr(fx.urllib.request, "urlopen", fake_urlopen)
    assert fx._default_open("http://x", 5) == (429, b'{"retry_after_seconds": 2}')


@pytest.mark.parametrize("body,expected", [
    (b'{"retry_after_seconds": 3}', 3.0),
    (b'{"retry_after_seconds": "2.5"}', 2.5),
    (b'{"retry_after_seconds": 0}', 0.0),
    (b"garbage", 10.0),
    (b"", 10.0),
    (b"{}", 10.0),
    (b"[1, 2]", 10.0),
    (b'{"retry_after_seconds": null}', 10.0),
    (b'{"retry_after_seconds": "abc"}', 10.0),
    (b'{"retry_after_seconds": -5}', 10.0),
])
def test_retry_after_from_body(body, expected):
    assert retry_after_from_body(body) == expected


def test_retry_after_from_body_custom_default():
    assert retry_after_from_body(b"garbage", default=4.0) == 4.0
    assert retry_after_from_body(b'{"retry_after_seconds": -1}', default=4.0) == 4.0


def test_fetch_stats_caps_retry_wait_at_max_retry_wait():
    assert fx.MAX_RETRY_WAIT == 60.0
    op = FakeOpener([(429, b'{"retry_after_seconds": 1000}'), (200, b"ok")])
    sleeps, sleep = _sleeper()
    res = fetch_stats("http://x", opener=op, sleep=sleep)
    assert sleeps == [60.0]
    assert res.waited_seconds == 60.0


def test_fetch_stats_uses_default_wait_for_garbage_429_body():
    op = FakeOpener([(429, b"<html>slow down</html>"), (200, b"ok")])
    sleeps, sleep = _sleeper()
    fetch_stats("http://x", opener=op, sleep=sleep)
    assert sleeps == [10.0]


def test_module_constants():
    assert fx.STATS_URL.startswith("https://") and fx.STATS_URL.endswith("/metadata/v2/stats")
    assert fx.TIMEOUT == 20 and fx.MAX_RETRIES == 3
