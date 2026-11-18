import pytest


def test_load_env_file_parses_quotes_comments_and_dollar(tp, tmp_path, monkeypatch):
    env_file = tmp_path / "test.env"
    env_file.write_text(
        "# comment\n"
        "\n"
        "T_PLAIN=value\n"
        "T_SINGLE='$Secret44'\n"
        'T_DOUBLE="with spaces"\n'
        "T_EQUALS=a=b\n"
        "not a variable\n"
    )
    for key in ("T_PLAIN", "T_SINGLE", "T_DOUBLE", "T_EQUALS"):
        monkeypatch.delenv(key, raising=False)

    tp.load_env_file(str(env_file))

    import os

    assert os.environ["T_PLAIN"] == "value"
    assert os.environ["T_SINGLE"] == "$Secret44"  # no $ expansion
    assert os.environ["T_DOUBLE"] == "with spaces"
    assert os.environ["T_EQUALS"] == "a=b"


def test_load_env_file_does_not_override_existing_env(tp, tmp_path, monkeypatch):
    env_file = tmp_path / "test.env"
    env_file.write_text("T_KEEP=from_file\n")
    monkeypatch.setenv("T_KEEP", "from_env")

    tp.load_env_file(str(env_file))

    import os

    assert os.environ["T_KEEP"] == "from_env"


def test_load_env_file_missing_file_is_ignored(tp, tmp_path):
    tp.load_env_file(str(tmp_path / "nope.env"))


def test_parse_phones_list(tp, monkeypatch):
    monkeypatch.setenv(
        "PHONES", " 192.168.1.50=AA:BB:CC:DD:EE:01 , =AA:BB:CC:DD:EE:FF,192.168.1.9 "
    )
    assert tp.parse_phones() == [
        ("192.168.1.50", "aa:bb:cc:dd:ee:01"),
        ("", "aa:bb:cc:dd:ee:ff"),
        ("192.168.1.9", ""),
    ]


def test_parse_phones_legacy_single_phone(tp, monkeypatch):
    monkeypatch.delenv("PHONES", raising=False)
    monkeypatch.setenv("PHONE_IP", "192.168.1.50")
    monkeypatch.setenv("PHONE_MAC", "AA:BB:CC:DD:EE:FF")
    assert tp.parse_phones() == [("192.168.1.50", "aa:bb:cc:dd:ee:ff")]


def test_parse_phones_missing_exits(tp, monkeypatch):
    monkeypatch.delenv("PHONES", raising=False)
    monkeypatch.delenv("PHONE_IP", raising=False)
    with pytest.raises(SystemExit):
        tp.parse_phones()
