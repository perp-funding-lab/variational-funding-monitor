"""Variational の stats を 1 回取得する。タイムアウト 20 秒、429 は retry_after_seconds を待って最大 3 回。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

STATS_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats"
# Python 標準の User-Agent(Python-urllib/x.y)は 403 で弾かれるので固有の名前を付ける
USER_AGENT = "variational-funding-monitor/1.0"
TIMEOUT = 20
MAX_RETRIES = 3
MAX_RETRY_WAIT = 60.0


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None, attempts: int = 0):
        super().__init__(message)
        self.status = status
        self.attempts = attempts


@dataclass
class FetchResult:
    status: int
    body: bytes
    attempts: int
    waited_seconds: float


def _default_open(url: str, timeout: int) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def retry_after_from_body(body: bytes, default: float = 10.0) -> float:
    try:
        v = json.loads(body).get("retry_after_seconds", default)
        v = float(v)
        return v if v >= 0 else default
    except Exception:
        return default


def fetch_stats(url: str = STATS_URL, *, timeout: int = TIMEOUT, max_retries: int = MAX_RETRIES,
                opener: Callable[[str, int], tuple[int, bytes]] = _default_open,
                sleep: Callable[[float], None] = time.sleep) -> FetchResult:
    """429 のときだけ再試行する(最大 max_retries 回、合計 1 + max_retries 回の試行)。
    それ以外の失敗は FetchError。ネットワーク例外も FetchError にまとめる。"""
    attempts = 0
    waited = 0.0
    last_status: int | None = None
    while True:
        attempts += 1
        try:
            status, body = opener(url, timeout)
        except Exception as e:  # URLError, timeout, ConnectionReset など
            raise FetchError(f"network error: {e!r}", None, attempts) from e
        last_status = status
        if status == 200:
            return FetchResult(status, body, attempts, waited)
        if status == 429 and attempts <= max_retries:
            wait = min(retry_after_from_body(body), MAX_RETRY_WAIT)
            waited += wait
            sleep(wait)
            continue
        raise FetchError(f"HTTP {status}", status, attempts)
