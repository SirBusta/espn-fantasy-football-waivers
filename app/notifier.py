"""ESPN Waiver Results: emails your ESPN fantasy football league's waiver results.

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

log = logging.getLogger("espn-waiver-results")
APP_NAME = "ESPN Waiver Results"
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


def _default_season() -> int:
    # ESPN names a season by the year it starts; Jan/Feb belong to last season.
    now = datetime.now()
    return now.year if now.month >= 3 else now.year - 1


@dataclass
class Config:
    league_id: int
    season: int
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
        for t in (_env("CHECK_TIMES") or "").split(","):
            t = t.strip()
            if not t:
                continue
            try:
                hour, minute = (int(x) for x in t.split(":"))
                assert 0 <= hour < 24 and 0 <= minute < 60
            except (ValueError, AssertionError):
                sys.exit(f"Bad time {t!r} in CHECK_TIMES; use 24-hour HH:MM like 09:15")
            check_times.append((hour, minute))
        if check_times and not check_days:
            check_days = set(range(7))

        return cls(
            # "Nothing today" emails only make sense with fixed check times,
            # so they're on by default there and off for every-N-minutes polling.
            notify_when_empty=bool(check_times) and _env_bool("NOTIFY_WHEN_EMPTY", True),
            check_days=check_days,
            check_times=sorted(check_times),
            retries=int(_env("RETRIES", "3")),
            retry_minutes=float(_env("RETRY_MINUTES", "15")),
            league_id=int(league_id),
            season=int(_env("SEASON") or _default_season()),
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
            # On a schedule, alert once a morning's retries are used up.
            alert_after_failures=int(
                _env("ALERT_AFTER_FAILURES")
                or (int(_env("RETRIES", "3")) + 1 if check_times else 8)
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
        if path.exists():
            data = json.loads(path.read_text())
            self.seen = data.get("seen", [])
            self.initialized = data.get("initialized", False)
            self.consecutive_failures = data.get("consecutive_failures", 0)
            self.alert_sent = data.get("alert_sent", False)
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


def fetch_events(cfg: Config, size: int = 100) -> tuple[str, list[Event]]:
    league = League(
        league_id=cfg.league_id,
        year=cfg.season,
        espn_s2=cfg.espn_s2,
        swid=cfg.swid,
    )
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
    return league.settings.name, events


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
    """Next scheduled check strictly after `now` (wall-clock time in cfg.timezone)."""
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


def sleep_until(when: datetime) -> None:
    while (remaining := (when - datetime.now(when.tzinfo)).total_seconds()) > 0:
        time.sleep(min(remaining, 3600))


def poll_once(cfg: Config, state: State) -> bool:
    """Check ESPN and email anything new. Returns False if it should be retried."""
    try:
        league_name, events = fetch_events(cfg)
    except Exception as exc:  # network errors, expired cookies, ESPN outages
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

    new = [e for e in events if e.key not in state]
    if not state.initialized and not cfg.send_on_first_run:
        log.info("Connected to '%s'. First run: recorded %d existing transactions "
                 "without emailing; only new ones will be sent.", league_name, len(new))
        state.add([e.key for e in new])
        state.initialized = True
        state.save()
        return True
    state.initialized = True

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
    else:
        log.info("Nothing new to report (%d recent transactions checked).", len(events))
        if cfg.notify_when_empty:
            what = ("waiver claims" if cfg.categories == {"WAIVER"}
                    else "transactions")
            now = _fmt_time(datetime.now(cfg.timezone))
            try:
                send_email(
                    cfg,
                    f"[{league_name}] No {what} today",
                    f"Checked {league_name} at {now}: no new {what} since the last check.\n\n"
                    "(This email means the notifier is running normally.)",
                )
            except Exception as exc:
                log.error("Could not send email: %s", exc)
                state.save()
                return False
    state.add([e.key for e in new])
    state.save()
    return True


def poll_with_retries(cfg: Config, state: State) -> None:
    for attempt in range(cfg.retries + 1):
        try:
            if poll_once(cfg, state):
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


def startup_check(cfg: Config) -> None:
    """Log whether ESPN is reachable so setup problems show up in the logs right away."""
    try:
        league_name, events = fetch_events(cfg)
        log.info("Connected to '%s' (%d recent transactions).", league_name, len(events))
    except Exception as exc:
        log.error("Could not read league %s: %s: %s. If the league is private, check "
                  "ESPN_S2 and SWID.", cfg.league_id, type(exc).__name__, exc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="poll once and exit")
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
            league_name, events = fetch_events(cfg, size=50)
        except Exception as exc:
            sys.exit(f"Could not read league {cfg.league_id}: {type(exc).__name__}: {exc}")
        events = [e for e in events if e.category in cfg.categories][-15:]
        print(f"Most recent activity matching NOTIFY_TYPES={','.join(sorted(cfg.categories))}:\n")
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

    types = ",".join(sorted(cfg.categories))
    if not cfg.check_times:
        log.info("Watching league %s (%s season) for %s every %g min.",
                 cfg.league_id, cfg.season, types, cfg.poll_minutes)
        while True:
            poll_with_retries(cfg, state)
            time.sleep(cfg.poll_minutes * 60)

    days = ",".join(d for i, d in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
                    if i in cfg.check_days)
    times = ",".join(f"{h:02d}:{m:02d}" for h, m in cfg.check_times)
    log.info("Watching league %s (%s season) for %s on %s at %s %s.",
             cfg.league_id, cfg.season, types, days, times, cfg.timezone.key)
    if not state.initialized:
        poll_with_retries(cfg, state)  # snapshot existing activity so it isn't emailed
    else:
        startup_check(cfg)
    while True:
        when = next_run(cfg, datetime.now(cfg.timezone))
        log.info("Next check: %s", _fmt_time(when))
        sleep_until(when)
        poll_with_retries(cfg, state)


if __name__ == "__main__":
    main()
