<p align="center"><img src="assets/icon.png" width="96" alt=""></p>

# ESPN Fantasy Football Waivers

ESPN Fantasy Football doesn't email you when waivers run. This does. After each
waiver run, you get one email listing every claim in your league: who picked up
whom, who they dropped and what they bid.

```
[My League] Waivers processed: 3 claims

Wed Nov 4, 9:00 AM PST
  Taco Corp: Added (waivers) Gardner Minshew II (QB, ARI)  ($21 FAAB)
  Taco Corp: Dropped Tanner McKee (QB, PHI)

Wed Nov 4, 9:00 AM PST
  Gridiron Gang: Added (waivers) Tyler Goodson (RB, DAL)  ($0 FAAB)
  Gridiron Gang: Dropped Dameon Pierce (RB, PHI)
...
```

- Works with public and private leagues, with or without FAAB.
- Can also include free-agent pickups, drops and trades.
- Reads your league's waiver schedule from ESPN and checks 15 minutes after each
  waiver run, so there's nothing to look up. (Fixed times or every 15 minutes also work.)
- Sends a short "No waiver claims today" email when nothing went through, so you
  know it's working. These stop by themselves when the season ends and start
  again after next season's draft.
- Never emails the same move twice, retries when ESPN is slow, and emails you once
  if it can't reach your league (for example, when your cookies expire).
- Runs as a small Docker container. There's an Unraid Community Applications template.

*Unofficial tool; not affiliated with or endorsed by ESPN.*

## What you need

**Your league ID.** Open your league on fantasy.espn.com. It's the number after
`leagueId=` in the address bar.

**Two ESPN cookies, for private leagues** (most leagues are private). ESPN
doesn't allow automated logins, so the app uses your browser's login cookies:

1. Log in at espn.com in Chrome or Edge.
2. Press **F12**, open the **Application** tab, then **Cookies** > `https://fantasy.espn.com`
   (or `https://www.espn.com`; both show the same cookies).
3. Type `espn_s2` in the **Filter** box and click the row. Copy the full value from
   the panel below the list, since the table cuts it off. Leave "Show URL-decoded" unticked.
4. Do the same for `SWID`. It looks like `{1A2B3C4D-...}`; copy it with the curly braces.

The cookies usually last about a year. **Don't click "Log out" on espn.com in that
browser**, which can end the session early. If they stop working, the app emails
you; copy fresh ones and restart it.

**An email account to send from.** Any SMTP server works. For Gmail, turn on
2-Step Verification, then create an App Password at
https://myaccount.google.com/apppasswords and use it (typed without spaces) as
the SMTP password. Your normal Gmail password won't work.

## Install on Unraid

1. On the **Apps** tab, search for **ESPN Fantasy Football Waivers** and click **Install**.
2. Fill in your league ID, cookies and email settings. Leave **Check times** on
   `auto`; it follows your league's waiver schedule.
3. To confirm email works, set **Send test email on start** to `true`, click **Apply**
   and check your inbox. Then set it back to `false`.

Open the container's **Logs** to see what it's doing. A healthy start looks like:

```
ESPN Fantasy Football Waivers 1.1.0 starting.
Watching league 123456 (latest season) for WAIVER, 15 min after each of the league's waiver runs.
Connected to 'My League' (2026 season, week 9 of 17, next waiver run Fri Nov 6, 9:00 AM PST). First run: recorded 25 existing transactions without emailing; only new ones will be sent.
Next check: Fri Nov 6, 9:15 AM PST (after the Fri Nov 6, 9:00 AM PST waiver run)
```

### Between seasons

When your league's final week is over, you get one "Season's over" email, and the
"No waiver claims today" emails stop. The app keeps checking once a day, quietly.
When the league manager renews the league for the new season and you draft,
the emails start again on their own.

## Install with Docker

Copy [`.env.example`](.env.example) to `.env`, fill it in, then either use the
included [`docker-compose.yml`](docker-compose.yml):

```bash
docker compose up -d
```

or plain Docker:

```bash
docker run -d --name espn-fantasy-football-waivers --restart always --env-file .env -v "$(pwd)/data:/data" ghcr.io/sirbusta/espn-fantasy-football-waivers:latest
```

Two commands help with setup. This one prints your league's recent activity
without sending anything, to confirm your league ID and cookies work:

```bash
docker run --rm --env-file .env ghcr.io/sirbusta/espn-fantasy-football-waivers:latest python notifier.py --dry-run
```

This one sends a test email:

```bash
docker run --rm --env-file .env ghcr.io/sirbusta/espn-fantasy-football-waivers:latest python notifier.py --test-email
```

## Settings

| Setting | Default | Notes |
|---|---|---|
| `LEAGUE_ID` | required | From your league's URL |
| `ESPN_S2`, `SWID` | blank | Required for private leagues |
| `EMAIL_TO` | required | Several addresses: comma separated, no spaces |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_SECURITY` | `smtp.gmail.com` / 587 / `starttls` | `starttls` on 587 or `ssl` on 465 |
| `SMTP_USER` / `SMTP_PASSWORD` | | Login for the sending account |
| `EMAIL_FROM` | `SMTP_USER` | Sender address |
| `NOTIFY_TYPES` | `WAIVER` | Add any of `FA`, `TRADE`, `DROP`, e.g. `WAIVER,FA,TRADE` |
| `CHECK_TIMES` | `auto` | `auto`: after each of the league's waiver runs. Or fixed 24-hour `HH:MM` times in `TZ`, e.g. `09:15,13:00`. Or `interval`: every `POLL_MINUTES` |
| `AUTO_DELAY_MINUTES` | 15 | With `auto`, how long after each waiver run to check |
| `CHECK_DAYS` | every day | Days for fixed `CHECK_TIMES`, e.g. `Wed,Thu,Fri,Sat,Sun` |
| `POLL_MINUTES` | 15 | How often to check with `interval` |
| `NOTIFY_WHEN_EMPTY` | true | Email "No waiver claims today" after a waiver run with nothing new (not with `interval`); paused between seasons |
| `TZ` | `America/New_York` | Time zone for check times and emails (Unraid sets this for you) |
| `SEND_TEST_EMAIL` | false | Send a test email each time the container starts |
| `SEND_ON_FIRST_RUN` | false | Email the existing recent activity the first time it starts |
| `SEASON` | latest | Year the season started. Blank follows the league's latest season |
| `RETRIES` / `RETRY_MINUTES` | 3 / 15 | If a scheduled check fails, try again this many times, this far apart |
| `ALERT_AFTER_FAILURES` | `RETRIES`+1, or 8 with `interval` | Failed checks in a row before the one-time alert email |
| `PUID` / `PGID` | 99 / 100 | Owner of the data folder (Unraid's nobody:users) |

## How it works

The app uses the community [espn_api](https://github.com/cwendt94/espn-api)
library to read your league's activity feed, the same list you see under
**League > Recent Activity** on ESPN. It remembers which moves it has already
emailed in `/data/state.json`. If an email fails to send, it tries again on the
next check.

ESPN's API is unofficial and can change without notice. If it stops working
after an ESPN update, check for a newer version of this app.

## Development

```bash
pip install -r requirements.txt pytest
pytest
```

Pushes to `main` publish the `edge` image. Tagging `vX.Y.Z` (matching the
`VERSION` file) publishes `X.Y.Z` and `latest`, and creates a GitHub release
from `CHANGELOG.md`.

## License

[MIT](LICENSE)
