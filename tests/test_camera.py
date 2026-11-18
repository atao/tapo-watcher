import pytest


class FakeTapo:
    def __init__(self, presets=None):
        self.presets = presets if presets is not None else {"1": "Home", "2": "Away"}
        self.calls = []

    def getPresets(self):
        return self.presets

    def setPreset(self, pid):
        self.calls.append(("setPreset", pid))

    def moveMotor(self, x, y):
        self.calls.append(("moveMotor", x, y))

    def setPrivacyMode(self, enabled):
        self.calls.append(("setPrivacyMode", enabled))


@pytest.fixture
def cam(tp, monkeypatch):
    fake = FakeTapo()
    monkeypatch.setattr(tp, "camera", lambda: fake)
    return fake


def test_find_preset_by_id_or_name(tp):
    tapo = FakeTapo()
    assert tp.find_preset_id(tapo, "2") == "2"
    assert tp.find_preset_id(tapo, "home") == "1"
    with pytest.raises(ValueError, match="not found"):
        tp.find_preset_id(tapo, "Garage")


def test_preset_action_disables_privacy_first(tp, cam):
    tp.run_action("preset:Away")
    assert cam.calls == [("setPrivacyMode", False), ("setPreset", "2")]


def test_move_action(tp, cam):
    tp.run_action("move:90,-10")
    assert cam.calls == [("setPrivacyMode", False), ("moveMotor", 90, -10)]


@pytest.mark.parametrize("arg, expected", [("on", True), ("yes", True), ("off", False)])
def test_privacy_action(tp, cam, arg, expected):
    tp.run_action(f"privacy:{arg}")
    assert cam.calls == [("setPrivacyMode", expected)]


@pytest.mark.parametrize("action", ["none", "", "  "])
def test_none_action_does_not_touch_camera(tp, monkeypatch, action):
    monkeypatch.setattr(tp, "camera", lambda: pytest.fail("camera() called"))
    tp.run_action(action)


def test_unknown_action_raises(tp, cam):
    with pytest.raises(ValueError, match="Unknown action"):
        tp.run_action("dance:now")


def test_apply_state_reports_failure(tp, monkeypatch):
    def boom():
        raise ConnectionError("camera unreachable")

    monkeypatch.setattr(tp, "camera", boom)
    assert tp.apply_state(True) is False


def test_apply_state_uses_home_and_away_actions(tp, cam):
    assert tp.apply_state(True)
    assert tp.apply_state(False)
    assert ("setPreset", "1") in cam.calls
    assert cam.calls[-1] == ("setPreset", "2")
