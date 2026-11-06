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
- Reads your league's waiver schedule from ESPN and checks 5 minutes after each
  waiver run, then once a minute until ESPN has processed it, so there's nothing
  to look up. (Fixed times or every 15 minutes also work.)
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
Watching league 123456 (latest season) for WAIVER, 5 min after each of the league's waiver runs.
Connected to 'My League' (2026 season, week 9 of 17, next waiver run Fri Nov 6, 9:00 AM PST). First run: recorded 25 existing transactions without emailing; only new ones will be sent.
Next check: Fri Nov 6, 9:05 AM PST (after the Fri Nov 6, 9:00 AM PST waiver run)
```

### Between seasons

When your league's final week is over, you get one "Season's over" email, and the
"No waiver claims today" emails stop. The app keeps checking once a day, quietly.
When the league manager renews the league for the new season and you draft,
the emails start again on their own.

## Install with Docker

Runs anywhere Docker does: a Linux server, a Raspberry Pi 4 or 5 (64-bit OS), a
Synology, QNAP or TrueNAS NAS, or Docker Desktop on Windows or a Mac (it only
checks while that computer is on and awake). The image is built for both
amd64 and arm64.

1. Make a folder for it and download two files into it: [`.env.example`](https://raw.githubusercontent.com/SirBusta/espn-fantasy-football-waivers/main/.env.example),
   saved as `.env`, and [`docker-compose.yml`](https://raw.githubusercontent.com/SirBusta/espn-fantasy-football-waivers/main/docker-compose.yml).
   On Linux or a Mac:

   ```bash
   mkdir espn-fantasy-football-waivers && cd espn-fantasy-football-waivers
   curl -fsSL -o .env https://raw.githubusercontent.com/SirBusta/espn-fantasy-football-waivers/main/.env.example
   curl -fsSLO https://raw.githubusercontent.com/SirBusta/espn-fantasy-football-waivers/main/docker-compose.yml
   ```

2. Open `.env` in a text editor and fill in your league ID, cookies and email
   settings. **Set `TZ` to your time zone** (for example `America/Los_Angeles`;
   [list of names](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)),
   or the emails show New York time.

3. Check that your league ID and cookies work. This prints your league's recent
   activity without sending anything:

   ```bash
   docker run --rm --env-file .env ghcr.io/sirbusta/espn-fantasy-football-waivers:latest python notifier.py --dry-run
   ```

4. Send a test email to check the email settings:

   ```bash
   docker run --rm --env-file .env ghcr.io/sirbusta/espn-fantasy-football-waivers:latest python notifier.py --test-email
   ```

5. Start it. It keeps running in the background and starts again after a reboot:

   ```bash
   docker compose up -d
   ```

   Without Docker Compose, this does the same:

   ```bash
   docker run -d --name espn-fantasy-football-waivers --restart always --env-file .env -v "$(pwd)/data:/data" ghcr.io/sirbusta/espn-fantasy-football-waivers:latest
   ```

The `data` folder it creates remembers which moves you've been emailed about;
keep it when updating.

**See what it's doing:** `docker logs -f espn-fantasy-football-waivers` (Ctrl+C
to stop watching). A healthy start looks like the log in the Unraid section above.

**Change a setting:** edit `.env`, then `docker compose up -d` again.

**Update to a new version:**

```bash
docker compose pull && docker compose up -d
```

(Without Compose: `docker pull` the image, then `docker rm -f espn-fantasy-football-waivers`
and run the `docker run -d ...` command again.)

## Settings

| Setting | Default | Notes |
|---|---|---|
| `LEAGUE_ID` | required | From your league's URL |
| `ESPN_S2`, `SWID` | blank | Required for private leagues |
| `EMAIL_TO` | required | Several addresses: comma separated, no spaces |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_SECURITY` | `smtp.gmail.com` / 587 / `starttls` | `starttls` on 587 or `ssl` on 465 |
| `SMTP_USER` / `SMTP_PASSWORD` | | Login for the sending account |
| `EMAIL_FROM` | `SMTP_USER` | Sender address |
| `NOTIFY_TYPES` | `WAIVER` | Any of `WAIVER` (waiver claims), `FA` (free-agent pickups), `TRADE`, `DROP` (drops without a pickup), e.g. `WAIVER,DROP` or `WAIVER,FA,TRADE,DROP`. With `auto`, moves made between waiver runs come in the next waiver run's email; use `interval` to get them within `POLL_MINUTES` |
| `CHECK_TIMES` | `auto` | `auto`: after each of the league's waiver runs. Or fixed 24-hour `HH:MM` times in `TZ`, e.g. `09:15,13:00`. Or `interval`: every `POLL_MINUTES` |
| `AUTO_DELAY_MINUTES` | 5 | With `auto`, how long after each scheduled waiver run to check. If ESPN hasn't processed it yet, it checks every minute until it has |
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

The Unraid template is in the [BudMinder repository](https://github.com/SirBusta/budminder/blob/main/templates/espn-fantasy-football-waivers.xml),
which Community Applications reads for all of SirBusta's apps. Change it there.

## Support

Questions: the [Unraid forum support thread](https://forums.unraid.net/topic/201197-support-sirbusta-espn-fantasy-football-waivers-email/)
(you don't need Unraid to ask there). Bugs: there or on
[GitHub Issues](https://github.com/SirBusta/espn-fantasy-football-waivers/issues).

## License

[MIT](LICENSE)
