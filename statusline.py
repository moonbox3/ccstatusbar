#!/usr/bin/env python3
"""ccstatusbar — Claude Code status line.

Slice 01: skeleton. Reads Claude Code's JSON payload on stdin, prints a
single line with the cwd basename and model name. Later slices add git,
context %, rate-limit segments, colors, and the render budget.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

GIT_TIMEOUT_SECONDS = 2


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


def render(payload: dict) -> str:
    parts = []
    cwd = extract_cwd_basename(payload)
    model = extract_model_name(payload)
    git_cwd = (payload.get("workspace") or {}).get("current_dir")
    git_seg = render_git_segment(get_git_status(git_cwd))
    ctx_seg = render_ctx_segment(parse_transcript(payload.get("transcript_path")))
    if cwd:
        parts.append(cwd)
    if git_seg:
        parts.append(git_seg)
    if ctx_seg:
        parts.append(ctx_seg)
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
