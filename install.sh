#!/usr/bin/env bash
# claudecode-statusbar installer.
# - Installs statusline.py to ~/.claude/claudecode-statusbar.py
# - Patches ~/.claude/settings.json to invoke it as a status line command
# - Backs up an existing settings.json to settings.json.bak
#
# Honors $HOME so it can be exercised in a sandbox during tests.
# Override the source URL by exporting CLAUDECODE_STATUSBAR_SRC_URL.
# Override the pinned version (tag, branch, or commit) with CLAUDECODE_STATUSBAR_REF.

set -euo pipefail

CLAUDECODE_STATUSBAR_REF="${CLAUDECODE_STATUSBAR_REF:-v1.0.5}"
SRC_URL="${CLAUDECODE_STATUSBAR_SRC_URL:-https://raw.githubusercontent.com/moonbox3/claudecode-statusbar/${CLAUDECODE_STATUSBAR_REF}/statusline.py}"
DEST_DIR="${HOME}/.claude"
DEST_SCRIPT="${DEST_DIR}/claudecode-statusbar.py"
SETTINGS="${DEST_DIR}/settings.json"
BACKUP="${DEST_DIR}/settings.json.bak"

mkdir -p "${DEST_DIR}"

# Source the script: prefer a local copy if the installer sits next to one
# (e.g. when run from a git checkout), otherwise download. When piped into
# bash (curl ... | bash), BASH_SOURCE is unset — set -u would trip on it —
# so skip the local-copy probe and download.
LOCAL_SRC=""
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    LOCAL_SRC="${SCRIPT_DIR}/statusline.py"
fi

if [[ -n "${LOCAL_SRC}" && -f "${LOCAL_SRC}" ]]; then
    cp "${LOCAL_SRC}" "${DEST_SCRIPT}"
else
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "${SRC_URL}" -o "${DEST_SCRIPT}"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "${DEST_SCRIPT}" "${SRC_URL}"
    else
        echo "claudecode-statusbar: need curl or wget to download ${SRC_URL}" >&2
        exit 1
    fi
fi

chmod +x "${DEST_SCRIPT}"

# Patch settings.json via Python (Python is guaranteed; jq is not).
SETTINGS_PATH="${SETTINGS}" BACKUP_PATH="${BACKUP}" SCRIPT_PATH="${DEST_SCRIPT}" python3 - <<'PY'
import json, os, tempfile

settings_path = os.environ["SETTINGS_PATH"]
backup_path = os.environ["BACKUP_PATH"]
script_path = os.environ["SCRIPT_PATH"]

if os.path.exists(settings_path):
    with open(settings_path, "r") as f:
        raw = f.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        # Preserve the unparseable file as-is in the backup, start fresh.
        data = {}
    # Always back up the existing file before mutating.
    with open(backup_path, "w") as f:
        f.write(raw)
else:
    data = {}

if not isinstance(data, dict):
    data = {}

status_line = data.get("statusLine")
if not isinstance(status_line, dict):
    status_line = {}
status_line["type"] = "command"
status_line["command"] = script_path
data["statusLine"] = status_line

# Atomic write: tmp file in same dir + os.replace.
dir_ = os.path.dirname(settings_path) or "."
fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".json", dir=dir_)
try:
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, settings_path)
except Exception:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
PY

echo "claudecode-statusbar: installed ${DEST_SCRIPT}"
echo "claudecode-statusbar: patched ${SETTINGS}"
echo "claudecode-statusbar: restart Claude Code to see the new status line."
