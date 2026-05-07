#!/usr/bin/env python3
"""ccstatusbar — Claude Code status line.

Slice 01: skeleton. Reads Claude Code's JSON payload on stdin, prints a
single line with the cwd basename and model name. Later slices add git,
context %, rate-limit segments, colors, and the render budget.
"""

import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

GIT_TIMEOUT_SECONDS = 2

USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_FETCH_TIMEOUT_SECONDS = 3.0
CACHE_FRESH_SECONDS = 60
CACHE_STALE_SECONDS = 15 * 60
CACHE_FAILURE_BACKOFF_SECONDS = 5 * 60
CREDENTIALS_KEYCHAIN_SERVICE = "Claude Code-credentials"


def read_payload(stream) -> dict:
    try:
        raw = stream.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def extract_cwd_basename(payload: dict) -> str:
    workspace = payload.get("workspace") or {}
    cwd = workspace.get("current_dir") or payload.get("cwd") or os.getcwd()
    try:
        return Path(cwd).name or cwd
    except Exception:
        return ""


def extract_model_name(payload: dict) -> str:
    model = payload.get("model") or {}
    if isinstance(model, dict):
        return model.get("display_name") or model.get("id") or ""
    if isinstance(model, str):
        return model
    return ""


def parse_git_porcelain(output: str) -> dict:
    """Parse `git status --porcelain=v2 --branch` text into a dict.

    Empty output → not in a repo. The XY field on tracked entries is
    `<index><worktree>`; `.` means unchanged, so any non-`.` in the first
    position counts as staged and any non-`.` in the second as unstaged.
    """
    if not output.strip():
        return {"in_repo": False}
    branch = None
    ahead = behind = staged = unstaged = untracked = 0
    for line in output.splitlines():
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head "):].strip()
        elif line.startswith("# branch.ab "):
            for tok in line.split()[2:]:
                if tok.startswith("+"):
                    ahead = int(tok[1:])
                elif tok.startswith("-"):
                    behind = int(tok[1:])
        elif line.startswith(("1 ", "2 ")):
            xy = line.split(" ", 2)[1]
            if len(xy) >= 2:
                if xy[0] != ".":
                    staged += 1
                if xy[1] != ".":
                    unstaged += 1
        elif line.startswith("u "):
            unstaged += 1
        elif line.startswith("? "):
            untracked += 1
    if branch is None:
        return {"in_repo": False}
    return {
        "in_repo": True,
        "branch": branch,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "ahead": ahead,
        "behind": behind,
    }


def get_git_status(cwd: str = None) -> dict:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v2", "--branch"],
            capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            cwd=cwd,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired,
            subprocess.SubprocessError):
        return {"in_repo": False}
    if result.returncode != 0:
        return {"in_repo": False}
    return parse_git_porcelain(result.stdout)


def render_git_segment(g: dict) -> str:
    if not g.get("in_repo"):
        return ""
    branch = g.get("branch") or ""
    counters = []
    if g.get("staged"):
        counters.append(f"S:{g['staged']}")
    if g.get("unstaged"):
        counters.append(f"U:{g['unstaged']}")
    if g.get("untracked"):
        counters.append(f"A:{g['untracked']}")
    arrows = ""
    if g.get("ahead"):
        arrows += f"↑{g['ahead']}"
    if g.get("behind"):
        arrows += f"↓{g['behind']}"
    seg = branch
    if counters:
        seg = f"{branch} | {' '.join(counters)}"
    if arrows:
        seg = f"{seg} {arrows}"
    return seg


def parse_transcript(path) -> dict:
    """Read the session JSONL and return token info from the last assistant
    message that carries usage counters. Returns None on any failure.
    """
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except (OSError, ValueError):
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict) or d.get("type") != "assistant":
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        input_tokens = usage.get("input_tokens") or 0
        cache_read = usage.get("cache_read_input_tokens") or 0
        cache_creation = usage.get("cache_creation_input_tokens") or 0
        if not (input_tokens or cache_read or cache_creation):
            continue
        try:
            return {
                "input_tokens": int(input_tokens),
                "cache_read_tokens": int(cache_read),
                "cache_creation_tokens": int(cache_creation),
                "model_id": msg.get("model") or "",
            }
        except (TypeError, ValueError):
            continue
    return None


def context_limit(model_id: str) -> int:
    if model_id and "[1m]" in model_id.lower():
        return 1_000_000
    return 200_000


def render_ctx_segment(info: dict) -> str:
    if not info:
        return ""
    used = (info.get("input_tokens", 0)
            + info.get("cache_read_tokens", 0)
            + info.get("cache_creation_tokens", 0))
    if used <= 0:
        return ""
    limit = context_limit(info.get("model_id") or "")
    pct = round(used * 100 / limit)
    used_k = used // 1000
    limit_k = limit // 1000
    return f"ctx:{pct}%({used_k}k/{limit_k}k)"


def _read_keychain_credentials() -> dict:
    if sys.platform != "darwin":
        return None
    user = os.environ.get("USER") or ""
    if not user:
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", CREDENTIALS_KEYCHAIN_SERVICE,
             "-a", user, "-w"],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired,
            subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = (result.stdout or "").strip()
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return d if isinstance(d, dict) else None


def _read_file_credentials() -> dict:
    path = Path.home() / ".claude" / ".credentials.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _extract_oauth(creds) -> dict:
    if not isinstance(creds, dict):
        return None
    nested = creds.get("claudeAiOauth")
    if isinstance(nested, dict) and "accessToken" in nested:
        return nested
    if "accessToken" in creds:
        return creds
    return None


def get_access_token(now_ms=None):
    """Resolve the OAuth access token. Returns None if missing/expired.

    Slice 04 is read-only — expired tokens are dropped silently. Slice 05
    will refresh them.
    """
    creds = _read_keychain_credentials()
    if creds is None:
        creds = _read_file_credentials()
    oauth = _extract_oauth(creds)
    if not oauth:
        return None
    token = oauth.get("accessToken")
    if not token or not isinstance(token, str):
        return None
    expires_at = oauth.get("expiresAt")
    if expires_at is not None:
        try:
            expires_at = int(expires_at)
        except (TypeError, ValueError):
            return None
        now_v = now_ms if now_ms is not None else int(time.time() * 1000)
        if now_v >= expires_at:
            return None
    return token


def _normalize_usage(raw) -> dict:
    """Map several possible API shapes to a flat dict.

    Output keys (each optional): five_hour_pct, five_hour_resets_at,
    weekly_pct, weekly_resets_at. Percentages are normalized to a 0-100
    scale (fractional input <=1 is multiplied by 100).
    """
    if not isinstance(raw, dict):
        return {}

    def pick_pct(d, *keys):
        if not isinstance(d, dict):
            return None
        for k in keys:
            if k in d and d[k] is not None:
                try:
                    f = float(d[k])
                except (TypeError, ValueError):
                    continue
                return f * 100 if f <= 1.0 else f
        return None

    def pick_str(d, *keys):
        if not isinstance(d, dict):
            return None
        for k in keys:
            if k in d and d[k]:
                return d[k]
        return None

    fh = raw.get("five_hour") or raw.get("fiveHour") or raw
    wk = raw.get("weekly") or raw.get("seven_day") or raw.get("sevenDay") or raw

    out = {}
    fh_pct = pick_pct(fh, "utilization", "percent", "pct")
    if fh_pct is None:
        fh_pct = pick_pct(raw, "five_hour_pct")
    fh_reset = pick_str(fh, "resets_at", "reset_at", "resetsAt")
    if not fh_reset:
        fh_reset = pick_str(raw, "five_hour_resets_at")

    wk_pct = pick_pct(wk, "utilization", "percent", "pct")
    if wk_pct is None:
        wk_pct = pick_pct(raw, "weekly_pct")
    wk_reset = pick_str(wk, "resets_at", "reset_at", "resetsAt")
    if not wk_reset:
        wk_reset = pick_str(raw, "weekly_resets_at")

    if fh_pct is not None:
        out["five_hour_pct"] = fh_pct
    if fh_reset:
        out["five_hour_resets_at"] = fh_reset
    if wk_pct is not None:
        out["weekly_pct"] = wk_pct
    if wk_reset:
        out["weekly_resets_at"] = wk_reset
    return out


def fetch_usage(token: str, timeout: float = USAGE_FETCH_TIMEOUT_SECONDS) -> dict:
    """Returns {ok: bool, data?: dict, error?: str}."""
    if not token:
        return {"ok": False, "error": "auth"}
    req = urllib.request.Request(
        USAGE_API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ok": False, "error": "auth"}
        if e.code == 429:
            return {"ok": False, "error": "rate_limited"}
        return {"ok": False, "error": "unknown"}
    except (urllib.error.URLError, OSError, TimeoutError):
        return {"ok": False, "error": "network"}
    except Exception:
        return {"ok": False, "error": "unknown"}
    try:
        raw = json.loads(body)
    except (ValueError, TypeError):
        return {"ok": False, "error": "unknown"}
    return {"ok": True, "data": _normalize_usage(raw)}


def _cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "ccstatusbar" / "usage.json"


def cache_read(path=None) -> dict:
    p = Path(path) if path else _cache_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def cache_write(data: dict, path=None) -> None:
    p = Path(path) if path else _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, p)
    except OSError:
        return


def cache_decide(cache, now: float) -> dict:
    """Pure: decide whether to fetch and what to render from cache."""
    success = cache.get("last_success") if isinstance(cache, dict) else None
    failure = cache.get("last_failure") if isinstance(cache, dict) else None

    s_age = None
    if isinstance(success, dict) and isinstance(success.get("ts"), (int, float)):
        s_age = now - success["ts"]
    else:
        success = None

    f_age = None
    if isinstance(failure, dict) and isinstance(failure.get("ts"), (int, float)):
        f_age = now - failure["ts"]
    else:
        failure = None

    if success and s_age < CACHE_FRESH_SECONDS:
        return {"should_fetch": False, "data": success.get("data"),
                "stale": False}
    if failure and f_age < CACHE_FAILURE_BACKOFF_SECONDS:
        if success and s_age < CACHE_STALE_SECONDS:
            return {"should_fetch": False, "data": success.get("data"),
                    "stale": True}
        return {"should_fetch": False, "data": None, "stale": False}
    return {"should_fetch": True, "data": None, "stale": False}


def get_usage(token, fetch_fn=None, cache_path=None, now=None):
    """Returns {data, stale} for rendering, or None to omit segments."""
    now = now if now is not None else time.time()
    fetch_fn = fetch_fn or fetch_usage
    cache = cache_read(cache_path)
    decision = cache_decide(cache, now)
    if not decision["should_fetch"]:
        if decision["data"]:
            return {"data": decision["data"], "stale": decision["stale"]}
        return None

    result = fetch_fn(token)
    if result.get("ok"):
        cache["last_success"] = {"ts": now, "data": result.get("data") or {}}
        cache_write(cache, cache_path)
        return {"data": cache["last_success"]["data"], "stale": False}

    cache["last_failure"] = {"ts": now,
                             "error": result.get("error", "unknown")}
    cache_write(cache, cache_path)
    success = cache.get("last_success")
    if isinstance(success, dict):
        ts = success.get("ts")
        if isinstance(ts, (int, float)) and (now - ts) < CACHE_STALE_SECONDS:
            return {"data": success.get("data"), "stale": True}
    return None


def _format_hours_minutes(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h{m}m"


def _format_days_hours(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    return f"{d}d{h}h"


def _parse_iso(ts):
    if not ts or not isinstance(ts, str):
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def render_rate_limit_segments(usage, now=None):
    if not isinstance(usage, dict):
        return []
    data = usage.get("data") or {}
    if not isinstance(data, dict):
        return []
    star = "*" if usage.get("stale") else ""
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)

    segs = []
    fh_pct = data.get("five_hour_pct")
    fh_reset = _parse_iso(data.get("five_hour_resets_at"))
    if fh_pct is not None and fh_reset is not None:
        delta = (fh_reset - now_dt).total_seconds()
        segs.append(f"5h:{round(fh_pct)}%({_format_hours_minutes(delta)}){star}")

    wk_pct = data.get("weekly_pct")
    wk_reset = _parse_iso(data.get("weekly_resets_at"))
    if wk_pct is not None and wk_reset is not None:
        delta = (wk_reset - now_dt).total_seconds()
        segs.append(f"wk:{round(wk_pct)}%({_format_days_hours(delta)}){star}")

    return segs


def render(payload: dict) -> str:
    parts = []
    cwd = extract_cwd_basename(payload)
    model = extract_model_name(payload)
    git_cwd = (payload.get("workspace") or {}).get("current_dir")
    git_seg = render_git_segment(get_git_status(git_cwd))
    ctx_seg = render_ctx_segment(parse_transcript(payload.get("transcript_path")))

    rate_segs = []
    token = get_access_token()
    if token:
        usage_info = get_usage(token)
        rate_segs = render_rate_limit_segments(usage_info)

    if cwd:
        parts.append(cwd)
    if git_seg:
        parts.append(git_seg)
    if ctx_seg:
        parts.append(ctx_seg)
    parts.extend(rate_segs)
    if model:
        parts.append(model)
    return "  ".join(parts)


def main() -> int:
    try:
        payload = read_payload(sys.stdin)
        line = render(payload)
        sys.stdout.write(line + "\n")
    except Exception:
        # Last-resort fallback — never crash Claude Code's status line.
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
