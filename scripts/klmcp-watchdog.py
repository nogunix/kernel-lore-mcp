#!/usr/bin/env python3
"""klmcp-watchdog — detect the silent-failure modes this deployment hit.

Both production incidents here were silent: the unit exited 0, /status
reported every tier "in sync", and nothing in the logs said mail had
stopped arriving.

  * The incremental fetch transferred nothing for 11 hours while the
    fingerprint cache advanced, so the corpus froze mid-air.
  * `rebuild_bm25` appended a full copy of the corpus on every daily
    reindex until meta.json claimed 46.4M docs across 85 GB, and
    lore_search returned each message up to six times.

Every check below is anchored to one of those, plus the unit-level
failures that would mask them. Nothing speculative — this is not a
general-purpose monitor.

Exit 0 when no FAIL. Exit 1 on any FAIL, so systemd marks the unit
failed and `systemctl --user --failed` surfaces it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

STATUS_URL = os.environ.get("KLMCP_STATUS_URL", "http://127.0.0.1:8099/status")
DATA_DIR = Path(os.environ.get("KLMCP_DATA_DIR", "/mnt/hdd/klmcp-data"))
STATE_DIR = Path(
    os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
) / "klmcp-watchdog"
STATE_FILE = STATE_DIR / "last.json"

# One reindex adds ~0.03% to the corpus. A duplicated rebuild adds 100%.
# 25% is far above organic growth and far below the failure signature.
BM25_GROWTH_FAIL_PCT = 25.0
GENERATION_STALL_WARN_SECONDS = 3600

fails: list[str] = []
warns: list[str] = []


def ok(msg: str) -> None:
    print(f"[watchdog] PASS: {msg}")


def warn(msg: str) -> None:
    print(f"[watchdog] WARN: {msg}")
    warns.append(msg)


def fail(msg: str) -> None:
    print(f"[watchdog] FAIL: {msg}")
    fails.append(msg)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def fetch_status() -> dict | None:
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=10) as resp:
            return json.load(resp)
    except Exception as exc:  # noqa: BLE001 - any failure is a FAIL
        fail(f"/status unreachable at {STATUS_URL}: {exc}")
        return None


def bm25_live_docs() -> int | None:
    meta = DATA_DIR / "bm25" / "meta.json"
    try:
        segments = json.loads(meta.read_text()).get("segments", [])
    except (OSError, ValueError) as exc:
        fail(f"cannot read {meta}: {exc}")
        return None
    live = 0
    for seg in segments:
        deletes = seg.get("deletes") or {}
        live += seg.get("max_doc", 0) - deletes.get("num_deleted_docs", 0)
    return live


def check_freshness(status: dict) -> None:
    age = status.get("last_ingest_age_seconds")
    interval = status.get("configured_interval_seconds") or 300
    budget = max(3 * interval, 900)
    if not status.get("freshness_ok", False):
        fail(f"/status reports freshness_ok=false (last ingest {age}s ago)")
    elif age is None:
        fail("/status has no last_ingest_age_seconds")
    elif age > budget:
        fail(f"last ingest {age}s ago, over the {budget}s budget — corpus may be frozen")
    else:
        ok(f"freshness {age}s (budget {budget}s)")


def check_generation(status: dict, state: dict) -> int | None:
    gen = status.get("generation")
    prev_gen = state.get("generation")
    prev_ts = state.get("ts")
    if gen is None:
        fail("/status has no generation")
        return None
    if prev_gen is None or prev_ts is None:
        print(f"[watchdog] INFO: first run, recording generation {gen}")
        return gen
    stalled_for = time.time() - prev_ts
    if gen > prev_gen:
        ok(f"generation advanced {prev_gen} -> {gen}")
    elif stalled_for > GENERATION_STALL_WARN_SECONDS:
        warn(
            f"generation stuck at {gen} for {int(stalled_for)}s — "
            "the signature of a fetch that transfers nothing"
        )
    else:
        ok(f"generation {gen} unchanged, within the {GENERATION_STALL_WARN_SECONDS}s window")
    return gen


def check_bm25(state: dict) -> int | None:
    live = bm25_live_docs()
    if live is None:
        return None
    prev = state.get("bm25_live_docs")
    if prev is None:
        print(f"[watchdog] INFO: first run, recording bm25 live docs {live:,}")
        return live
    if prev <= 0:
        return live
    growth = (live - prev) / prev * 100.0
    if growth >= BM25_GROWTH_FAIL_PCT:
        fail(
            f"bm25 live docs {prev:,} -> {live:,} (+{growth:.1f}%) — "
            "rebuild is appending copies again"
        )
    elif live < prev * 0.5:
        warn(f"bm25 live docs dropped {prev:,} -> {live:,}; expected after a deliberate rebuild")
    else:
        ok(f"bm25 live docs {live:,} ({growth:+.2f}%)")
    return live


def check_fetch_warnings(state: dict) -> None:
    since = state.get("ts")
    since_arg = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))
        if since
        else "-1h"
    )
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", "klmcp-sync.service",
             "--since", since_arg, "-o", "cat", "--no-pager"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"cannot read the sync journal: {exc}")
        return
    hits = out.count("no ref mappings")
    if hits:
        fail(f"{hits} 'fetch produced no ref mappings' warnings since {since_arg}")
    else:
        ok(f"no 'no ref mappings' warnings since {since_arg}")


def check_units() -> None:
    failed = []
    for unit in ("klmcp.service", "klmcp-sync.service", "klmcp-reindex.service"):
        state = subprocess.run(
            ["systemctl", "--user", "is-failed", unit],
            capture_output=True, text=True,
        ).stdout.strip()
        if state == "failed":
            failed.append(unit)
    if failed:
        fail(f"units in failed state: {', '.join(failed)}")
    else:
        ok("klmcp units are not in failed state")


def main() -> int:
    state = load_state()
    status = fetch_status()

    new_state = {"ts": time.time()}
    if status is not None:
        check_freshness(status)
        gen = check_generation(status, state)
        if gen is not None:
            new_state["generation"] = gen
    else:
        new_state["generation"] = state.get("generation")

    live = check_bm25(state)
    if live is not None:
        new_state["bm25_live_docs"] = live

    check_fetch_warnings(state)
    check_units()
    save_state(new_state)

    print(f"[watchdog] {len(fails)} fail, {len(warns)} warn")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
