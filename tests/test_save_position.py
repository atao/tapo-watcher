"""--save-home / --save-away: replace a preset with the current position."""

import os
import stat

import pytest


class PresetCamera:
    """Fake Tapo keeping presets like the camera: new id on each save."""

    def __init__(self, presets):
        self.presets = dict(presets)
        self.calls = []
        self.save_fails = False

    def getPresets(self):
        return dict(self.presets)

    def savePreset(self, name):
        self.calls.append(("savePreset", name))
        if not self.save_fails:
            self.presets[str(max(map(int, self.presets), default=0) + 1)] = name

    def deletePreset(self, pid):
        self.calls.append(("deletePreset", pid))
        del self.presets[pid]


@pytest.fixture
def env_file(tp, monkeypatch, tmp_path):
    path = tmp_path / "tapo.env"
    path.write_text(
        "# config\nCAMERA_IP=192.168.1.100\nCAMERA_PASSWORD='a$b'\n"
        "ACTION_HOME=preset:Mur\nACTION_AWAY=preset:Entree\n"
    )
    os.chmod(path, 0o600)
    monkeypatch.setattr(tp, "ENV_FILE", str(path))
    monkeypatch.delenv("CAMERAS", raising=False)
    return path


@pytest.fixture
def fake(tp, monkeypatch):
    cam = PresetCamera({"1": "Mur", "2": "Entree"})
    monkeypatch.setattr(tp, "camera", lambda c=None: cam)
    return cam


def cam_cfg(tp, home="preset:Mur", away="preset:Entree", name="camera"):
    return tp.Camera(name, "192.168.1.100", "u", "p", home, away)


def test_overwrites_the_configured_preset(tp, fake, env_file):
    before = env_file.read_text()
    assert tp.save_position(cam_cfg(tp), home=True) == "Mur"
    assert fake.presets == {"2": "Entree", "3": "Mur"}
    # saved before deleting: a failed save never loses the old preset
    assert fake.calls == [("savePreset", "Mur"), ("deletePreset", "1")]
    assert env_file.read_text() == before  # name unchanged: config untouched


def test_new_name_updates_tapo_env(tp, fake, env_file):
    tp.save_position(cam_cfg(tp), home=False, name="Porte")
    assert fake.presets == {"1": "Mur", "2": "Entree", "3": "Porte"}
    text = env_file.read_text()
    assert "ACTION_AWAY=preset:Porte\n" in text
    assert "ACTION_HOME=preset:Mur\n" in text
    assert "CAMERA_PASSWORD='a$b'\n" in text  # rest of the file kept as is
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600


def test_default_name_when_action_is_not_a_preset(tp, fake, env_file):
    assert tp.save_position(cam_cfg(tp, home="privacy:on"), home=True) == "Home"
    assert "ACTION_HOME=preset:Home\n" in env_file.read_text()


def test_refuses_the_other_states_preset(tp, fake, env_file):
    with pytest.raises(SystemExit, match="away preset"):
        tp.save_position(cam_cfg(tp), home=True, name="entree")
    assert fake.calls == []


def test_failed_save_keeps_the_old_preset(tp, fake, env_file):
    fake.save_fails = True
    with pytest.raises(RuntimeError, match="not saved"):
        tp.save_position(cam_cfg(tp), home=True)
    assert fake.presets == {"1": "Mur", "2": "Entree"}


def test_multi_camera_writes_the_camera_key(tp, fake, env_file, monkeypatch):
    monkeypatch.setenv("CAMERAS", "salon,living-room")
    tp.save_position(cam_cfg(tp, name="living-room"), home=True, name="Canape")
    assert "CAMERA_LIVING_ROOM_ACTION_HOME=preset:Canape\n" in env_file.read_text()


def test_set_env_value_replaces_or_appends(tp, tmp_path):
    path = tmp_path / "x.env"
    path.write_text("A=1\n  B=2\nA=3")
    tp.set_env_value("A", "9", str(path))
    tp.set_env_value("C", "4", str(path))
    assert path.read_text() == "A=9\n  B=2\nA=9\nC=4\n"


def test_cli_needs_camera_with_several(tp, monkeypatch, fake, env_file):
    monkeypatch.setattr(tp, "CAMERAS", [cam_cfg(tp, name="a"), cam_cfg(tp, name="b")])
    monkeypatch.setattr(tp.sys, "argv", ["tapo_watcher.py", "--save-home"])
    with pytest.raises(SystemExit, match="--camera"):
        tp.main()
    monkeypatch.setattr(tp.sys, "argv", ["tapo_watcher.py", "--save-away", "--camera", "b"])
    tp.main()
    assert fake.calls[0] == ("savePreset", "Entree")
