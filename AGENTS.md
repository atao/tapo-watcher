# AGENTS.md

Guidance for AI coding agents working on this repository.

## Project

Single-file Python tool that detects phones on the home Wi-Fi (ping + ARP) and
moves a TP-Link Tapo C210 camera via [pytapo](https://github.com/JurajNyiri/pytapo).
See `README.md` for user-facing docs.

## Layout

| File | Purpose |
| --- | --- |
| `tapo_watcher.py` | The whole tool: config, phone detection, camera actions, main loop |
| `tapo.env.example` | Config template (committed, placeholder values only) |
| `tapo.env` | Real config with credentials — **gitignored, never read aloud, commit or print** |
| `tapo-watcher.service` | systemd user unit |
| `pyproject.toml` / `uv.lock` | Dependencies, managed with uv (both committed) |
| `tapo-watcher.state.json` | Learned phone silences for the adaptive away delay (gitignored, runtime state — do not delete or edit without asking) |
| `tests/` | pytest suite — no network, camera or real `tapo.env` (see `tests/conftest.py`) |
| `.github/workflows/ci.yml` | GitHub Actions: ruff lint/format + pytest on Python 3.9, 3.12, 3.13 |
| `.venv/` | Virtualenv created by `uv sync` (gitignored) |

## Shell commands: always use `rtk`

Always run shell commands through [`rtk`](https://github.com/rtk-ai/rtk), the
token-saving CLI proxy installed on this machine:

- Use the matching subcommand when one exists: `rtk git status`, `rtk ls`,
  `rtk grep`, `rtk find`, `rtk diff`, `rtk read`, `rtk log`, `rtk test`…
- Wrap other commands with `rtk summary <cmd>` or `rtk err <cmd>`.
- Treat rtk output as the complete result. Use `rtk bypass <cmd>` only when the
  output is unusable (empty when output was expected, contradicts the exit
  code, or garbled).
- Run `rtk --help` to list subcommands.

## Commands

```bash
rtk summary uv sync                              # install deps from uv.lock
rtk err uv run python -m py_compile tapo_watcher.py  # syntax check
uv run tapo_watcher.py --check                  # detection only, no camera access
uv run tapo_watcher.py --presets                # read-only camera call
uv run tapo_watcher.py --stats                  # learned away delays, read-only
uv run tapo_watcher.py --once                   # MOVES the camera
```

```bash
rtk test uv run pytest                           # test suite (what CI runs)
rtk err uv run ruff check .                      # lint
uv run ruff format .                             # format
```

Before finishing a change, run `uv run ruff format .`, `uv run ruff check .` and
`uv run pytest` — CI fails on any of them. Add or update tests in `tests/` for
every behaviour change; tests must never hit the network, the camera or the
real `tapo.env` (use the `tp` / `net` fixtures and monkeypatch).

## Rules

- **Secrets**: never print, log, or commit `CAMERA_PASSWORD` or the contents of
  `tapo.env`. Keep `tapo.env` at mode `600`. `CAMERA_PASSWORD` must stay
  single-quoted because it may contain `$`.
- **Physical side effects**: `--once`, the main loop, `setPreset`, `moveMotor`,
  `savePreset` and `setPrivacyMode` act on a real camera in someone's home.
  Prefer `--check` and `--presets`; ask before moving the camera or overwriting
  presets.
- **Network**: detection must stay passive-ish (UDP 5353 poke, ping, ARP read).
  Do not add scans beyond the camera's /24 or anything requiring root.
- **Dependencies**: managed with **uv** only — use `uv add` / `uv remove` /
  `uv lock`, never `pip install` or hand-editing `uv.lock`. Run Python through
  `uv run`. Standard library + `pytapo` only. `pytapo` is imported lazily
  inside `camera()` so `--check` works without it.
- **Config**: all settings come from environment variables, auto-loaded from
  `tapo.env` next to the script by `load_env_file` (existing env wins, no `$`
  expansion, quotes stripped). When adding one,
  update `tapo.env.example` and the table in `README.md`. Keep `PHONE_IP` /
  `PHONE_MAC` backward compatible.
- **Language**: everything in the repository is in English — code comments,
  docstrings, log messages, CLI output, config comments and docs.
- **Phone identity is the MAC**: the IP in `PHONES` is only a starting hint.
  When a phone is not found at its last known IP, it is re-resolved from its
  MAC (`ip_from_mac`) and cached in `_known_ip`. Never write it back to
  `tapo.env`.
- **Adaptive away delay**: presence is time-based (`last_seen` per phone vs
  `DelayLearner.delay(key)`), not a miss counter. Only silences that end with
  the phone answering again and are `<= AWAY_MAX` may be learned.
- **State changes only**: the camera must move only when presence changes, and a
  failed action must be retried on the next loop (state not updated).

## Git workflow

- After **every** change, propose a commit and a push: show the files changed
  and a suggested commit message, then wait for the user's approval before
  running `git commit` and `git push`. Never commit or push without approval.
- One logical change per commit, with a short imperative English message
  (e.g. `Add adaptive away delay`).
- Before proposing, make sure `ruff format`, `ruff check` and `pytest` pass.
- Check `git status` first: `tapo.env`, `*.state.json` and `.venv/` must never
  be staged.
