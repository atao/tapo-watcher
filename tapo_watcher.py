#!/usr/bin/env python3
"""
Watch whether phones are connected to the local Wi-Fi and move one or more
TP-Link Tapo C210 cameras accordingly.

  - at least one phone home  -> ACTION_HOME (e.g. turn the camera to the wall)
  - all phones away          -> ACTION_AWAY (e.g. turn back to the entrance)

Optionally, the Home Mode of Synology Surveillance Station follows the same
state (on when a phone is home, off when all are away): set DSM_URL.

The action only runs when the state changes, not on every check. With several
cameras (CAMERAS=name1,name2), each camera has its own settings
(CAMERA_<NAME>_IP, ...) and a camera whose action failed is retried alone.

Configured through environment variables (see tapo.env.example).

Actions:
  preset:<name or id>   go to a position saved in the Tapo app
  move:<x>,<y>          relative motor move (e.g. move:90,0)
  privacy:on|off        enable / disable privacy mode
  none                  do nothing

Usage:
  python3 tapo_watcher.py            # run forever
  python3 tapo_watcher.py --once     # single check + action
  python3 tapo_watcher.py --check    # only print whether a phone is home
  python3 tapo_watcher.py --presets  # list the camera presets
  python3 tapo_watcher.py --stats    # show the learned away delay per phone
  python3 tapo_watcher.py --dsm      # show the Surveillance Station Home Mode
  python3 tapo_watcher.py --save-home [PRESET]  # current position = home preset
  python3 tapo_watcher.py --save-away [PRESET]  # current position = away preset
  python3 tapo_watcher.py --rename-presets [HOME,AWAY]  # rename them (camera moves)
  --camera NAME                      # limit --once / --presets to one camera

Adaptive away delay:
  A sleeping iPhone can stay silent on the network for minutes. Every time a
  phone goes silent and then answers again (shorter than AWAY_MAX), the
  silence is recorded in STATE_FILE. The away delay of each phone is then
  the 95th percentile of its recorded silences x ADAPTIVE_MARGIN, or that
  percentile + ADAPTIVE_EXTRA_CHECKS x INTERVAL if longer, clamped to
  [AWAY_MIN, AWAY_MAX]. Until ADAPTIVE_MIN_SAMPLES silences are recorded,
  AWAY_AFTER_MISSES x INTERVAL is used.
"""

import argparse
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass

log = logging.getLogger("tapo-watcher")


def load_env_file(path):
    """Load KEY=value lines from a .env file into os.environ.

    Variables already set in the environment win (e.g. systemd
    EnvironmentFile or `export`). Values may be wrapped in single or double
    quotes; no `$` expansion is done, so passwords containing `$` are safe.
    """
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


# tapo.env next to the script, or the file given in TAPO_ENV_FILE
ENV_FILE = os.environ.get(
    "TAPO_ENV_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "tapo.env"),
)
load_env_file(ENV_FILE)


def set_env_value(key: str, value: str, path: str = None):
    """Set KEY=value in tapo.env (every existing KEY line, or appended),
    atomically and keeping the file at mode 600."""
    path = path or ENV_FILE
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        lines = []
    pattern = re.compile(rf"^\s*{re.escape(key)}=")
    found = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={value}\n"
            found = True
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.writelines(lines)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Missing environment variable: {name}")
    return value


def parse_phones():
    """PHONES="192.168.1.50=aa:bb:..,=cc:dd:.." : comma-separated ip=mac list.
    IP or MAC may be omitted (MAC only = IP resolved from the ARP table).
    Backward compatible with PHONE_IP / PHONE_MAC for a single phone."""
    raw = env("PHONES", "")
    if not raw and env("PHONE_IP"):
        raw = f"{env('PHONE_IP')}={env('PHONE_MAC', '')}"
    phones = []
    for item in filter(None, (p.strip() for p in raw.split(","))):
        ip, _, mac = item.partition("=")
        phones.append((ip.strip(), mac.strip().lower()))
    if not phones:
        sys.exit("Missing environment variable: PHONES")
    return phones


@dataclass(frozen=True)
class Camera:
    name: str
    ip: str
    user: str
    password: str
    action_home: str
    action_away: str


def env_name(name: str) -> str:
    """Camera name -> environment variable infix ("living-room" -> "LIVING_ROOM")."""
    return re.sub(r"[^A-Z0-9]", "_", name.upper())


def parse_cameras():
    """CAMERAS="salon,entrance": one camera per name, configured with
    CAMERA_<NAME>_IP / _USER / _PASSWORD / _ACTION_HOME / _ACTION_AWAY.
    CAMERA_USER, CAMERA_PASSWORD, ACTION_HOME and ACTION_AWAY are the defaults
    shared by all cameras. Without CAMERAS, a single camera is read from
    CAMERA_IP (backward compatible)."""
    user = env("CAMERA_USER", "admin")
    password = env("CAMERA_PASSWORD", "")
    home = env("ACTION_HOME", "preset:Home")
    away = env("ACTION_AWAY", "preset:Away")
    names = [n.strip() for n in env("CAMERAS", "").split(",") if n.strip()]
    if not names:
        return [Camera("camera", env("CAMERA_IP", required=True), user, password, home, away)]

    cameras, seen = [], set()
    for name in names:
        prefix = f"CAMERA_{env_name(name)}_"
        if prefix in seen:
            sys.exit(f"Duplicate camera name in CAMERAS: {name}")
        seen.add(prefix)
        cameras.append(
            Camera(
                name,
                env(prefix + "IP", required=True),
                env(prefix + "USER", user),
                env(prefix + "PASSWORD", password),
                env(prefix + "ACTION_HOME", home),
                env(prefix + "ACTION_AWAY", away),
            )
        )
    return cameras


@dataclass(frozen=True)
class SurveillanceStation:
    url: str  # e.g. https://192.168.1.10:5001
    user: str
    password: str
    verify_ssl: bool = True


def parse_dsm():
    """DSM_URL / DSM_USER / DSM_PASSWORD: Synology Surveillance Station whose
    Home Mode follows the phones. None when DSM_URL is not set."""
    url = env("DSM_URL", "").strip().rstrip("/")
    if not url:
        return None
    if not re.match(r"^https?://", url):
        sys.exit("DSM_URL must start with http:// or https:// (e.g. https://192.168.1.10:5001)")
    return SurveillanceStation(
        url,
        env("DSM_USER", required=True),
        env("DSM_PASSWORD", required=True),
        env("DSM_VERIFY_SSL", "1").lower() in ("1", "true", "yes", "on"),
    )


PHONES = parse_phones()
CAMERAS = parse_cameras()
DSM = parse_dsm()
INTERVAL = int(env("INTERVAL", "30"))
# Phones often turn Wi-Fi off while asleep: wait for several consecutive
# failed checks before declaring them away.
AWAY_AFTER_MISSES = int(env("AWAY_AFTER_MISSES", "6"))

# Adaptive away delay (see module docstring)
ADAPTIVE = env("ADAPTIVE_DELAY", "1").lower() in ("1", "true", "yes", "on")
AWAY_MIN = int(env("AWAY_MIN", "60"))
# Silences longer than this are real absences: never learned, and the delay
# never exceeds it.
AWAY_MAX = int(env("AWAY_MAX", "1200"))
ADAPTIVE_MARGIN = float(env("ADAPTIVE_MARGIN", "1.2"))
# Missed checks tolerated on top of the learned silence. Silences are
# multiples of the check period (one missed check ~ 2 x INTERVAL), so a
# multiplicative margin alone is too thin for short silences.
ADAPTIVE_EXTRA_CHECKS = int(env("ADAPTIVE_EXTRA_CHECKS", "2"))
ADAPTIVE_MIN_SAMPLES = int(env("ADAPTIVE_MIN_SAMPLES", "5"))
ADAPTIVE_HISTORY = 200  # silences kept per phone (most recent)
SWEEP_BATCH = 32  # pings run at once when looking for a phone's new IP
STATE_FILE = env(
    "STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "tapo-watcher.state.json"),
)


# --------------------------------------------------------------------------
# Phone detection
# --------------------------------------------------------------------------
# Last known IP of each phone (key = MAC). The configured IP is only a
# starting hint: if DHCP hands out another one, it is found again through
# the MAC and remembered here.
_known_ip = {}


def phone_key(ip: str, mac: str) -> str:
    """Stable identifier of a phone: its MAC, or its IP if no MAC is set."""
    return mac or ip


def phone_seen(ip: str, mac: str) -> bool:
    """True if this phone answers on the Wi-Fi right now."""
    current = _known_ip.get(mac, ip) if mac else ip
    if current and phone_is_connected(current, mac):
        log.debug("%s (%s) present", current, mac or "?")
        return True
    if not mac:
        return False
    # Not found at the known IP: the phone may have changed IP
    found = ip_from_mac(mac)
    if found and found != current:
        log.info("Phone %s: new IP %s (was: %s)", mac, found, current or "unknown")
        _known_ip[mac] = found
        return phone_is_connected(found, mac)
    return False


def any_phone_connected() -> bool:
    """True if at least one phone from the list is on the Wi-Fi."""
    return any(phone_seen(ip, mac) for ip, mac in PHONES)


# --------------------------------------------------------------------------
# Adaptive away delay
# --------------------------------------------------------------------------
class DelayLearner:
    """Learns how long each phone can stay silent while still at home."""

    def __init__(self, path: str):
        self.path = path
        self.silences = {}  # phone key -> [seconds, ...]
        try:
            with open(path) as fh:
                self.silences = json.load(fh).get("silences", {})
        except (FileNotFoundError, ValueError):
            pass

    def record(self, key: str, seconds: float):
        values = self.silences.setdefault(key, [])
        values.append(round(seconds))
        del values[:-ADAPTIVE_HISTORY]
        self._save()
        log.info(
            "Phone %s: silent for %ds, then answered again -> away delay now %ds",
            key,
            seconds,
            self.delay(key),
        )

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump({"silences": self.silences}, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Cannot save %s: %s", self.path, exc)

    def p95(self, key: str) -> float:
        values = sorted(self.silences.get(key, []))
        return values[int(0.95 * (len(values) - 1))] if values else 0

    def delay(self, key: str) -> int:
        """Seconds without an answer before this phone counts as away."""
        default = max(AWAY_MIN, AWAY_AFTER_MISSES * INTERVAL)
        if not ADAPTIVE or len(self.silences.get(key, [])) < ADAPTIVE_MIN_SAMPLES:
            return default
        p95 = self.p95(key)
        learned = max(p95 * ADAPTIVE_MARGIN, p95 + ADAPTIVE_EXTRA_CHECKS * INTERVAL)
        return int(min(AWAY_MAX, max(AWAY_MIN, learned)))

    def stats(self) -> str:
        lines = []
        for ip, mac in PHONES:
            key = phone_key(ip, mac)
            values = self.silences.get(key, [])
            learned = "learned" if ADAPTIVE and len(values) >= ADAPTIVE_MIN_SAMPLES else "default"
            lines.append(
                f"{key}: {len(values)} silences recorded"
                + (f", max {max(values)}s, p95 {self.p95(key):.0f}s" if values else "")
                + f" -> away delay {self.delay(key)}s ({learned})"
            )
        return "\n".join(lines)


def ip_from_mac(mac: str) -> str:
    """Find a phone's IP from its MAC in the ARP table.

    If it is not there, ping the whole /24 of each camera (in parallel, ~1 s)
    to trigger ARP requests, then look again.
    """

    def lookup():
        out = subprocess.run(["ip", "neigh"], capture_output=True, text=True).stdout
        for line in out.lower().splitlines():
            if f"lladdr {mac} " in line + " " and "failed" not in line:
                return line.split()[0]
        return ""

    ip = lookup()
    if ip:
        return ip
    prefixes = dict.fromkeys(cam.ip.rsplit(".", 1)[0] for cam in CAMERAS)
    targets = [f"{prefix}.{i}" for prefix in prefixes for i in range(1, 255)]
    # Ping in batches: 254 processes at once exceed the systemd TasksMax of
    # small Raspberry Pis (fork fails with EAGAIN and the service crashes).
    for start in range(0, len(targets), SWEEP_BATCH):
        procs = []
        for target in targets[start : start + SWEEP_BATCH]:
            try:
                procs.append(
                    subprocess.Popen(
                        ["ping", "-c", "1", "-W", "1", target],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                )
            except OSError as exc:  # out of processes: use what is running
                log.debug("Sweep: cannot ping %s: %s", target, exc)
                break
        for proc in procs:
            proc.wait()
    return lookup()


def phone_is_connected(ip: str, mac: str) -> bool:
    """Wake the phone up, ping it, then check the ARP table.

    Many phones (iPhones especially) ignore ping while asleep but still answer
    the ARP request triggered by the ping, so the ARP entry state is checked
    too.
    """
    # Wake a sleeping iPhone's Wi-Fi chip: a few UDP packets to the mDNS port
    # (5353) are enough to make it answer ARP.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for _ in range(5):
                s.sendto(b"\x00", (ip, 5353))
                time.sleep(0.1)
    except OSError:
        pass

    ping = subprocess.run(
        ["ping", "-c", "2", "-W", "1", ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    neigh = (
        subprocess.run(["ip", "neigh", "show", ip], capture_output=True, text=True)
        .stdout.strip()
        .lower()
    )

    if mac and neigh and "lladdr" in neigh and mac not in neigh:
        log.debug("IP %s has another MAC address: %s", ip, neigh)
        return False

    if ping.returncode == 0:
        return True

    # Fresh ARP entry = the phone answered ARP
    return bool(re.search(r"lladdr \S+ .*\b(reachable|delay|probe)\b", neigh))


# --------------------------------------------------------------------------
# Camera
# --------------------------------------------------------------------------
def tapo_class():
    """pytapo's Tapo class, imported lazily so --check works without pytapo."""
    try:
        from pytapo import Tapo
    except ImportError:
        sys.exit("pytapo is not installed: run with `uv run tapo_watcher.py` (or `uv sync` first)")
    return Tapo


def camera(cam: Camera = None):
    """Connect to `cam` (default: the first configured camera)."""
    cam = cam or CAMERAS[0]
    return tapo_class()(cam.ip, cam.user, cam.password)


def select_cameras(name: str = None):
    """All cameras, or only the one called `name`."""
    if not name:
        return CAMERAS
    for cam in CAMERAS:
        if cam.name.lower() == name.lower():
            return [cam]
    sys.exit(f"Unknown camera '{name}'. Configured: {', '.join(c.name for c in CAMERAS)}")


def find_preset_id(tapo, wanted: str) -> str:
    presets = tapo.getPresets()  # {id: name}
    if wanted in presets:
        return wanted
    for pid, name in presets.items():
        if name.lower() == wanted.lower():
            return pid
    raise ValueError(f"Preset '{wanted}' not found. Available: {presets}")


def action_key(cam: Camera, home: bool) -> str:
    """tapo.env variable holding the home/away action of `cam`."""
    suffix = "ACTION_HOME" if home else "ACTION_AWAY"
    if not env("CAMERAS", "").strip():
        return suffix  # single camera: plain ACTION_HOME / ACTION_AWAY
    return f"CAMERA_{env_name(cam.name)}_{suffix}"


def preset_name(action: str):
    """Preset name of a preset action ("preset:Mur" -> "Mur"), else None."""
    kind, _, arg = action.strip().partition(":")
    return arg if kind == "preset" and arg else None


def save_position(cam: Camera, home: bool, name: str = None) -> str:
    """Save the camera's current position as its home (or away) preset.

    The preset keeps the name configured for that state (default "Home" /
    "Away", or `name`). Presets already called that are replaced: the new
    one is saved first, then the old ones are deleted, so a failed save
    loses nothing. The camera does not move. If the configured action did
    not point to this preset, tapo.env is updated. Returns the preset name.
    """
    current, other = (
        (cam.action_home, cam.action_away) if home else (cam.action_away, cam.action_home)
    )
    name = (name or preset_name(current) or ("Home" if home else "Away")).strip()
    if not name or "," in name:
        sys.exit(f"Invalid preset name: '{name}'")
    if preset_name(other) and preset_name(other).lower() == name.lower():
        sys.exit(
            f"'{name}' is the {'away' if home else 'home'} preset of camera {cam.name}: "
            "pick another name"
        )

    replaced, _ = store_preset(camera(cam), cam, name)
    log.info(
        "Camera %s: current position saved as %s preset '%s'%s",
        cam.name,
        "home" if home else "away",
        name,
        f" (replaces id {', '.join(replaced)})" if replaced else "",
    )

    point_action_to(cam, home, current, name)
    return name


def store_preset(tapo, cam: Camera, name: str, also_delete=()):
    """Save the current position as `name`, then delete the presets already
    called `name` and the ids in `also_delete`. Saving first means a failed
    save loses nothing. Returns (deleted ids, new id)."""
    before = dict(tapo.getPresets())
    replaced = [pid for pid, n in before.items() if n.lower() == name.lower()]
    replaced += [pid for pid in also_delete if pid in before and pid not in replaced]
    tapo.savePreset(name)
    added = set(tapo.getPresets()) - set(before)
    if not added:
        raise RuntimeError(f"Camera {cam.name}: preset '{name}' was not saved (8 presets max?)")
    for pid in replaced:
        tapo.deletePreset(pid)
    return replaced, added.pop()


def point_action_to(cam: Camera, home: bool, current_action: str, name: str):
    """Write ACTION_HOME/AWAY=preset:<name> in tapo.env if it differs."""
    wanted = f"preset:{name}"
    if current_action.strip() == wanted:
        return
    key = action_key(cam, home)
    set_env_value(key, wanted)
    log.info(
        "%s=%s written to %s: restart the service to use it "
        "(systemctl --user restart tapo-watcher)",
        key,
        wanted,
        ENV_FILE,
    )


PRESET_MOVE_WAIT = 10  # seconds for the motor to reach a preset


def rename_presets(cam: Camera, home_name: str = "HOME", away_name: str = "AWAY"):
    """Rename the home and away presets of `cam` (the ones ACTION_HOME /
    ACTION_AWAY point to) to `home_name` / `away_name`, and update tapo.env.

    The Tapo API has no rename: the camera goes to each preset, saves it
    under the new name, then the old one is deleted. The camera MOVES, and
    ends on the preset matching the phones (home or away)."""
    home_name, away_name = home_name.strip(), away_name.strip()
    for name in (home_name, away_name):
        if not name or "," in name:
            sys.exit(f"Invalid preset name: '{name}'")
    if home_name.lower() == away_name.lower():
        sys.exit("The home and away presets need different names")
    old_home, old_away = preset_name(cam.action_home), preset_name(cam.action_away)
    if not old_home or not old_away:
        sys.exit(
            f"Camera {cam.name}: ACTION_HOME and ACTION_AWAY must both be presets "
            f"(now: {cam.action_home!r} / {cam.action_away!r})"
        )
    if home_name.lower() == old_away.lower() or away_name.lower() == old_home.lower():
        sys.exit("Swapping the two names is not supported: use other names")

    tapo = camera(cam)
    new_ids = {}
    for home, old, new in ((True, old_home, home_name), (False, old_away, away_name)):
        old_id = find_preset_id(tapo, old)
        if old == new:
            new_ids[home] = old_id
            continue
        tapo.setPrivacyMode(False)  # privacy mode blocks motion
        tapo.setPreset(old_id)
        time.sleep(PRESET_MOVE_WAIT)
        _, new_ids[home] = store_preset(tapo, cam, new, also_delete=[old_id])
        log.info("Camera %s: preset '%s' renamed '%s' (id %s)", cam.name, old, new, new_ids[home])
        point_action_to(cam, home, cam.action_home if home else cam.action_away, new)

    present = any_phone_connected()
    tapo.setPreset(new_ids[present])
    log.info(
        "Camera %s: back on the %s preset '%s'",
        cam.name,
        "home" if present else "away",
        home_name if present else away_name,
    )


def run_action(action: str, cam: Camera = None):
    cam = cam or CAMERAS[0]
    action = action.strip()
    if not action or action == "none":
        return
    kind, _, arg = action.partition(":")
    tapo = camera(cam)

    if kind == "preset":
        pid = find_preset_id(tapo, arg)
        log.info("Camera %s -> preset %s (%s)", cam.name, arg, pid)
        # Privacy mode blocks motion
        tapo.setPrivacyMode(False)
        tapo.setPreset(pid)
    elif kind == "move":
        x, y = (int(v) for v in arg.split(","))
        log.info("Camera %s -> move x=%d y=%d", cam.name, x, y)
        tapo.setPrivacyMode(False)
        tapo.moveMotor(x, y)
    elif kind == "privacy":
        enabled = arg.lower() in ("on", "1", "true", "yes")
        log.info("Camera %s -> privacy mode %s", cam.name, "ON" if enabled else "OFF")
        tapo.setPrivacyMode(enabled)
    else:
        raise ValueError(f"Unknown action: {action}")


# --------------------------------------------------------------------------
# Synology Surveillance Station (optional)
# --------------------------------------------------------------------------
HOME_MODE_API = "SYNO.SurveillanceStation.HomeMode"
DSM_ERRORS = {
    100: "unknown error",
    101: "invalid parameter",
    102: "API not found",
    103: "method not found",
    104: "API version not supported",
    105: "permission denied: give this user the Surveillance Station manager profile",
    106: "session timeout",
    107: "session interrupted by a duplicate login",
    119: "invalid session",
    400: "wrong username or password",
    401: "account disabled",
    402: "permission denied",
    403: "2-step verification required: use an account without 2FA",
    404: "2-step verification failed",
    407: "IP address blocked after too many failed logins",
}


class DsmError(Exception):
    pass


def dsm_request(dsm: SurveillanceStation, path: str, params: dict) -> dict:
    """POST one Web API call (form-encoded, so the password stays out of URLs
    and logs) and return its `data`. Raises DsmError when DSM refuses it."""
    context = None
    if dsm.url.startswith("https://") and not dsm.verify_ssl:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(
        f"{dsm.url}/webapi/{path}", data=urllib.parse.urlencode(params).encode()
    )
    with urllib.request.urlopen(request, timeout=15, context=context) as response:
        body = json.load(response)
    if not body.get("success"):
        code = (body.get("error") or {}).get("code")
        reason = DSM_ERRORS.get(code, "see the Synology Web API documentation")
        raise DsmError(f"{params['api']} {params['method']}: error {code}, {reason}")
    return body.get("data") or {}


@contextmanager
def dsm_session(dsm: SurveillanceStation):
    """Log in, yield `call(method, **params)` for the Home Mode API, log out."""
    apis = dsm_request(
        dsm,
        "query.cgi",
        {
            "api": "SYNO.API.Info",
            "version": "1",
            "method": "query",
            "query": f"SYNO.API.Auth,{HOME_MODE_API}",
        },
    )
    if HOME_MODE_API not in apis:
        raise DsmError("Surveillance Station not found: is it installed and running on the NAS?")
    auth, home = apis["SYNO.API.Auth"], apis[HOME_MODE_API]
    auth_base = {"api": "SYNO.API.Auth", "version": str(min(auth["maxVersion"], 6))}
    sid = dsm_request(
        dsm,
        auth["path"],
        {
            **auth_base,
            "method": "login",
            "account": dsm.user,
            "passwd": dsm.password,
            "session": "SurveillanceStation",
            "format": "sid",
        },
    )["sid"]

    def call(method, **params):
        return dsm_request(
            dsm,
            home["path"],
            {
                "api": HOME_MODE_API,
                "version": str(max(home["minVersion"], 1)),
                "method": method,
                "_sid": sid,
                **params,
            },
        )

    try:
        yield call
    finally:
        try:
            dsm_request(
                dsm,
                auth["path"],
                {**auth_base, "method": "logout", "session": "SurveillanceStation", "_sid": sid},
            )
        except Exception as exc:  # the session expires on its own anyway
            log.debug("Surveillance Station logout failed: %s", exc)


def get_home_mode(dsm: SurveillanceStation) -> bool:
    with dsm_session(dsm) as call:
        return bool(call("GetInfo")["on"])


def set_home_mode(dsm: SurveillanceStation, on: bool):
    with dsm_session(dsm) as call:
        call("Switch", on="true" if on else "false")
    log.info("Surveillance Station -> Home Mode %s", "ON" if on else "OFF")


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def apply_camera(cam: Camera, home: bool) -> bool:
    """Run the home/away action of one camera. False if it failed."""
    try:
        run_action(cam.action_home if home else cam.action_away, cam)
        return True
    except Exception as exc:  # camera unreachable, wrong password...
        log.error("Camera %s: action failed: %s", cam.name, exc)
        return False


def apply_dsm(dsm: SurveillanceStation, home: bool) -> bool:
    """Home Mode on when a phone is home, off when all are away. False if it failed."""
    try:
        set_home_mode(dsm, home)
        return True
    except Exception as exc:  # NAS unreachable, wrong password, missing rights...
        log.error("Surveillance Station: Home Mode switch failed: %s", exc)
        return False


def apply_state(home: bool, cameras=None, dsm=None) -> bool:
    """Apply the state to every camera and to `dsm` (all are tried).
    True if all succeeded."""
    results = [apply_camera(cam, home) for cam in (cameras or CAMERAS)]
    if dsm:
        results.append(apply_dsm(dsm, home))
    return all(results)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--presets", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument(
        "--dsm", action="store_true", help="show the Surveillance Station Home Mode"
    )
    for state in ("home", "away"):
        parser.add_argument(
            f"--save-{state}",
            nargs="?",
            const="",
            metavar="PRESET",
            help=f"save the camera's current position as its {state} preset "
            "(replacing it; PRESET = new name, default: the configured one)",
        )
    parser.add_argument(
        "--rename-presets",
        nargs="?",
        const="HOME,AWAY",
        metavar="HOME,AWAY",
        help="rename the home and away presets (default HOME,AWAY); the camera moves",
    )
    parser.add_argument(
        "--camera",
        metavar="NAME",
        help="limit --once / --presets / --save-* / --rename-presets to one camera",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    learner = DelayLearner(STATE_FILE)

    if args.stats:
        print(learner.stats())
        return
    if args.presets:
        cameras = select_cameras(args.camera)
        for cam in cameras:
            presets = camera(cam).getPresets()
            print(f"{cam.name}: {presets}" if len(CAMERAS) > 1 else presets)
        return
    if args.rename_presets is not None:
        if len(CAMERAS) > 1 and not args.camera:
            sys.exit("Several cameras configured: add --camera NAME")
        names = args.rename_presets.split(",")
        if len(names) != 2:
            sys.exit("--rename-presets expects HOME_NAME,AWAY_NAME (e.g. HOME,AWAY)")
        rename_presets(select_cameras(args.camera)[0], *names)
        return
    if args.save_home is not None or args.save_away is not None:
        if args.save_home is not None and args.save_away is not None:
            sys.exit("Use --save-home or --save-away, not both (one camera position each)")
        if len(CAMERAS) > 1 and not args.camera:
            sys.exit("Several cameras configured: add --camera NAME")
        home = args.save_home is not None
        save_position(select_cameras(args.camera)[0], home, args.save_home or args.save_away)
        return
    if args.dsm:
        if not DSM:
            sys.exit("DSM_URL is not set")
        print("Home Mode", "on" if get_home_mode(DSM) else "off")
        return
    if args.check:
        present = any_phone_connected()
        print("connected" if present else "away")
        sys.exit(0 if present else 1)
    if args.once:
        present = any_phone_connected()
        log.info("Phone %s", "connected" if present else "away")
        # --camera targets one camera only: leave Surveillance Station alone
        dsm = None if args.camera else DSM
        sys.exit(0 if apply_state(present, select_cameras(args.camera), dsm) else 1)

    tapo_class()  # fail fast if pytapo is missing, not minutes later on first move

    state = None  # unknown at startup -> log the first state
    cam_state = {cam.name: None for cam in CAMERAS}  # last state applied per camera
    dsm_state = None  # last Home Mode applied
    last_seen = {}  # phone key -> time.monotonic() of the last answer
    log.info("Watching %s every %ds", ", ".join(ip for ip, _ in PHONES), INTERVAL)
    log.info("Cameras: %s", ", ".join(f"{c.name} ({c.ip})" for c in CAMERAS))
    if DSM:
        log.info("Surveillance Station: %s (Home Mode follows the phones)", DSM.url)
    log.info("Away delay:\n%s", learner.stats())
    while True:
        now = time.monotonic()
        for ip, mac in PHONES:
            key = phone_key(ip, mac)
            if not phone_seen(ip, mac):
                continue
            prev = last_seen.get(key)
            if prev is not None:
                silence = now - prev
                # Missed at least one check but came back: learn from it.
                # Longer than AWAY_MAX = a real absence, not a sleeping phone.
                if INTERVAL * 1.5 < silence <= AWAY_MAX:
                    learner.record(key, silence)
            last_seen[key] = now

        # Home if any phone answered within its own away delay. A phone not
        # seen since startup counts as away right away.
        present = any(now - seen < learner.delay(key) for key, seen in last_seen.items())

        if present != state:
            log.info("Phone %s", "connected" if present else "away")
            state = present
        for cam in CAMERAS:
            # A failed camera is retried on the next iteration, the others
            # are not moved again.
            if cam_state[cam.name] != present and apply_camera(cam, present):
                cam_state[cam.name] = present
        if DSM and dsm_state != present and apply_dsm(DSM, present):
            dsm_state = present

        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
