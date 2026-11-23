"""Synology Surveillance Station Home Mode, against a fake DSM Web API."""

import io
import json
import ssl
import urllib.parse

import pytest

DSM_URL = "https://nas.local:5001"


class FakeDsm:
    """Answers urlopen() like DSM. `fail[(api, method)] = code` makes a call fail."""

    def __init__(self, home_mode_installed=True):
        self.calls = []  # (url, params)
        self.contexts = []
        self.fail = {}
        self.home_mode = False
        self.home_mode_installed = home_mode_installed

    def urlopen(self, request, timeout=None, context=None):
        params = dict(urllib.parse.parse_qsl(request.data.decode()))
        self.calls.append((request.full_url, params))
        self.contexts.append(context)
        key = (params["api"], params["method"])
        if key in self.fail:
            return self._reply({"success": False, "error": {"code": self.fail[key]}})
        if key == ("SYNO.API.Info", "query"):
            data = {"SYNO.API.Auth": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 7}}
            if self.home_mode_installed:
                data["SYNO.SurveillanceStation.HomeMode"] = {
                    "path": "entry.cgi",
                    "minVersion": 1,
                    "maxVersion": 1,
                }
            return self._reply({"success": True, "data": data})
        if key == ("SYNO.API.Auth", "login"):
            return self._reply({"success": True, "data": {"sid": "SID42"}})
        if key == ("SYNO.API.Auth", "logout"):
            return self._reply({"success": True})
        assert params["_sid"] == "SID42"
        if key == ("SYNO.SurveillanceStation.HomeMode", "Switch"):
            self.home_mode = params["on"] == "true"
            return self._reply({"success": True})
        if key == ("SYNO.SurveillanceStation.HomeMode", "GetInfo"):
            return self._reply({"success": True, "data": {"on": self.home_mode}})
        raise AssertionError(f"unexpected call {key}")

    @staticmethod
    def _reply(body):
        return io.BytesIO(json.dumps(body).encode())

    def methods(self):
        return [(p["api"], p["method"]) for _, p in self.calls]


@pytest.fixture
def dsm(tp, monkeypatch):
    fake = FakeDsm()
    monkeypatch.setattr(tp.urllib.request, "urlopen", fake.urlopen)
    return fake


@pytest.fixture
def station(tp):
    return tp.SurveillanceStation(DSM_URL, "watcher", "p@ss$word")


def test_switch_logs_in_switches_and_logs_out(tp, dsm, station):
    tp.set_home_mode(station, True)
    assert dsm.home_mode is True
    assert dsm.methods() == [
        ("SYNO.API.Info", "query"),
        ("SYNO.API.Auth", "login"),
        ("SYNO.SurveillanceStation.HomeMode", "Switch"),
        ("SYNO.API.Auth", "logout"),
    ]
    tp.set_home_mode(station, False)
    assert dsm.home_mode is False


def test_password_is_posted_never_in_the_url(tp, dsm, station):
    tp.set_home_mode(station, True)
    login_url, login = dsm.calls[1]
    assert login["passwd"] == "p@ss$word"
    assert login["account"] == "watcher"
    assert login["version"] == "6"  # capped: format=sid is stable up to v6
    for url, _ in dsm.calls:
        assert url.startswith(f"{DSM_URL}/webapi/")
        assert "?" not in url


def test_get_home_mode(tp, dsm, station):
    dsm.home_mode = True
    assert tp.get_home_mode(station) is True


def test_wrong_password_is_explained(tp, dsm, station):
    dsm.fail[("SYNO.API.Auth", "login")] = 400
    with pytest.raises(tp.DsmError, match="wrong username or password"):
        tp.set_home_mode(station, True)


def test_logout_even_when_switch_fails(tp, dsm, station):
    dsm.fail[("SYNO.SurveillanceStation.HomeMode", "Switch")] = 105
    with pytest.raises(tp.DsmError, match="Surveillance Station manager"):
        tp.set_home_mode(station, True)
    assert dsm.methods()[-1] == ("SYNO.API.Auth", "logout")


def test_missing_surveillance_station(tp, monkeypatch, station):
    fake = FakeDsm(home_mode_installed=False)
    monkeypatch.setattr(tp.urllib.request, "urlopen", fake.urlopen)
    with pytest.raises(tp.DsmError, match="Surveillance Station not found"):
        tp.set_home_mode(station, True)
    assert fake.methods() == [("SYNO.API.Info", "query")]  # no login attempted


def test_apply_dsm_reports_failure_without_raising(tp, dsm, station):
    dsm.fail[("SYNO.API.Auth", "login")] = 403
    assert tp.apply_dsm(station, True) is False
    del dsm.fail[("SYNO.API.Auth", "login")]
    assert tp.apply_dsm(station, True) is True


def test_certificate_checked_unless_disabled(tp, dsm):
    tp.set_home_mode(tp.SurveillanceStation(DSM_URL, "u", "p"), True)
    assert set(dsm.contexts) == {None}  # default context: certificate verified

    dsm.contexts.clear()
    tp.set_home_mode(tp.SurveillanceStation(DSM_URL, "u", "p", verify_ssl=False), True)
    assert all(c.verify_mode == ssl.CERT_NONE and not c.check_hostname for c in dsm.contexts)


def test_apply_state_includes_dsm(tp, monkeypatch, station):
    applied = []
    monkeypatch.setattr(tp, "apply_camera", lambda cam, home: applied.append("cam") or True)
    monkeypatch.setattr(tp, "apply_dsm", lambda dsm, home: applied.append("dsm") or False)
    assert tp.apply_state(True, dsm=station) is False  # every target tried, failure reported
    assert applied == ["cam", "dsm"]


# ---------------------------------------------------------------- config


def test_dsm_disabled_without_url(tp, monkeypatch):
    monkeypatch.delenv("DSM_URL", raising=False)
    assert tp.parse_dsm() is None


def test_dsm_config(tp, monkeypatch):
    monkeypatch.setenv("DSM_URL", "https://192.168.1.10:5001/")
    monkeypatch.setenv("DSM_USER", "watcher")
    monkeypatch.setenv("DSM_PASSWORD", "secret")
    monkeypatch.setenv("DSM_VERIFY_SSL", "0")
    assert tp.parse_dsm() == tp.SurveillanceStation(
        "https://192.168.1.10:5001", "watcher", "secret", False
    )


def test_dsm_url_needs_a_scheme(tp, monkeypatch):
    monkeypatch.setenv("DSM_URL", "192.168.1.10:5001")
    with pytest.raises(SystemExit, match="http"):
        tp.parse_dsm()


def test_dsm_needs_credentials(tp, monkeypatch):
    monkeypatch.setenv("DSM_URL", "https://192.168.1.10:5001")
    monkeypatch.delenv("DSM_USER", raising=False)
    with pytest.raises(SystemExit, match="DSM_USER"):
        tp.parse_dsm()
