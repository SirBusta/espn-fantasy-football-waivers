# Changelog

## 1.1.0

- New default `CHECK_TIMES=auto`: reads the league's waiver schedule from ESPN
  and checks 15 minutes after each waiver run (`AUTO_DELAY_MINUTES`). If ESPN
  hasn't finished processing yet, it checks again every 15 minutes until it has.
  Fixed times still work; `interval` replaces a blank `CHECK_TIMES` for
  every-N-minutes checking.
- Season awareness: one "Season's over" email after the final week, then no
  "No waiver claims today" emails until after next season's draft.
- Follows the league's latest season. Between the new year and the league
  being renewed, it stays on last season instead of sending false
  "can't reach your league" alerts.
- `--dry-run` and the logs show the season, week and next waiver run.

## 1.0.0

- First release.
- Emails waiver claims with the player added, the player dropped and the FAAB bid.
- Optional: free-agent pickups, drops and trades (`NOTIFY_TYPES`).
- Check at fixed times after waivers run (`CHECK_TIMES`, `CHECK_DAYS`), with a
  "No waiver claims today" email when nothing went through, or every few minutes.
- Retries when ESPN is slow, and one alert email if the league can't be reached
  (for example, expired cookies).
- Unraid Community Applications template; runs as nobody:users (PUID/PGID).
