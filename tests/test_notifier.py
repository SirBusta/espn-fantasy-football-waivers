"""Tests for the notifier, using a fake ESPN league and a fake mail server."""

import sys
import types
from datetime import datetime
from pathlib import Path

import pytest
from espn_api.football.activity import Activity

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import notifier  # noqa: E402


class Team:
    def __init__(self, team_id, name):
        self.team_id, self.team_name, self.roster = team_id, name, []


class Player:
    def __init__(self, player_id, name, pos, pro):
        self.playerId, self.name, self.position, self.proTeam = player_id, name, pos, pro


TEAMS = {1: Team(1, "Taco Corp"), 2: Team(2, "Gridiron Gang")}
PLAYERS = {
    10: Player(10, "Puka Nacua", "WR", "LAR"),
    11: Player(11, "Zack Moss", "RB", "CIN"),
    12: Player(12, "Tyler Higbee", "TE", "LAR"),
    13: Player(13, "Jaylen Waddle", "WR", "MIA"),
}


def activity(date, messages):
    return Activity({"date": date, "messages": messages}, {}, TEAMS.get,
                    lambda playerId: PLAYERS.get(playerId))


def waiver(date, team, player, bid=0, dropped=None):
    msgs = [{"messageTypeId": 180, "to": team, "from": bid, "targetId": player}]
    if dropped:
        msgs.append({"messageTypeId": 179, "to": team, "targetId": dropped})
    return activity(date, msgs)


class FakeESPN:
    def __init__(self):
        self.activities = []
        self.fail = False

    def league(self, **_):
        if self.fail:
            raise Exception("401 access denied")
        espn = self

        class League:
            settings = types.SimpleNamespace(name="Test League", faab=True)

            def recent_activity(self, size):
                return list(espn.activities)

        return League()


class FakeMail:
    def __init__(self):
        self.sent = []
        self.fail = False

    def smtp(self, *_, **__):
        mail = self

        class SMTP:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def starttls(self, context):
                pass

            def login(self, user, password):
                pass

            def send_message(self, msg):
                if mail.fail:
                    raise OSError("smtp down")
                mail.sent.append(msg)

        return SMTP()

    @property
    def subjects(self):
        return [m["Subject"] for m in self.sent]


@pytest.fixture
def env(monkeypatch, tmp_path):
    for key in ["CHECK_TIMES", "CHECK_DAYS", "NOTIFY_TYPES", "NOTIFY_WHEN_EMPTY",
                "ALERT_AFTER_FAILURES", "SEND_ON_FIRST_RUN", "TZ"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LEAGUE_ID", "123")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("EMAIL_FROM", "me@example.com")
    monkeypatch.setenv("EMAIL_TO", "a@example.com,b@example.com")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    return monkeypatch


@pytest.fixture
def espn(monkeypatch):
    fake = FakeESPN()
    monkeypatch.setattr(notifier, "League", fake.league)
    return fake


@pytest.fixture
def mail(monkeypatch):
    fake = FakeMail()
    monkeypatch.setattr(notifier.smtplib, "SMTP", fake.smtp)
    return fake


def make(env):
    cfg = notifier.Config.from_env()
    return cfg, notifier.State(cfg.data_dir / "state.json")


def test_first_run_is_silent_then_new_claims_are_emailed(env, espn, mail):
    espn.activities = [waiver(1, 1, 10, bid=17, dropped=11)]
    cfg, state = make(env)
    assert notifier.poll_once(cfg, state)
    assert mail.sent == []

    espn.activities.append(waiver(2, 2, 12, bid=3))
    assert notifier.poll_once(cfg, state)
    assert mail.subjects == ["[Test League] Waivers processed: 1 claim"]
    assert mail.sent[0]["To"] == "a@example.com, b@example.com"
    body = mail.sent[0].get_body(("plain",)).get_content()
    assert "Gridiron Gang: Added (waivers) Tyler Higbee (TE, LAR)  ($3 FAAB)" in body
    assert "Puka Nacua" not in body  # already seen on the first run

    assert notifier.poll_once(cfg, state)
    assert len(mail.sent) == 1  # no duplicates


def test_state_survives_restart(env, espn, mail):
    espn.activities = [waiver(1, 1, 10)]
    cfg, state = make(env)
    notifier.poll_once(cfg, state)
    _, restarted = make(env)
    assert restarted.initialized
    notifier.poll_once(cfg, restarted)
    assert mail.sent == []


def test_failed_send_is_retried(env, espn, mail):
    cfg, state = make(env)
    notifier.poll_once(cfg, state)
    espn.activities.append(waiver(2, 2, 12))
    mail.fail = True
    assert not notifier.poll_once(cfg, state)
    mail.fail = False
    assert notifier.poll_once(cfg, state)
    assert len(mail.sent) == 1


def test_notify_types_filter(env, espn, mail):
    trade = activity(3, [{"messageTypeId": 244, "from": 1, "to": 2, "targetId": 13}])
    fa_add = activity(4, [{"messageTypeId": 178, "to": 1, "targetId": 11}])

    cfg, state = make(env)  # default: waivers only
    notifier.poll_once(cfg, state)
    espn.activities += [trade, fa_add]
    notifier.poll_once(cfg, state)
    assert mail.sent == []

    env.setenv("NOTIFY_TYPES", "WAIVER,TRADE")
    env.setenv("DATA_DIR", str(Path(cfg.data_dir) / "second"))
    espn.activities = []
    cfg, state = make(env)
    notifier.poll_once(cfg, state)
    espn.activities += [trade, fa_add]
    notifier.poll_once(cfg, state)
    assert mail.subjects == ["[Test League] Trade alert (1)"]
    body = mail.sent[0].get_body(("plain",)).get_content()
    assert "Taco Corp: Traded away Jaylen Waddle" in body
    assert "Zack Moss" not in body


def test_one_alert_after_repeated_failures(env, espn, mail):
    env.setenv("ALERT_AFTER_FAILURES", "2")
    cfg, state = make(env)
    notifier.poll_once(cfg, state)
    espn.fail = True
    for _ in range(4):
        assert not notifier.poll_once(cfg, state)
    assert mail.subjects == ["[ESPN Fantasy Football Waivers] Can't reach your league"]
    espn.fail = False
    assert notifier.poll_once(cfg, state)
    assert state.consecutive_failures == 0 and not state.alert_sent


@pytest.mark.parametrize("extra, expected", [
    ({"CHECK_TIMES": "09:15"}, ["No waiver claims today", "No waiver claims today",
                                "Waivers processed: 1 claim"]),
    ({"CHECK_TIMES": "09:15", "NOTIFY_WHEN_EMPTY": "false"}, ["Waivers processed: 1 claim"]),
    ({}, ["Waivers processed: 1 claim"]),  # every-N-minutes mode never sends "nothing" emails
])
def test_nothing_new_email(env, espn, mail, extra, expected):
    for key, value in extra.items():
        env.setenv(key, value)
    espn.activities = [waiver(1, 1, 10)]
    cfg, state = make(env)
    notifier.poll_once(cfg, state)  # first run: silent
    notifier.poll_once(cfg, state)  # nothing new
    espn.activities.append(activity(2, [{"messageTypeId": 178, "to": 1, "targetId": 11}]))
    notifier.poll_once(cfg, state)  # free-agent add only: still no waiver claims
    espn.activities.append(waiver(3, 1, 12))
    notifier.poll_once(cfg, state)
    assert [s.split("] ", 1)[1] for s in mail.subjects] == expected


@pytest.mark.parametrize("now, expected", [
    ((2026, 11, 4, 8, 0), "Wed 2026-11-04 09:15 PST"),   # before 9:15 -> today
    ((2026, 11, 4, 9, 15), "Thu 2026-11-05 09:15 PST"),  # exactly 9:15 -> tomorrow
    ((2026, 11, 8, 10, 0), "Wed 2026-11-11 09:15 PST"),  # Sunday after -> skip Mon/Tue
    ((2027, 3, 13, 10, 0), "Sun 2027-03-14 09:15 PDT"),  # across daylight saving
])
def test_next_run(env, now, expected):
    env.setenv("CHECK_TIMES", "09:15")
    env.setenv("CHECK_DAYS", "Wed,Thu,Fri,Sat,Sun")
    cfg = notifier.Config.from_env()
    got = notifier.next_run(cfg, datetime(*now, tzinfo=cfg.timezone))
    assert got.strftime("%a %Y-%m-%d %H:%M %Z") == expected


def test_retries(env, monkeypatch):
    env.setenv("CHECK_TIMES", "09:15")
    cfg = notifier.Config.from_env()
    assert cfg.alert_after_failures == 4
    results = iter([False, False, True])
    calls, sleeps = [], []
    monkeypatch.setattr(notifier, "poll_once", lambda c, s: calls.append(1) or next(results))
    monkeypatch.setattr(notifier.time, "sleep", sleeps.append)
    notifier.poll_with_retries(cfg, None)
    assert len(calls) == 3 and sleeps == [900, 900]


def test_requests_get_a_timeout(monkeypatch):
    seen = {}
    monkeypatch.setattr(notifier, "_original_request", lambda self, m, u, **kw: seen.update(kw))
    notifier.requests.get("https://example.invalid")
    assert seen["timeout"] == 60


def test_bad_settings_exit(env):
    env.setenv("CHECK_TIMES", "9am")
    with pytest.raises(SystemExit):
        notifier.Config.from_env()
