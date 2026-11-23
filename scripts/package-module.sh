#!/usr/bin/env bash
# Shell entry point for the shared module validator and packager.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BINARY_PATH="${1:-target/aarch64-linux-android/release/duckd}"
DIST_DIR="${2:-dist}"
X86_BINARY_PATH="${3:-}"

VERSION="$(python3 -c 'import pathlib, sys; print(next(line.split("=", 1)[1].strip() for line in pathlib.Path(sys.argv[1]).read_text().splitlines() if line.startswith("version=")))' "${REPO_ROOT}/module/module.prop")"
args=(--binary "${BINARY_PATH}" --output "${DIST_DIR}/duck-toolbox-${VERSION}.zip")
[ -n "${X86_BINARY_PATH}" ] && args+=(--binary-x86-64 "${X86_BINARY_PATH}")
exec python3 "${REPO_ROOT}/scripts/package-module.py" "${args[@]}"
