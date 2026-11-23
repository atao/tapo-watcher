"""Simulate the main loop with a fake clock and a scripted phone."""

import pytest


class Stop(Exception):
    pass


def run_loop(tp, monkeypatch, timeline, fail_actions=0, cameras=None):
    """Run main() over `timeline` (one bool per check). Returns camera moves.

    `fail_actions` maps a camera name to the number of actions that fail
    (an int applies to the first camera)."""
    cameras = cameras or [tp.Camera("cam", "192.168.1.100", "u", "p", "home", "away")]
    clock = [0.0]
    answers = iter(timeline)
    moves = []
    if isinstance(fail_actions, int):
        fail_actions = {cameras[0].name: fail_actions}
    failures = dict(fail_actions)

    def apply_camera(cam, home):
        if failures.get(cam.name):
            failures[cam.name] -= 1
            return False
        state = "HOME" if home else "AWAY"
        moves.append(
            (int(clock[0]), state) if len(cameras) == 1 else (int(clock[0]), cam.name, state)
        )
        return True

    def sleep(seconds):
        clock[0] += seconds

    def phone_seen(ip, mac):
        try:
            return next(answers)
        except StopIteration:
            raise Stop from None

    monkeypatch.setattr(tp, "phone_seen", phone_seen)
    monkeypatch.setattr(tp, "apply_camera", apply_camera)
    monkeypatch.setattr(tp, "tapo_class", lambda: None)
    monkeypatch.setattr(tp, "CAMERAS", cameras)
    monkeypatch.setattr(tp.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tp.time, "sleep", sleep)
    monkeypatch.setattr(tp, "PHONES", [("192.168.1.50", "aa:bb:cc:dd:ee:01")])
    monkeypatch.setattr(tp, "INTERVAL", 30)
    monkeypatch.setattr(tp, "AWAY_AFTER_MISSES", 2)
    monkeypatch.setattr(tp, "AWAY_MIN", 60)
    monkeypatch.setattr(tp.sys, "argv", ["tapo_watcher.py"])
    with pytest.raises(Stop):
        tp.main()
    return moves


def test_startup_applies_current_state(tp, monkeypatch):
    assert run_loop(tp, monkeypatch, [True, True]) == [(0, "HOME")]


def test_startup_without_phone_goes_away_immediately(tp, monkeypatch):
    assert run_loop(tp, monkeypatch, [False, False]) == [(0, "AWAY")]


def test_short_silence_does_not_move_camera(tp, monkeypatch):
    # one missed check (30 s) < 60 s default delay
    assert run_loop(tp, monkeypatch, [True, False, True, True]) == [(0, "HOME")]


def test_departure_after_default_delay(tp, monkeypatch):
    moves = run_loop(tp, monkeypatch, [True] + [False] * 5)
    assert moves == [(0, "HOME"), (60, "AWAY")]


def test_learns_sleep_pattern_and_stops_false_departures(tp, monkeypatch):
    sleepy = ([True] * 3 + [False] * 4 + [True]) * 6  # silent 150 s, six times
    leave = [False] * 10
    moves = run_loop(tp, monkeypatch, sleepy + leave)

    false_departures = [m for m in moves[:-1] if m[1] == "AWAY"]
    assert len(false_departures) == 5  # learning phase (ADAPTIVE_MIN_SAMPLES)
    assert moves[-1][1] == "AWAY"  # the real departure is still detected
    assert tp.DelayLearner(tp.STATE_FILE).delay("aa:bb:cc:dd:ee:01") == 210


def test_long_absence_is_not_learned(tp, monkeypatch):
    monkeypatch.setattr(tp, "AWAY_MAX", 300)
    run_loop(tp, monkeypatch, [True] + [False] * 20 + [True])
    assert tp.DelayLearner(tp.STATE_FILE).silences == {}


def test_failed_camera_action_is_retried(tp, monkeypatch):
    moves = run_loop(tp, monkeypatch, [True, True, True], fail_actions=1)
    assert moves == [(30, "HOME")]


def test_every_camera_moves_on_state_change(tp, monkeypatch):
    cams = [
        tp.Camera("salon", "192.168.1.100", "u", "p", "home", "away"),
        tp.Camera("garden", "192.168.1.101", "u", "p", "home", "away"),
    ]
    moves = run_loop(tp, monkeypatch, [True, False, False, False], cameras=cams)
    assert moves == [
        (0, "salon", "HOME"),
        (0, "garden", "HOME"),
        (60, "salon", "AWAY"),
        (60, "garden", "AWAY"),
    ]


def test_failed_camera_is_retried_alone(tp, monkeypatch):
    cams = [
        tp.Camera("salon", "192.168.1.100", "u", "p", "home", "away"),
        tp.Camera("garden", "192.168.1.101", "u", "p", "home", "away"),
    ]
    moves = run_loop(tp, monkeypatch, [True, True, True], fail_actions={"garden": 1}, cameras=cams)
    assert moves == [(0, "salon", "HOME"), (30, "garden", "HOME")]


def test_dsm_home_mode_follows_state_and_is_retried(tp, monkeypatch):
    switches, failures = [], [1]

    def apply_dsm(dsm, home):
        if failures[0]:
            failures[0] -= 1
            return False
        switches.append(home)
        return True

    monkeypatch.setattr(tp, "DSM", tp.SurveillanceStation("https://nas:5001", "u", "p"))
    monkeypatch.setattr(tp, "apply_dsm", apply_dsm)
    moves = run_loop(tp, monkeypatch, [True, True, True, False, False, False, False])
    assert moves == [(0, "HOME"), (120, "AWAY")]
    # first switch failed, retried on the next check, not repeated after that
    assert switches == [True, False]
