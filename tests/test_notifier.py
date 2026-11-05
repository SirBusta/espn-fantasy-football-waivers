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


MS = 1000


class FakeESPN:
    def __init__(self):
        self.activities = []
        self.fail = False
        self.missing_years = set()  # seasons ESPN hasn't created yet
        self.years = []  # seasons requested
        self.week, self.final_week, self.drafted = 9, 17, True
        self.last_waiver_ms = None
        self.next_waiver_ms = None

    def league(self, league_id, year, **_):
        if self.fail:
            raise Exception("401 access denied")
        self.years.append(year)
        if year in self.missing_years:
            raise notifier.ESPNInvalidLeague(f"League {league_id} does not exist")
        espn = self

        class Request:
            def league_get(self, params=None, headers=None, extend=""):
                return {
                    "scoringPeriodId": espn.week,
                    "draftDetail": {"drafted": espn.drafted},
                    "status": {
                        "finalScoringPeriod": espn.final_week,
                        "waiverLastExecutionDate": espn.last_waiver_ms,
                        "waiverNextExecutionDate": espn.next_waiver_ms,
                    },
                }

        class League:
            settings = types.SimpleNamespace(name="Test League", faab=True)
            espn_request = Request()

            def __init__(self):
                self.year = year

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
    # every-N-minutes mode never sends "nothing" emails
    ({"CHECK_TIMES": "interval"}, ["Waivers processed: 1 claim"]),
])
def test_nothing_new_email(env, espn, mail, extra, expected):
    for key, value in extra.items():
        env.setenv(key, value)
    espn.activities = [waiver(1, 1, 10)]
    cfg, state = make(env)
    notifier.poll_once(cfg, state)  # first run: silent
    notifier.poll_once(cfg, state, waiver_check=True)  # nothing new
    espn.activities.append(activity(2, [{"messageTypeId": 178, "to": 1, "targetId": 11}]))
    notifier.poll_once(cfg, state, waiver_check=True)  # free-agent add only: no claims
    espn.activities.append(waiver(3, 1, 12))
    notifier.poll_once(cfg, state, waiver_check=True)
    assert [s.split("] ", 1)[1] for s in mail.subjects] == expected


def test_check_modes(env):
    assert notifier.Config.from_env().check_mode == "auto"  # the default
    env.setenv("CHECK_TIMES", "interval")
    cfg = notifier.Config.from_env()
    assert cfg.check_mode == "interval" and not cfg.notify_when_empty
    env.setenv("CHECK_TIMES", "09:15,13:00")
    cfg = notifier.Config.from_env()
    assert cfg.check_mode == "fixed" and cfg.check_times == [(9, 15), (13, 0)]


def test_falls_back_to_last_season_until_renewed(env, espn, mail):
    this_year = datetime.now().year
    espn.missing_years = {this_year}
    cfg, state = make(env)
    _, _, status = notifier.fetch_events(cfg)
    assert espn.years == [this_year, this_year - 1] and status.season == this_year - 1


def test_wrong_league_id_is_not_mistaken_for_offseason(env, espn):
    espn.fail = True  # ESPN answers "access denied" for a league that isn't yours
    cfg, _ = make(env)
    with pytest.raises(Exception):
        notifier.fetch_events(cfg)


@pytest.mark.parametrize("week, final, drafted, phase", [
    (9, 17, True, "active"),
    (17, 17, True, "active"),   # final week: waivers still run
    (18, 17, True, "over"),
    (1, 17, False, "predraft"),
])
def test_season_phase(env, espn, week, final, drafted, phase):
    espn.week, espn.final_week, espn.drafted = week, final, drafted
    cfg, _ = make(env)
    assert notifier.fetch_events(cfg)[2].phase == phase


def test_season_over_email_once_then_quiet(env, espn, mail):
    env.setenv("CHECK_TIMES", "09:15")
    cfg, state = make(env)
    notifier.poll_once(cfg, state)  # first run, mid-season
    espn.week = 18  # the final week has ended
    notifier.poll_once(cfg, state, waiver_check=True)
    notifier.poll_once(cfg, state, waiver_check=True)
    assert mail.subjects == ["[Test League] Season's over"]
    espn.week, espn.drafted = 1, False  # next season, before the draft
    notifier.poll_once(cfg, state, waiver_check=True)
    espn.drafted = True  # drafted: the daily emails are back
    notifier.poll_once(cfg, state, waiver_check=True)
    assert mail.subjects[1:] == ["[Test League] No waiver claims today"]


def at(*args):
    return datetime(*args, tzinfo=notifier.ZoneInfo("America/Los_Angeles"))


def test_auto_waits_for_espn_to_process_the_run(env, espn, mail):
    run = at(2026, 11, 6, 9, 0)
    espn.last_waiver_ms = int(at(2026, 11, 5, 9, 0, 10).timestamp()) * MS  # yesterday's run
    cfg, state = make(env)
    notifier.poll_once(cfg, state)  # first run
    notifier.poll_once(cfg, state, waiver_check=True, expected_run=run)
    assert mail.sent == []  # today's run hasn't been processed: don't say "nothing"
    espn.last_waiver_ms = int(run.timestamp() + 10) * MS
    notifier.poll_once(cfg, state, waiver_check=True, expected_run=run)
    notifier.poll_once(cfg, state, waiver_check=True, expected_run=run)  # ESPN schedule stale
    assert mail.subjects == ["[Test League] No waiver claims today"]  # once per run


def test_plan_auto_check(env):
    cfg = notifier.Config.from_env()
    now = at(2026, 11, 6, 8, 0)

    def status(phase="active", next_waiver=at(2026, 11, 6, 9, 0)):
        return notifier.LeagueStatus(2026, phase, 9, 17, None, next_waiver)

    assert cfg.check_mode == "auto"
    assert notifier.plan_auto_check(cfg, status(), now) == (at(2026, 11, 6, 9, 15), at(2026, 11, 6, 9, 0))
    # run due but not processed yet: look again in 15 minutes
    later = at(2026, 11, 6, 9, 20)
    assert notifier.plan_auto_check(cfg, status(), later) == (at(2026, 11, 6, 9, 35), at(2026, 11, 6, 9, 0))
    # off-season or before the draft: once a day at noon, no "nothing new" emails
    assert notifier.plan_auto_check(cfg, status("over"), now) == (at(2026, 11, 6, 12, 0), None)
    assert notifier.plan_auto_check(cfg, status("predraft"), at(2026, 11, 6, 13, 0)) == (at(2026, 11, 7, 12, 0), None)
    # last check failed: try again in an hour
    assert notifier.plan_auto_check(cfg, None, now) == (at(2026, 11, 6, 9, 0), None)
    # league without waivers: every POLL_MINUTES
    assert notifier.plan_auto_check(cfg, status(next_waiver=None), now) == (at(2026, 11, 6, 8, 15), None)


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
