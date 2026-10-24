"""CSV/JSON の読み書きと保持期間の管理。追記のみ。"""
from __future__ import annotations

import csv
import datetime as _dt
import gzip
import io
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .timeutil import UTC, day_str, fmt_iso, month_str, parse_iso

RUN_HEADER = ["scheduled_ts", "fetch_ts", "delay_s", "http_status", "n_listings", "n_perp", "n_swap",
              "n_obs_rows", "n_funding_rows", "n_anomalies", "trigger", "note"]
OBS_HEADER = ["obs_ts", "ticker", "interval_seconds", "next_timestamp", "interval_rate", "annualized_rate", "kind"]
FUNDING_HEADER = ["pay_ts", "ticker", "interval_seconds", "interval_rate", "annualized_rate", "status", "basis_obs_ts"]
SNAP_HEADER = ["snap_ts", "ticker", "contract_type", "mark_price", "long_oi", "short_oi", "volume_24h", "base_spread_bps"]

OBS_KEEP_DAYS = 30


@dataclass
class DataPaths:
    root: Path

    def __post_init__(self):
        self.root = Path(self.root)

    @property
    def runs_dir(self) -> Path: return self.root / "runs"
    @property
    def obs_dir(self) -> Path: return self.root / "obs"
    @property
    def funding_dir(self) -> Path: return self.root / "funding"
    @property
    def snap_dir(self) -> Path: return self.root / "snapshots"
    @property
    def archive_dir(self) -> Path: return self.root / "archive"
    @property
    def hl_dir(self) -> Path: return self.root / "hl"
    @property
    def meta_path(self) -> Path: return self.root / "meta.json"
    @property
    def swaps_path(self) -> Path: return self.root / "swaps_latest.json"
    @property
    def hl_path(self) -> Path: return self.hl_dir / "hl_7d.json"

    def run_file(self, d: _dt.datetime) -> Path: return self.runs_dir / f"{month_str(d)}.csv"
    def obs_file(self, d: _dt.datetime) -> Path: return self.obs_dir / f"{day_str(d)}.csv"
    def funding_file(self, d: _dt.datetime) -> Path: return self.funding_dir / f"{month_str(d)}.csv"
    def snap_file(self, d: _dt.datetime) -> Path: return self.snap_dir / f"{day_str(d)}.csv"

    def ensure(self) -> None:
        for p in (self.runs_dir, self.obs_dir, self.funding_dir, self.snap_dir, self.archive_dir / "snapshots", self.hl_dir):
            p.mkdir(parents=True, exist_ok=True)


def append_csv(path: Path, header: list[str], rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, lineterminator="\n", extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in header})
    return len(rows)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def iter_csv_files(d: Path, pattern: str = r"^\d{4}-\d{2}(-\d{2})?\.csv(\.gz)?$", newest_first: bool = False) -> list[Path]:
    if not d.exists():
        return []
    files = sorted(p for p in d.iterdir() if re.match(pattern, p.name))
    return files[::-1] if newest_first else files


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, obj, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def load_last_obs(paths: DataPaths, max_files: int = OBS_KEEP_DAYS + 1) -> dict[str, dict]:
    """銘柄ごとの最新の観測行(obs_ts が最大のもの)。新しいファイルから順に読む。"""
    last: dict[str, dict] = {}
    for p in iter_csv_files(paths.obs_dir, newest_first=True)[:max_files]:
        for r in read_csv(p):
            t = r["ticker"]
            cur = last.get(t)
            if cur is None or r["obs_ts"] > cur["obs_ts"]:
                last[t] = r
    return last


def load_last_pay(paths: DataPaths, max_files: int = 2) -> dict[str, int]:
    """銘柄ごとの最後に記録した pay_ts(missing を含む)。直近 2 か月分だけ見る。"""
    out: dict[str, int] = {}
    for p in iter_csv_files(paths.funding_dir, newest_first=True)[:max_files]:
        for r in read_csv(p):
            v = int(r["pay_ts"])
            if v > out.get(r["ticker"], -1):
                out[r["ticker"]] = v
    return out


def load_all_funding(paths: DataPaths) -> list[dict]:
    rows: list[dict] = []
    for p in iter_csv_files(paths.funding_dir):
        rows.extend(read_csv(p))
    return rows


def prune_obs(paths: DataPaths, now: _dt.datetime, keep_days: int = OBS_KEEP_DAYS) -> list[Path]:
    """keep_days より古い日の観測ログを削除する(git 履歴には残る)。"""
    cutoff = day_str(now - _dt.timedelta(days=keep_days))
    removed = []
    for p in iter_csv_files(paths.obs_dir):
        if p.name[:10] < cutoff:
            p.unlink()
            removed.append(p)
    return removed


def archive_snapshots(paths: DataPaths, now: _dt.datetime) -> list[Path]:
    """先月以前の日次スナップショットを月ごとの .csv.gz にまとめ、日次ファイルを消す。"""
    cur = month_str(now)
    by_month: dict[str, list[Path]] = {}
    for p in iter_csv_files(paths.snap_dir, pattern=r"^\d{4}-\d{2}-\d{2}\.csv$"):
        m = p.name[:7]
        if m < cur:
            by_month.setdefault(m, []).append(p)
    out = []
    for m, files in sorted(by_month.items()):
        dest = paths.archive_dir / "snapshots" / f"{m}.csv.gz"
        dest.parent.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        w = None
        for p in files:
            for r in read_csv(p):
                if w is None:
                    w = csv.DictWriter(buf, fieldnames=SNAP_HEADER, lineterminator="\n")
                    w.writeheader()
                w.writerow(r)
        if dest.exists():   # 既にある月に追加(通常は起きない)
            with gzip.open(dest, "rt", encoding="utf-8") as f:
                old = f.read()
            body = old + "".join(buf.getvalue().splitlines(True)[1:])
        else:
            body = buf.getvalue()
        with gzip.open(dest, "wt", encoding="utf-8", compresslevel=9) as f:
            f.write(body)
        for p in files:
            p.unlink()
        out.append(dest)
    return out


def default_meta(now: _dt.datetime) -> dict:
    return {"collection_start": fmt_iso(now), "last_run_ts": None, "last_success_ts": None,
            "funding_mode": None, "anomalies_by_day": {}}
