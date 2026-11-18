#!/usr/bin/env python3
"""
Watch whether phones are connected to the local Wi-Fi and move a TP-Link
Tapo C210 camera accordingly.

  - at least one phone home  -> ACTION_HOME (e.g. turn the camera to the wall)
  - all phones away          -> ACTION_AWAY (e.g. turn back to the entrance)

The action only runs when the state changes, not on every check.

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

Adaptive away delay:
  A sleeping iPhone can stay silent on the network for minutes. Every time a
  phone goes silent and then answers again (shorter than AWAY_MAX), the
  silence is recorded in STATE_FILE. The away delay of each phone is then
  the 95th percentile of its recorded silences x ADAPTIVE_MARGIN, clamped to
  [AWAY_MIN, AWAY_MAX]. Until ADAPTIVE_MIN_SAMPLES silences are recorded,
  AWAY_AFTER_MISSES x INTERVAL is used.
"""

import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time

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
load_env_file(
    os.environ.get(
        "TAPO_ENV_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "tapo.env"),
    )
)


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


PHONES = parse_phones()
CAMERA_IP = env("CAMERA_IP", required=True)
CAMERA_USER = env("CAMERA_USER", "admin")
CAMERA_PASSWORD = env("CAMERA_PASSWORD", "")
ACTION_HOME = env("ACTION_HOME", "preset:Home")
ACTION_AWAY = env("ACTION_AWAY", "preset:Away")
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
ADAPTIVE_MIN_SAMPLES = int(env("ADAPTIVE_MIN_SAMPLES", "5"))
ADAPTIVE_HISTORY = 200  # silences kept per phone (most recent)
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
        learned = self.p95(key) * ADAPTIVE_MARGIN
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

    If it is not there, ping the camera's whole /24 (in parallel, ~1 s) to
    trigger ARP requests, then look again.
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
    prefix = CAMERA_IP.rsplit(".", 1)[0]
    procs = [
        subprocess.Popen(
            ["ping", "-c", "1", "-W", "1", f"{prefix}.{i}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for i in range(1, 255)
    ]
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
def camera():
    try:
        from pytapo import Tapo
    except ImportError:
        sys.exit("pytapo is not installed: run with `uv run tapo_watcher.py` (or `uv sync` first)")
    return Tapo(CAMERA_IP, CAMERA_USER, CAMERA_PASSWORD)


def find_preset_id(tapo, wanted: str) -> str:
    presets = tapo.getPresets()  # {id: name}
    if wanted in presets:
        return wanted
    for pid, name in presets.items():
        if name.lower() == wanted.lower():
            return pid
    raise ValueError(f"Preset '{wanted}' not found. Available: {presets}")


def run_action(action: str):
    action = action.strip()
    if not action or action == "none":
        return
    kind, _, arg = action.partition(":")
    tapo = camera()

    if kind == "preset":
        pid = find_preset_id(tapo, arg)
        log.info("Camera -> preset %s (%s)", arg, pid)
        # Privacy mode blocks motion
        tapo.setPrivacyMode(False)
        tapo.setPreset(pid)
    elif kind == "move":
        x, y = (int(v) for v in arg.split(","))
        log.info("Camera -> move x=%d y=%d", x, y)
        tapo.setPrivacyMode(False)
        tapo.moveMotor(x, y)
    elif kind == "privacy":
        enabled = arg.lower() in ("on", "1", "true", "yes")
        log.info("Camera -> privacy mode %s", "ON" if enabled else "OFF")
        tapo.setPrivacyMode(enabled)
    else:
        raise ValueError(f"Unknown action: {action}")


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def apply_state(home: bool):
    try:
        run_action(ACTION_HOME if home else ACTION_AWAY)
        return True
    except Exception as exc:  # camera unreachable, wrong password...
        log.error("Camera action failed: %s", exc)
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--presets", action="store_true")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    learner = DelayLearner(STATE_FILE)

    if args.stats:
        print(learner.stats())
        return
    if args.presets:
        print(camera().getPresets())
        return
    if args.check:
        present = any_phone_connected()
        print("connected" if present else "away")
        sys.exit(0 if present else 1)
    if args.once:
        present = any_phone_connected()
        log.info("Phone %s", "connected" if present else "away")
        sys.exit(0 if apply_state(present) else 1)

    camera()  # fail fast if pytapo is missing, not minutes later on first move

    state = None  # unknown at startup -> apply the first state
    last_seen = {}  # phone key -> time.monotonic() of the last answer
    log.info("Watching %s every %ds", ", ".join(ip for ip, _ in PHONES), INTERVAL)
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
            if apply_state(present):
                state = present
            # otherwise retry on the next iteration

        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
