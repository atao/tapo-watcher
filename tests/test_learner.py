import json

KEY = "aa:bb:cc:dd:ee:01"


def test_default_delay_until_enough_samples(tp, monkeypatch):
    monkeypatch.setattr(tp, "AWAY_AFTER_MISSES", 2)
    monkeypatch.setattr(tp, "INTERVAL", 30)
    monkeypatch.setattr(tp, "ADAPTIVE_MIN_SAMPLES", 5)
    learner = tp.DelayLearner(tp.STATE_FILE)
    for _ in range(4):
        learner.record(KEY, 300)
    assert learner.delay(KEY) == 60


def test_learned_delay_is_p95_times_margin(tp, monkeypatch):
    monkeypatch.setattr(tp, "ADAPTIVE_MARGIN", 1.2)
    learner = tp.DelayLearner(tp.STATE_FILE)
    for _ in range(5):
        learner.record(KEY, 150)
    assert learner.delay(KEY) == 180


def test_learned_delay_is_clamped(tp, monkeypatch):
    monkeypatch.setattr(tp, "AWAY_MIN", 60)
    monkeypatch.setattr(tp, "AWAY_MAX", 1200)
    learner = tp.DelayLearner(tp.STATE_FILE)
    for _ in range(5):
        learner.record("short", 40)
        learner.record("long", 1100)
    assert learner.delay("short") == 60
    assert learner.delay("long") == 1200


def test_p95_ignores_rare_outliers(tp):
    learner = tp.DelayLearner(tp.STATE_FILE)
    for _ in range(99):
        learner.record(KEY, 100)
    learner.record(KEY, 1000)
    assert learner.p95(KEY) == 100


def test_adaptive_disabled_uses_fixed_delay(tp, monkeypatch):
    monkeypatch.setattr(tp, "ADAPTIVE", False)
    monkeypatch.setattr(tp, "AWAY_AFTER_MISSES", 6)
    learner = tp.DelayLearner(tp.STATE_FILE)
    for _ in range(10):
        learner.record(KEY, 600)
    assert learner.delay(KEY) == 180


def test_history_is_capped(tp, monkeypatch):
    monkeypatch.setattr(tp, "ADAPTIVE_HISTORY", 10)
    learner = tp.DelayLearner(tp.STATE_FILE)
    for i in range(25):
        learner.record(KEY, 100 + i)
    assert learner.silences[KEY] == list(range(115, 125))


def test_state_persists_across_instances(tp):
    learner = tp.DelayLearner(tp.STATE_FILE)
    for _ in range(5):
        learner.record(KEY, 150)

    with open(tp.STATE_FILE) as fh:
        assert json.load(fh) == {"silences": {KEY: [150] * 5}}
    assert tp.DelayLearner(tp.STATE_FILE).delay(KEY) == 180


def test_corrupt_state_file_starts_empty(tp):
    with open(tp.STATE_FILE, "w") as fh:
        fh.write("{not json")
    assert tp.DelayLearner(tp.STATE_FILE).silences == {}


def test_stats_mentions_each_phone(tp):
    learner = tp.DelayLearner(tp.STATE_FILE)
    assert KEY in learner.stats()
    assert "(default)" in learner.stats()
