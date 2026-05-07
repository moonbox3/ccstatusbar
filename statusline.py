#!/usr/bin/env python3
"""ccstatusbar — Claude Code status line.

Slice 01: skeleton. Reads Claude Code's JSON payload on stdin, prints a
single line with the cwd basename and model name. Later slices add git,
context %, rate-limit segments, colors, and the render budget.
"""

import json
import os
import sys
from pathlib import Path


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


def render(payload: dict) -> str:
    parts = []
    cwd = extract_cwd_basename(payload)
    model = extract_model_name(payload)
    if cwd:
        parts.append(cwd)
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
