"""Test setup: isolate the module from the real tapo.env, network and camera."""

import os
import sys
import tempfile

import pytest

# Must happen before tapo_watcher is imported: it reads its config at import.
_TMP = tempfile.mkdtemp(prefix="tapo-watcher-tests-")
os.environ["TAPO_ENV_FILE"] = os.path.join(_TMP, "missing.env")  # never load the real tapo.env
os.environ["PHONES"] = "192.168.1.50=aa:bb:cc:dd:ee:01"
os.environ["CAMERA_IP"] = "192.168.1.100"
os.environ["CAMERA_USER"] = "user"
os.environ["CAMERA_PASSWORD"] = "secret"
os.environ["ACTION_HOME"] = "preset:Home"
os.environ["ACTION_AWAY"] = "preset:Away"
os.environ["INTERVAL"] = "30"
os.environ["AWAY_AFTER_MISSES"] = "2"
os.environ["STATE_FILE"] = os.path.join(_TMP, "state.json")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tapo_watcher  # noqa: E402


@pytest.fixture
def tp(monkeypatch, tmp_path):
    """The module with a fresh IP cache and a per-test state file."""
    monkeypatch.setattr(tapo_watcher, "_known_ip", {})
    monkeypatch.setattr(tapo_watcher, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(tapo_watcher.time, "sleep", lambda s: None)
    return tapo_watcher


class FakeProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture
def net(monkeypatch, tp):
    """Fake network: set `net.ping[ip]` (bool) and `net.neigh[ip]` (ip neigh line)."""

    class Net:
        ping = {}
        neigh = {}
        swept = False

    n = Net()

    def run(cmd, **kwargs):
        if cmd[0] == "ping":
            return FakeProc(0 if n.ping.get(cmd[-1]) else 1)
        if cmd[:2] == ["ip", "neigh"]:
            if len(cmd) == 4:  # ip neigh show <ip>
                line = n.neigh.get(cmd[3], "")
                return FakeProc(0, f"{cmd[3]} {line}" if line else "")
            return FakeProc(0, "\n".join(f"{ip} {line}" for ip, line in n.neigh.items()))
        raise AssertionError(f"unexpected command {cmd}")

    class Popen:
        def __init__(self, cmd, **kwargs):
            n.swept = True

        def wait(self):
            return 0

    class FakeSocket:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def sendto(self, data, addr):
            pass

    monkeypatch.setattr(tp.subprocess, "run", run)
    monkeypatch.setattr(tp.subprocess, "Popen", Popen)
    monkeypatch.setattr(tp.socket, "socket", FakeSocket)
    return n
