# ccstatusbar

A single-file, stdlib-only status line for [Claude Code](https://claude.com/claude-code). Shows your working directory, git state, context-window usage, and 5-hour / weekly rate-limit usage at a glance.

```
~/myproject  main | S:2 U:1 ↑1  ctx:42%(84k/200k)  5h:13%(0h44m)  wk:25%(1d1h)  Opus 4.7
```

Segments appear and disappear based on what's available — no git repo, no git segment; API-key user, no rate-limit segments; etc.

## Install (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/evanrmattso/ccstatusbar/main/install.sh | bash
```

This downloads `statusline.py` to `~/.claude/ccstatusbar.py`, makes it executable, and patches `~/.claude/settings.json` to wire it up as the status line. An existing `settings.json` is backed up to `settings.json.bak` first.

Restart Claude Code after install.

## Install (manual)

If you'd rather not pipe a script into bash:

1. Download [`statusline.py`](https://raw.githubusercontent.com/evanrmattso/ccstatusbar/main/statusline.py) to `~/.claude/ccstatusbar.py`.
2. `chmod +x ~/.claude/ccstatusbar.py`.
3. Add this to `~/.claude/settings.json` (merge with anything that's already there):

   ```json
   {
     "statusLine": {
       "type": "command",
       "command": "/Users/YOU/.claude/ccstatusbar.py"
     }
   }
   ```

4. Restart Claude Code.

## Platform support

- **macOS** — first-class. OAuth credentials are read from (and refreshed back to) the Keychain.
- **Linux** — first-class. OAuth credentials are read from `~/.claude/.credentials.json` (atomic write, mode 0600).
- **Windows: WSL strongly recommended.** Native Windows is not supported in v1. Run inside WSL for the full experience. You *can* run the script under PowerShell at your own risk, but there is no Keychain / Credential Manager support — rate-limit segments will be silently omitted.

Runtime requirements: Python 3.9+ and `git` on `$PATH`. No third-party packages.

## Segment reference

Segments are joined by two spaces. Order is fixed:

| Segment | When it shows | Example |
|---|---|---|
| `cwd` | Always | `~/myproject` |
| `git` | When the cwd is inside a git repo | `main \| S:2 U:1 A:1 ↑1↓0` |
| `ctx` | When a transcript is available with at least one assistant message that reports usage | `ctx:42%(84k/200k)` |
| `5h` / `wk` | OAuth user only, when the usage API is reachable (or recently cached) | `5h:13%(0h44m)` `wk:25%(1d1h)` |
| `[API auth]` | OAuth credentials are present but a refresh failed | `[API auth]` |
| `model` | Always (last) | `Opus 4.7` |

Git counters: `S` = staged, `U` = unstaged, `A` = untracked, `↑N` = commits ahead of upstream, `↓N` = commits behind. Zero counters are suppressed. A clean repo shows just the branch name.

Rate-limit segments display percent used and the time until reset (`HhMm` for the 5-hour window, `DdHh` for weekly). A trailing `*` means the value came from cache after a network failure.

## Threshold colors

The percentage digits in `ctx`, `5h`, and `wk` are colored:

- `< 70%` — green
- `70–89%` — yellow
- `≥ 90%` — red

Other accents: `cwd` blue, branch cyan, git counters yellow, `↑N↓N` cyan, `[API auth]` yellow, model dim.

Set `NO_COLOR=1` (any non-empty value, per [no-color.org](https://no-color.org)) to disable all colors.

## Troubleshooting

**Rate-limit segments missing.**
- API-key users (no Claude subscription OAuth token) — expected; the usage endpoint is OAuth-only.
- Credentials expired and refresh failed — you'll see `[API auth]` instead. Run `claude login`.
- Network error and no recent cache — the segments stay hidden. They'll come back on the next successful fetch.

**I see `[API auth]`.**
Your OAuth refresh token can't get a new access token. Run `claude login` to re-authenticate.

**Status line not showing at all.**
- Check `~/.claude/settings.json` has a `statusLine.command` entry pointing at `~/.claude/ccstatusbar.py`.
- Run the script directly to see if it errors:

  ```bash
  echo '{"model":{"display_name":"Opus 4.7"},"workspace":{"current_dir":"'"$PWD"'"},"transcript_path":""}' | python3 ~/.claude/ccstatusbar.py
  ```

- Restart Claude Code after editing `settings.json`.

**Cache feels stale.**
Delete `~/.cache/ccstatusbar/usage.json`. The next render will repopulate it.

## Customization

Open `~/.claude/ccstatusbar.py` and edit the constants near the top of the file:

- `RENDER_BUDGET_SECONDS` — wall-clock deadline for the network path (default 2.0).
- `CACHE_FRESH_SECONDS`, `CACHE_STALE_SECONDS`, `CACHE_FAILURE_BACKOFF_SECONDS` — usage cache TTLs.
- `COLOR_*` constants — ANSI codes for each accent.
- Threshold breakpoints in `_threshold_color` — change the 70 / 90 cutoffs.
- Segment composition order — see `render()` near the bottom of the file.

The script is a single file with no runtime dependencies, so it's safe to edit in place. Re-running the installer will overwrite your edits — keep a copy if you've customized.

## Uninstall

1. Remove the `statusLine` block from `~/.claude/settings.json`.
2. Delete `~/.claude/ccstatusbar.py`.
3. (Optional) Delete the cache: `rm -rf ~/.cache/ccstatusbar`.
