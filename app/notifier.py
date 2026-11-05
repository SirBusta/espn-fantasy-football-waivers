"""ESPN Fantasy Football Waivers: emails your ESPN fantasy football league's waiver results.

Reads an ESPN league's recent activity (waiver claims, and optionally
free-agent adds, drops and trades) and emails a summary of anything new.
Unofficial; not affiliated with or endorsed by ESPN.

Usage:
    python notifier.py               # run forever (fixed CHECK_TIMES or every POLL_MINUTES)
    python notifier.py --once        # check one time, then exit
    python notifier.py --dry-run     # print recent activity; send/save nothing
    python notifier.py --test-email  # send a test email to check SMTP settings
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from espn_api.football import League
from espn_api.requests.espn_requests import ESPNInvalidLeague, EspnFantasyRequests

log = logging.getLogger("espn-fantasy-football-waivers")
APP_NAME = "ESPN Fantasy Football Waivers"
APP_VERSION = os.environ.get("APP_VERSION", "dev")

# espn_api calls requests.get() with no timeout, so a stalled ESPN connection
# could hang forever and stop all future checks. Give every request a limit.
_original_request = requests.Session.request


def _request_with_timeout(self, method, url, **kwargs):
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = 60
    return _original_request(self, method, url, **kwargs)


requests.Session.request = _request_with_timeout

ALL_CATEGORIES = {"WAIVER", "FA", "TRADE", "DROP"}
ACTION_LABELS = {
    "WAIVER ADDED": "Added (waivers)",
    "FA ADDED": "Added (free agent)",
    "DROPPED": "Dropped",
    "TRADE_SENT": "Traded away",
    "TRADE_RECEIVED": "Received in trade",
}
ACTION_COLORS = {
    "WAIVER ADDED": "#1a7f37",
    "FA ADDED": "#1a7f37",
    "DROPPED": "#cf222e",
    "TRADE_SENT": "#9a6700",
    "TRADE_RECEIVED": "#0969da",
}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    if value is not None:
        value = value.strip()
    return value or default


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    league_id: int
    season: int | None  # None: follow the league's latest season
    espn_s2: str | None
    swid: str | None
    poll_minutes: float
    categories: set[str]
    send_on_first_run: bool
    timezone: ZoneInfo
    data_dir: Path
    smtp_host: str | None
    smtp_port: int
    smtp_security: str
    smtp_user: str | None
    smtp_password: str | None
    email_from: str | None
    email_to: list[str] = field(default_factory=list)
    alert_after_failures: int = 8
    check_days: set[int] = field(default_factory=set)  # 0=Mon .. 6=Sun
    check_times: list[tuple[int, int]] = field(default_factory=list)  # (hour, minute)
    retries: int = 3
    retry_minutes: float = 15
    notify_when_empty: bool = False
    # "auto": check AUTO_DELAY_MINUTES after each of the league's waiver runs;
    # "fixed": at CHECK_TIMES on CHECK_DAYS; "interval": every POLL_MINUTES.
    check_mode: str = "auto"
    auto_delay_minutes: float = 5

    @classmethod
    def from_env(cls) -> "Config":
        league_id = _env("LEAGUE_ID")
        if not league_id or not league_id.isdigit():
            sys.exit("LEAGUE_ID must be set to your numeric ESPN league ID.")

        categories = {
            c.strip().upper()
            for c in (_env("NOTIFY_TYPES", "WAIVER") or "").split(",")
            if c.strip()
        }
        unknown = categories - ALL_CATEGORIES
        if unknown:
            sys.exit(f"Unknown NOTIFY_TYPES {sorted(unknown)}; use {sorted(ALL_CATEGORIES)}")

        swid = _env("SWID")
        if swid and not swid.startswith("{"):
            swid = "{" + swid.strip("{}") + "}"

        security = (_env("SMTP_SECURITY", "starttls") or "starttls").lower()
        if security not in {"starttls", "ssl", "none"}:
            sys.exit("SMTP_SECURITY must be starttls, ssl or none.")

        day_names = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
        check_days = set()
        for d in (_env("CHECK_DAYS") or "").split(","):
            d = d.strip().upper()[:3]
            if not d:
                continue
            if d not in day_names:
                sys.exit(f"Unknown day {d!r} in CHECK_DAYS; use Mon,Tue,Wed,...")
            check_days.add(day_names.index(d))
        check_times = []
        raw_times = (_env("CHECK_TIMES") or "auto").strip().lower()
        if raw_times in {"auto", "interval"}:
            check_mode = raw_times
        else:
            check_mode = "fixed"
            for t in raw_times.split(","):
                t = t.strip()
                if not t:
                    continue
                try:
                    hour, minute = (int(x) for x in t.split(":"))
                    assert 0 <= hour < 24 and 0 <= minute < 60
                except (ValueError, AssertionError):
                    sys.exit(f"Bad time {t!r} in CHECK_TIMES; use auto, interval, "
                             "or 24-hour HH:MM like 09:15")
                check_times.append((hour, minute))
        if check_times and not check_days:
            check_days = set(range(7))
        scheduled = check_mode != "interval"

        return cls(
            check_mode=check_mode,
            auto_delay_minutes=float(_env("AUTO_DELAY_MINUTES", "5")),
            # "Nothing today" emails only make sense when checks follow the
            # waiver runs, so they're off for every-N-minutes polling.
            notify_when_empty=scheduled and _env_bool("NOTIFY_WHEN_EMPTY", True),
            check_days=check_days,
            check_times=sorted(check_times),
            retries=int(_env("RETRIES", "3")),
            retry_minutes=float(_env("RETRY_MINUTES", "15")),
            league_id=int(league_id),
            season=int(_env("SEASON")) if _env("SEASON") else None,
            espn_s2=_env("ESPN_S2"),
            swid=swid,
            poll_minutes=float(_env("POLL_MINUTES", "15")),
            categories=categories,
            send_on_first_run=_env_bool("SEND_ON_FIRST_RUN"),
            timezone=ZoneInfo(_env("TZ", "America/New_York")),
            data_dir=Path(_env("DATA_DIR", "/data")),
            smtp_host=_env("SMTP_HOST"),
            smtp_port=int(_env("SMTP_PORT", "465" if security == "ssl" else "587")),
            smtp_security=security,
            smtp_user=_env("SMTP_USER"),
            smtp_password=_env("SMTP_PASSWORD"),
            email_from=_env("EMAIL_FROM") or _env("SMTP_USER"),
            email_to=[a.strip() for a in (_env("EMAIL_TO") or "").split(",") if a.strip()],
            # On a schedule, alert once a check's retries are used up.
            alert_after_failures=int(
                _env("ALERT_AFTER_FAILURES")
                or (int(_env("RETRIES", "3")) + 1 if scheduled else 8)
            ),
        )

    def require_email(self) -> None:
        missing = [
            name
            for name, value in [
                ("SMTP_HOST", self.smtp_host),
                ("EMAIL_FROM", self.email_from),
                ("EMAIL_TO", self.email_to),
            ]
            if not value
        ]
        if missing:
            sys.exit(f"Email settings missing: {', '.join(missing)}")


# --------------------------------------------------------------------------- #
# State (which activities we've already emailed about)
# --------------------------------------------------------------------------- #
class State:
    MAX_SEEN = 5000

    def __init__(self, path: Path):
        self.path = path
        self.seen: list[str] = []
        self.initialized = False
        self.consecutive_failures = 0
        self.alert_sent = False
        self.phase: str | None = None  # season phase at the last check
        self.last_reported_run: str | None = None  # waiver run already reported on
        self.status: LeagueStatus | None = None  # from the last check (not saved)
        if path.exists():
            data = json.loads(path.read_text())
            self.seen = data.get("seen", [])
            self.initialized = data.get("initialized", False)
            self.consecutive_failures = data.get("consecutive_failures", 0)
            self.alert_sent = data.get("alert_sent", False)
            self.phase = data.get("phase")
            self.last_reported_run = data.get("last_reported_run")
        self._seen_set = set(self.seen)

    def __contains__(self, key: str) -> bool:
        return key in self._seen_set

    def add(self, keys: list[str]) -> None:
        for key in keys:
            if key not in self._seen_set:
                self.seen.append(key)
                self._seen_set.add(key)
        self.seen = self.seen[-self.MAX_SEEN :]
        self._seen_set = set(self.seen)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "seen": self.seen,
                    "initialized": self.initialized,
                    "consecutive_failures": self.consecutive_failures,
                    "alert_sent": self.alert_sent,
                    "phase": self.phase,
                    "last_reported_run": self.last_reported_run,
                },
                indent=1,
            )
        )
        tmp.replace(self.path)


# --------------------------------------------------------------------------- #
# Fetching and normalizing ESPN activity
# --------------------------------------------------------------------------- #
@dataclass
class Move:
    team: str
    action: str
    player: str
    position: str
    pro_team: str
    bid: int | None


@dataclass
class Event:
    key: str
    when: datetime
    category: str
    moves: list[Move]


@dataclass
class LeagueStatus:
    season: int
    phase: str  # "predraft", "active" or "over"
    week: int
    final_week: int
    last_waiver: datetime | None  # when ESPN last processed waivers
    next_waiver: datetime | None  # when ESPN will next process waivers

    def describe(self) -> str:
        if self.phase == "predraft":
            return f"{self.season} season: waiting for the draft"
        if self.phase == "over":
            return f"{self.season} season is over"
        nxt = f", next waiver run {_fmt_time(self.next_waiver)}" if self.next_waiver else ""
        return f"{self.season} season, week {self.week} of {self.final_week}{nxt}"


def _team_name(team) -> str:
    if not team:
        return "Unknown team"
    return getattr(team, "team_name", None) or str(team)


def _player_fields(player) -> tuple[str, str, str]:
    if isinstance(player, (int, str)) or player is None:
        return (f"Player #{player}" if isinstance(player, int) else str(player or "Unknown"), "", "")
    return (
        getattr(player, "name", "Unknown"),
        getattr(player, "position", "") or "",
        getattr(player, "proTeam", "") or "",
    )


def _player_id(player) -> str:
    if isinstance(player, int):
        return str(player)
    return str(getattr(player, "playerId", None) or _player_fields(player)[0])


def _categorize(actions: list[str]) -> str:
    if any(a.startswith("TRADE") for a in actions):
        return "TRADE"
    if "WAIVER ADDED" in actions:
        return "WAIVER"
    if "FA ADDED" in actions:
        return "FA"
    return "DROP"


def _event_key(date_ms: int, raw_actions) -> str:
    parts = [str(date_ms)]
    for team, action, player, *_ in raw_actions:
        parts.append(f"{getattr(team, 'team_id', team)}|{action}|{_player_id(player)}")
    return hashlib.sha1("\n".join(sorted(parts)).encode()).hexdigest()


def open_league(cfg: Config):
    """Open the configured season, or else the league's latest one.

    ESPN only creates a league's new season when the league manager renews it
    (often months after the calendar year changes), so if this year's season
    doesn't exist yet, fall back to last year's.
    """
    def connect(year):
        return League(league_id=cfg.league_id, year=year,
                      espn_s2=cfg.espn_s2, swid=cfg.swid)

    if cfg.season:
        return connect(cfg.season)
    year = datetime.now(cfg.timezone).year
    try:
        return connect(year)
    except ESPNInvalidLeague:
        return connect(year - 1)


def _from_ms(ms, tz: ZoneInfo) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, tz=tz) if ms else None


def fetch_status(league, cfg: Config) -> LeagueStatus:
    return _parse_status(league.espn_request, league.year, cfg)


def fetch_status_only(cfg: Config, season: int) -> LeagueStatus:
    """Just the league's status: one small request instead of the full league
    download, for checking every minute whether a waiver run has finished."""
    cookies = {"espn_s2": cfg.espn_s2, "SWID": cfg.swid} if cfg.espn_s2 and cfg.swid else None
    request = EspnFantasyRequests(sport="nfl", year=season, league_id=cfg.league_id,
                                  cookies=cookies, logger=None)
    return _parse_status(request, season, cfg)


def _parse_status(request, season: int, cfg: Config) -> LeagueStatus:
    data = request.league_get(params={"view": ["mSettings", "mDraftDetail"]})
    status = data.get("status", {})
    week = data.get("scoringPeriodId") or 0
    final_week = status.get("finalScoringPeriod") or 0
    if not data.get("draftDetail", {}).get("drafted"):
        phase = "predraft"
    elif final_week and week > final_week:
        phase = "over"
    else:
        phase = "active"
    return LeagueStatus(
        season=season,
        phase=phase,
        week=week,
        final_week=final_week,
        last_waiver=_from_ms(status.get("waiverLastExecutionDate"), cfg.timezone),
        next_waiver=_from_ms(status.get("waiverNextExecutionDate"), cfg.timezone),
    )


def fetch_events(cfg: Config, size: int = 100) -> tuple[str, list[Event], LeagueStatus]:
    league = open_league(cfg)
    status = fetch_status(league, cfg)
    uses_faab = bool(getattr(league.settings, "faab", False))
    events: list[Event] = []
    for activity in league.recent_activity(size=size):
        raw = activity.actions
        moves = []
        for team, action, player, *rest in raw:
            name, pos, pro = _player_fields(player)
            bid = rest[0] if rest else None
            moves.append(
                Move(
                    team=_team_name(team),
                    action=action,
                    player=name,
                    position=pos,
                    pro_team=pro,
                    bid=bid if (uses_faab and action == "WAIVER ADDED") else None,
                )
            )
        # Show adds first, then drops, for readability.
        order = {"WAIVER ADDED": 0, "FA ADDED": 0, "TRADE_RECEIVED": 1, "TRADE_SENT": 2, "DROPPED": 3}
        moves.sort(key=lambda m: (m.team, order.get(m.action, 9)))
        events.append(
            Event(
                key=_event_key(activity.date, raw),
                when=datetime.fromtimestamp(activity.date / 1000, tz=cfg.timezone),
                category=_categorize([m.action for m in moves]),
                moves=moves,
            )
        )
    events.sort(key=lambda e: e.when)
    return league.settings.name, events, status


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%a %b %d, %I:%M %p %Z").replace(" 0", " ")


def _subject(league_name: str, events: list[Event]) -> str:
    counts = {c: sum(e.category == c for e in events) for c in ALL_CATEGORIES}
    if counts["WAIVER"] and counts["WAIVER"] == len(events):
        what = f"Waivers processed: {counts['WAIVER']} claim{'s' * (counts['WAIVER'] != 1)}"
    elif counts["TRADE"] and counts["TRADE"] == len(events):
        what = f"Trade alert ({counts['TRADE']})"
    else:
        what = f"{len(events)} new transaction{'s' * (len(events) != 1)}"
    return f"[{league_name}] {what}"


def render_text(league_name: str, events: list[Event]) -> str:
    lines = [f"{league_name} - league activity", ""]
    for e in events:
        lines.append(_fmt_time(e.when))
        for m in e.moves:
            detail = ", ".join(x for x in [m.position, m.pro_team] if x)
            bid = f"  (${m.bid} FAAB)" if m.bid is not None else ""
            lines.append(
                f"  {m.team}: {ACTION_LABELS.get(m.action, m.action)} "
                f"{m.player}{f' ({detail})' if detail else ''}{bid}"
            )
        lines.append("")
    return "\n".join(lines)


def render_html(league_name: str, events: list[Event]) -> str:
    esc = html.escape
    rows = []
    for e in events:
        rows.append(
            f'<tr><td colspan="4" style="padding:14px 8px 4px;font-size:12px;'
            f'color:#57606a;border-bottom:1px solid #d0d7de">{esc(_fmt_time(e.when))}</td></tr>'
        )
        for m in e.moves:
            color = ACTION_COLORS.get(m.action, "#24292f")
            detail = " &middot; ".join(esc(x) for x in [m.position, m.pro_team] if x)
            bid = f"${m.bid}" if m.bid is not None else ""
            rows.append(
                "<tr>"
                f'<td style="padding:6px 8px;font-weight:600">{esc(m.team)}</td>'
                f'<td style="padding:6px 8px;color:{color};white-space:nowrap">'
                f"{esc(ACTION_LABELS.get(m.action, m.action))}</td>"
                f'<td style="padding:6px 8px">{esc(m.player)}'
                f'<span style="color:#57606a;font-size:12px"> {detail}</span></td>'
                f'<td style="padding:6px 8px;text-align:right;color:#57606a">{bid}</td>'
                "</tr>"
            )
    return f"""\
<html><body style="margin:0;padding:16px;background:#f6f8fa;
font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#24292f">
<div style="max-width:640px;margin:auto;background:#fff;border:1px solid #d0d7de;
border-radius:8px;padding:16px">
<h2 style="margin:0 0 4px;font-size:18px">{esc(league_name)}</h2>
<div style="color:#57606a;font-size:13px;margin-bottom:8px">
{len(events)} new transaction{'s' * (len(events) != 1)}</div>
<table style="width:100%;border-collapse:collapse;font-size:14px">
{''.join(rows)}
</table></div></body></html>"""


def send_email(cfg: Config, subject: str, text: str, html_body: str | None = None) -> None:
    cfg.require_email()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.email_from
    msg["To"] = ", ".join(cfg.email_to)
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    if cfg.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=30)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
    with server:
        if cfg.smtp_security == "starttls":
            server.starttls(context=context)
        if cfg.smtp_user and cfg.smtp_password:
            server.login(cfg.smtp_user, cfg.smtp_password)
        server.send_message(msg)
    log.info("Sent email to %s: %s", ", ".join(cfg.email_to), subject)


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def next_run(cfg: Config, now: datetime) -> datetime:
    """Next fixed-time check strictly after `now` (wall-clock time in cfg.timezone)."""
    for offset in range(8):
        day = (now + timedelta(days=offset)).date()
        if day.weekday() not in cfg.check_days:
            continue
        for hour, minute in cfg.check_times:
            candidate = datetime(day.year, day.month, day.day, hour, minute,
                                 tzinfo=cfg.timezone)
            if candidate > now:
                return candidate
    raise RuntimeError("No scheduled check found; check CHECK_DAYS/CHECK_TIMES")


def plan_auto_check(cfg: Config, status: LeagueStatus | None,
                    now: datetime) -> tuple[datetime, datetime | None]:
    """In auto mode: when to check next, and which waiver run that check covers."""
    if status is None:  # the last check failed
        return now + timedelta(hours=1), None
    if status.phase != "active":
        # Off-season or before the draft: look once a day for the new season.
        noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
        return (noon if noon > now else noon + timedelta(days=1)), None
    run = status.next_waiver
    if run is None:  # a league without waivers
        return now + timedelta(minutes=cfg.poll_minutes), None
    when = run + timedelta(minutes=cfg.auto_delay_minutes)
    if when > now:
        return when, run
    if now - run < WAIT_FOR_RUN_LIMIT:
        # The run is due, but ESPN hasn't processed it or updated its schedule yet.
        return now, run
    return now + timedelta(hours=1), None


WAIT_FOR_RUN_LIMIT = timedelta(hours=6)


def wait_for_run(cfg: Config, season: int, run: datetime) -> None:
    """Return once ESPN reports the waiver run as processed.

    ESPN usually starts within a couple of minutes of the scheduled time, so
    check once a minute (a small status request, not the full league), then
    every 15 minutes after half an hour, and give up after 6 hours.
    """
    said_waiting = False
    while True:
        try:
            status = fetch_status_only(cfg, season)
            if status.last_waiver and status.last_waiver >= run - timedelta(minutes=1):
                if said_waiting:
                    log.info("ESPN processed the waiver run at %s.", _fmt_time(status.last_waiver))
                return
            if status.next_waiver and status.next_waiver > run:
                log.info("ESPN skipped the %s waiver run.", _fmt_time(run))
                return
        except Exception as exc:
            log.warning("Couldn't check whether waivers have run: %s", exc)
        waited = datetime.now(run.tzinfo) - run
        if waited >= WAIT_FOR_RUN_LIMIT:
            log.warning("Gave up waiting for ESPN to process the %s waiver run.", _fmt_time(run))
            return
        if not said_waiting:
            log.info("ESPN hasn't processed the %s waiver run yet; checking every minute.",
                     _fmt_time(run))
            said_waiting = True
        time.sleep(60 if waited < timedelta(minutes=30) else 15 * 60)


def sleep_until(when: datetime) -> None:
    while (remaining := (when - datetime.now(when.tzinfo)).total_seconds()) > 0:
        time.sleep(min(remaining, 3600))


def _note_phase(cfg: Config, state: State, league_name: str, status: LeagueStatus) -> None:
    previous, state.phase = state.phase, status.phase
    if previous is None or previous == status.phase:
        return
    log.info("League status changed: %s.", status.describe())
    if previous == "active" and status.phase == "over" and cfg.notify_when_empty:
        try:
            send_email(
                cfg,
                f"[{league_name}] Season's over",
                f"The {status.season} season of {league_name} has ended, so the daily "
                "'No waiver claims today' emails stop here. They'll start again on their "
                "own after next season's draft.\n\n"
                "Any new transactions will still be emailed.",
            )
        except Exception as exc:
            log.error("Could not send the season's-over email: %s", exc)


def poll_once(cfg: Config, state: State, waiver_check: bool = False,
              expected_run: datetime | None = None) -> bool:
    """Check ESPN and email anything new. Returns False if it should be retried.

    waiver_check: this check follows a waiver run, so also report when nothing
    is new. expected_run: in auto mode, the waiver run this check is for.
    """
    try:
        league_name, events, status = fetch_events(cfg)
    except Exception as exc:  # network errors, expired cookies, ESPN outages
        state.status = None
        state.consecutive_failures += 1
        log.error("Could not fetch ESPN activity (%d in a row): %s",
                  state.consecutive_failures, exc)
        if state.consecutive_failures >= cfg.alert_after_failures and not state.alert_sent:
            try:
                send_email(
                    cfg,
                    f"[{APP_NAME}] Can't reach your league",
                    f"{APP_NAME} has failed to read league {cfg.league_id} "
                    f"{state.consecutive_failures} times in a row.\n\n"
                    f"Last error: {exc}\n\n"
                    "If your league is private, your espn_s2/SWID cookies may have expired "
                    "(this happens about once a year, or when you log out of espn.com in the "
                    "browser you copied them from). Copy fresh ones from espn.com into the "
                    "container's settings and restart it.\n\n"
                    "You'll get one of these alerts per outage.",
                )
                state.alert_sent = True
            except Exception as mail_exc:
                log.error("Could not send failure alert: %s", mail_exc)
        state.save()
        return False

    if state.consecutive_failures:
        log.info("ESPN reachable again after %d failure(s).", state.consecutive_failures)
    state.consecutive_failures = 0
    state.alert_sent = False
    state.status = status
    _note_phase(cfg, state, league_name, status)

    new = [e for e in events if e.key not in state]
    if not state.initialized and not cfg.send_on_first_run:
        log.info("Connected to '%s' (%s). First run: recorded %d existing transactions "
                 "without emailing; only new ones will be sent.",
                 league_name, status.describe(), len(new))
        state.add([e.key for e in new])
        state.initialized = True
        state.save()
        return True
    state.initialized = True

    run_key = expected_run.isoformat() if expected_run else None
    run_done = expected_run is None or (
        status.last_waiver is not None
        and status.last_waiver >= expected_run - timedelta(minutes=1)
    )

    to_send = [e for e in new if e.category in cfg.categories]
    if to_send:
        try:
            send_email(
                cfg,
                _subject(league_name, to_send),
                render_text(league_name, to_send),
                render_html(league_name, to_send),
            )
        except Exception as exc:  # nothing is marked seen, so the retry resends
            log.error("Could not send email: %s", exc)
            state.save()
            return False
        if waiver_check and run_done and run_key:
            state.last_reported_run = run_key
    else:
        log.info("Nothing new to report (%d recent transactions checked).", len(events))
        if cfg.notify_when_empty and waiver_check:
            if status.phase != "active":
                log.info("Not sending a 'nothing new' email: %s.", status.describe())
            elif not run_done:
                log.info("Not sending a 'nothing new' email: ESPN didn't process the %s "
                         "waiver run.", _fmt_time(expected_run))
            elif run_key and run_key == state.last_reported_run:
                pass  # already reported on this waiver run
            else:
                what = ("waiver claims" if cfg.categories == {"WAIVER"}
                        else "transactions")
                now = _fmt_time(datetime.now(cfg.timezone))
                try:
                    send_email(
                        cfg,
                        f"[{league_name}] No {what} today",
                        f"Checked {league_name} at {now}: no new {what} since the "
                        "last check.\n\n(This email means the notifier is running normally.)",
                    )
                except Exception as exc:
                    log.error("Could not send email: %s", exc)
                    state.save()
                    return False
                if run_key:
                    state.last_reported_run = run_key
    state.add([e.key for e in new])
    state.save()
    return True


def poll_with_retries(cfg: Config, state: State, **kwargs) -> None:
    for attempt in range(cfg.retries + 1):
        try:
            if poll_once(cfg, state, **kwargs):
                return
        except Exception:
            log.exception("Unexpected error during check")
        if attempt < cfg.retries:
            log.info("Retrying in %g min (%d of %d).",
                     cfg.retry_minutes, attempt + 1, cfg.retries)
            time.sleep(cfg.retry_minutes * 60)


def send_test_email(cfg: Config) -> None:
    send_email(cfg, f"[{APP_NAME}] Test email",
               f"If you can read this, {APP_NAME} can send email.\n\n"
               f"League ID: {cfg.league_id}\n"
               f"Watching for: {', '.join(sorted(cfg.categories))}\n"
               f"Recipients: {', '.join(cfg.email_to)}")


def drop_privileges(data_dir: Path) -> None:
    """When started as root (the Docker default), give the data folder to
    PUID:PGID (Unraid's nobody:users, 99:100, by default) and run as that user."""
    if os.name != "posix" or os.geteuid() != 0:
        return
    uid, gid = int(_env("PUID", "99")), int(_env("PGID", "100"))
    if uid == 0:
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    for path in [data_dir, *data_dir.rglob("*")]:
        try:
            os.chown(path, uid, gid)
        except OSError as exc:
            log.warning("Could not change owner of %s: %s", path, exc)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def startup_check(cfg: Config, state: State) -> None:
    """Log whether ESPN is reachable so setup problems show up in the logs right away."""
    try:
        league_name, events, status = fetch_events(cfg)
        state.status = status
        log.info("Connected to '%s' (%s; %d recent transactions).",
                 league_name, status.describe(), len(events))
    except Exception as exc:
        log.error("Could not read league %s: %s: %s. If the league is private, check "
                  "ESPN_S2 and SWID.", cfg.league_id, type(exc).__name__, exc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="print recent activity; don't email or save state")
    parser.add_argument("--test-email", action="store_true", help="send a test email and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    cfg = Config.from_env()
    drop_privileges(cfg.data_dir)

    if args.test_email:
        send_test_email(cfg)
        return

    if args.dry_run:
        try:
            league_name, events, status = fetch_events(cfg, size=50)
        except Exception as exc:
            sys.exit(f"Could not read league {cfg.league_id}: {type(exc).__name__}: {exc}")
        print(f"{league_name}: {status.describe()}.")
        if cfg.check_mode == "auto":
            when, _ = plan_auto_check(cfg, status, datetime.now(cfg.timezone))
            print(f"In auto mode, the next check would be {_fmt_time(when)}.")
        events = [e for e in events if e.category in cfg.categories][-15:]
        print(f"\nMost recent activity matching NOTIFY_TYPES={','.join(sorted(cfg.categories))}:\n")
        print(render_text(league_name, events) if events else "No recent activity found.")
        return

    cfg.require_email()
    state = State(cfg.data_dir / "state.json")
    if args.once:
        poll_once(cfg, state)
        return

    log.info("%s %s starting.", APP_NAME, APP_VERSION)
    if _env_bool("SEND_TEST_EMAIL"):
        try:
            send_test_email(cfg)
        except Exception as exc:
            log.error("Test email failed: %s: %s", type(exc).__name__, exc)

    what = (f"league {cfg.league_id} ({cfg.season or 'latest'} season) "
            f"for {','.join(sorted(cfg.categories))}")

    if cfg.check_mode == "interval":
        log.info("Watching %s every %g min.", what, cfg.poll_minutes)
        while True:
            poll_with_retries(cfg, state)
            time.sleep(cfg.poll_minutes * 60)

    if cfg.check_mode == "fixed":
        days = ",".join(d for i, d in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
                        if i in cfg.check_days)
        times = ",".join(f"{h:02d}:{m:02d}" for h, m in cfg.check_times)
        log.info("Watching %s on %s at %s %s.", what, days, times, cfg.timezone.key)
        if not state.initialized:
            poll_with_retries(cfg, state)  # snapshot existing activity so it isn't emailed
        else:
            startup_check(cfg, state)
        while True:
            when = next_run(cfg, datetime.now(cfg.timezone))
            log.info("Next check: %s", _fmt_time(when))
            sleep_until(when)
            poll_with_retries(cfg, state, waiver_check=True)

    log.info("Watching %s, %g min after each of the league's waiver runs.",
             what, cfg.auto_delay_minutes)
    if not state.initialized:
        poll_with_retries(cfg, state)
    else:
        startup_check(cfg, state)
    while True:
        when, run = plan_auto_check(cfg, state.status, datetime.now(cfg.timezone))
        after = f" (after the {_fmt_time(run)} waiver run)" if run else ""
        log.info("Next check: %s%s", _fmt_time(when), after)
        sleep_until(when)
        if run is not None and state.status is not None:
            wait_for_run(cfg, state.status.season, run)
        poll_with_retries(cfg, state, waiver_check=run is not None, expected_run=run)


if __name__ == "__main__":
    main()
