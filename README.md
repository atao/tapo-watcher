# tapo-watcher

[![CI](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

Moves a TP-Link Tapo C210 camera depending on whether your phones are
connected to the home Wi-Fi.

- At least one phone at home → camera goes to `ACTION_HOME` (e.g. a preset facing a wall)
- All phones away → camera goes to `ACTION_AWAY` (e.g. a preset facing the entrance)

The camera only moves when the state changes, not on every check.

## How detection works

Every `INTERVAL` seconds, for each phone in `PHONES`:

1. Send a few UDP packets to port 5353 (mDNS) to wake a sleeping iPhone's Wi-Fi.
2. Ping it.
3. Check the kernel ARP table (`ip neigh`): a fresh `REACHABLE`/`DELAY`/`PROBE`
   entry counts as present, since iPhones often ignore ping but still answer ARP.
4. If a MAC is configured, the ARP entry must match it.

**The IP follows the MAC.** A phone is identified by its MAC; the IP in
`PHONES` is only a starting hint. If the phone is not found at its last known
IP (DHCP gave it a new one, or no IP was configured), the script looks the MAC
up in the ARP table — pinging the camera's /24 first if needed — and remembers
the new IP (logged as `new IP`). `tapo.env` is never rewritten.

### Adaptive away delay

A sleeping iPhone can stay silent on the network for minutes, so a phone is
not declared **away** as soon as it stops answering. Each phone has its own
away delay, learned automatically:

1. When a phone misses at least one check and then answers again within
   `AWAY_MAX` (20 min by default), that silence is recorded in
   `tapo-watcher.state.json`. Longer silences are real absences and are ignored.
2. Once `ADAPTIVE_MIN_SAMPLES` silences are recorded, the away delay is the
   95th percentile of the last 200 silences × `ADAPTIVE_MARGIN`, clamped to
   [`AWAY_MIN`, `AWAY_MAX`].
3. Before that, `AWAY_AFTER_MISSES` × `INTERVAL` is used.

The learned data survives restarts. Check it with:

```bash
uv run tapo_watcher.py --stats
```

Delete `tapo-watcher.state.json` to start learning again (e.g. after
changing phone). Set `ADAPTIVE_DELAY=0` to always use the fixed delay.

The machine running the script must be on the **same LAN** as the phones
(ARP does not cross routers or guest networks).

## Requirements

- Linux with `ping` and `ip` (iproute2)
- [uv](https://docs.astral.sh/uv/) (manages Python and dependencies)
- Python 3.9+ (uv installs one if needed)
- [pytapo](https://github.com/JurajNyiri/pytapo), declared in `pyproject.toml` and pinned in `uv.lock`

## Setup

### 1. Install

```bash
uv sync                # creates .venv from uv.lock
cp tapo.env.example tapo.env
chmod 600 tapo.env
```

### 2. Camera

In the Tapo app:

- Create a **camera account**: Camera → Settings → Advanced Settings → Camera Account.
- Create the presets used by `ACTION_HOME` / `ACTION_AWAY`, or save the current
  position from the command line:

```bash
uv run python -c "import tapo_watcher as tp; print(tp.camera().savePreset('Away'))"
```

### 3. Phones

- **iPhone**: Settings → Wi-Fi → (i) → *Private Wi-Fi Address* = **Off** or **Fixed**
  (never *Rotating*). Note the MAC shown there.
- **DHCP** (e.g. Pi-hole → Settings → DHCP → Static DHCP leases): a static IP is
  optional since the IP is resolved from the MAC, but it avoids a subnet ping
  sweep when a phone changes IP.

### 4. Configure `tapo.env`

| Variable | Description | Default |
| --- | --- | --- |
| `PHONES` | Comma-separated `ip=mac` list. IP or MAC may be omitted (`=mac`, `ip=`). | required |
| `CAMERA_IP` | Camera IP address | required |
| `CAMERA_USER` | Camera account username | `admin` |
| `CAMERA_PASSWORD` | Camera account password. Wrap in single quotes if it contains `$`. | |
| `ACTION_HOME` | Action when a phone is home | `preset:Home` |
| `ACTION_AWAY` | Action when all phones are away | `preset:Away` |
| `INTERVAL` | Seconds between checks | `30` |
| `AWAY_AFTER_MISSES` | Failed checks before "away" (fixed delay, or until enough silences are learned) | `6` |
| `ADAPTIVE_DELAY` | Learn the away delay per phone (`1`/`0`) | `1` |
| `AWAY_MIN` | Minimum learned away delay (s) | `60` |
| `AWAY_MAX` | Maximum away delay (s); longer silences are real absences | `1200` |
| `ADAPTIVE_MARGIN` | Safety factor applied to the 95th percentile | `1.2` |
| `ADAPTIVE_MIN_SAMPLES` | Silences needed before using the learned delay | `5` |
| `STATE_FILE` | Where learned silences are stored | `tapo-watcher.state.json` |

`PHONE_IP` / `PHONE_MAC` are still accepted for a single phone.

**Actions:**

| Action | Effect |
| --- | --- |
| `preset:<name or id>` | Go to a saved position (disables privacy mode first) |
| `move:<x>,<y>` | Relative motor move — drifts over time, prefer presets |
| `privacy:on` / `privacy:off` | Toggle privacy mode (lens cover) |
| `none` | Do nothing |

## Usage

The script automatically loads `tapo.env` from its own directory (variables
already set in the environment take precedence). Use `TAPO_ENV_FILE=/path/to/file`
to load another file.

```bash
uv run tapo_watcher.py --check     # print whether a phone is connected
uv run tapo_watcher.py --presets   # list camera presets
uv run tapo_watcher.py --once      # one check + action
uv run tapo_watcher.py --stats     # learned away delay per phone
uv run tapo_watcher.py             # run forever
```

### Managing dependencies

```bash
uv add <package>        # add a dependency (updates pyproject.toml + uv.lock)
uv lock --upgrade       # upgrade pinned versions
uv sync                 # install exactly what uv.lock says
```

## Development

```bash
uv sync                 # installs dev tools too (pytest, ruff)
uv run pytest           # tests (no network, camera or tapo.env needed)
uv run ruff check .     # lint
uv run ruff format .    # format
```

GitHub Actions (`.github/workflows/ci.yml`) runs ruff and the tests on Python
3.9, 3.12 and 3.13 for every push to `main` and every pull request.

## Run as a service

`tapo-watcher.service` is a systemd user unit running `uv run --frozen`
(edit the paths if the project is not in `~/tapo-watcher` or uv is
not in `~/.local/bin`):

```bash
mkdir -p ~/.config/systemd/user
cp tapo-watcher.service ~/.config/systemd/user/
systemctl --user enable --now tapo-watcher
journalctl --user -u tapo-watcher -f
```

To keep it running when you are logged out:

```bash
sudo loginctl enable-linger "$USER"
```

## Troubleshooting

- **Phone always "absent"**: check that its MAC appears in `ip neigh` while it is
  on Wi-Fi, that the private address is not *Rotating*, and that it is not on a
  guest network.
- **"new IP" in the logs**: the phone changed IP; detection followed it via
  its MAC. Set a static DHCP lease if it happens often.
- **Camera auth error**: use the camera account from the Tapo app, not your
  TP-Link cloud email.
- **Camera does not move**: privacy mode blocks motion; `preset:` and `move:`
  turn it off automatically.
