"""時刻と年率換算の道具。内部はすべて UTC の aware datetime。"""
from __future__ import annotations

import datetime as _dt

UTC = _dt.timezone.utc
JST = _dt.timezone(_dt.timedelta(hours=9), "JST")
ISO = "%Y-%m-%dT%H:%M:%SZ"
SECONDS_PER_YEAR = 365 * 86400


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(UTC).replace(microsecond=0)


def fmt_iso(d: _dt.datetime) -> str:
    """aware datetime → '2026-10-25T00:08:12Z'(秒まで)。"""
    return d.astimezone(UTC).strftime(ISO)


def parse_iso(s: str) -> _dt.datetime:
    """'2026-10-25T00:08:12Z' または '...+00:00' 形式 → aware datetime(UTC)。"""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = _dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def from_unix(ts: int | float) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(int(ts), UTC)


def to_unix(d: _dt.datetime) -> int:
    return int(d.timestamp())


def to_jst(d: _dt.datetime) -> _dt.datetime:
    return d.astimezone(JST)


def fmt_jst(d: _dt.datetime) -> str:
    """報告用: '2026-10-25 09:08 JST'。"""
    return to_jst(d).strftime("%Y-%m-%d %H:%M JST")


def fmt_both(d: _dt.datetime) -> str:
    """報告用: UTC と JST の併記。"""
    return f"{d.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC')} ({to_jst(d).strftime('%H:%M JST')})"


def day_str(d: _dt.datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%d")


def month_str(d: _dt.datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m")


def floor_hour(d: _dt.datetime) -> _dt.datetime:
    return d.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def periods_per_year(interval_seconds: int) -> float:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    return SECONDS_PER_YEAR / interval_seconds


def annualize(rate: float, interval_seconds: int) -> float:
    """1 回分の FR → 年率(小数)。4h なら ×2190、8h なら ×1095、1h なら ×8760。"""
    return rate * periods_per_year(interval_seconds)


# 金利成分: 0.00125%/時(8 時間で 0.01%、年 10.95%)
INTEREST_PER_HOUR = 0.0000125


def interest_component(interval_seconds: int) -> float:
    return INTEREST_PER_HOUR * interval_seconds / 3600


INTEREST_APR = annualize(interest_component(28800), 28800)  # = 0.1095
