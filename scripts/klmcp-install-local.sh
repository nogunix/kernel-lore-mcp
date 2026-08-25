#!/usr/bin/env bash
# klmcp-install-local.sh — build the writer-side Rust binaries and
# install them so the canonical `kernel-lore-{sync,reindex,doctor}`
# commands run the code in this checkout.
#
# Why this script exists: `[tool.maturin] include` packages the three
# Rust binaries into the wheel by copying them out of
# src/kernel_lore_mcp/bin/. That directory is gitignored and nothing in
# the build populates it, so `uv tool install` happily ships whatever
# was staged there last. On this box that meant a v0.4.5 install kept
# executing binaries built nine days earlier — predating both
# silent-failure fixes — while `--version` reported the new version.
# Staging is the step that gets forgotten; keep it in one command.
#
# Usage:
#   scripts/klmcp-install-local.sh          # build + stage + install
#   scripts/klmcp-install-local.sh --check  # verify staged == installed
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

bins=(kernel-lore-sync kernel-lore-reindex kernel-lore-doctor)
staged_dir="src/kernel_lore_mcp/bin"

installed_path() {
    find "$HOME/.local/share/uv/tools/kernel-lore-mcp" \
        -path "*/kernel_lore_mcp/bin/$1" -print -quit 2>/dev/null
}

if [[ "${1:-}" == "--check" ]]; then
    rc=0
    for b in "${bins[@]}"; do
        inst="$(installed_path "$b")"
        if [[ -z "$inst" ]]; then
            echo "[install] FAIL: $b is not installed"; rc=1; continue
        fi
        if cmp -s "target/release/$b" "$inst"; then
            echo "[install] PASS: $b matches target/release"
        else
            echo "[install] FAIL: $b differs from target/release — re-run without --check"
            rc=1
        fi
    done
    if cmp -s scripts/klmcp-watchdog.py "$HOME/.local/bin/klmcp-watchdog"; then
        echo "[install] PASS: klmcp-watchdog matches scripts/klmcp-watchdog.py"
    else
        echo "[install] FAIL: klmcp-watchdog differs from scripts/klmcp-watchdog.py"
        rc=1
    fi
    exit $rc
fi

echo "[install] building release binaries"
cargo build --release --bins

echo "[install] staging into $staged_dir (the step the wheel silently depends on)"
for b in "${bins[@]}"; do
    install -m 755 "target/release/$b" "$staged_dir/$b"
done

echo "[install] uv tool install --reinstall ."
uv tool install --reinstall .

echo "[install] installing the watchdog alongside them"
install -m 755 scripts/klmcp-watchdog.py "$HOME/.local/bin/klmcp-watchdog"

echo "[install] verifying installed binaries match this checkout"
exec "$0" --check
