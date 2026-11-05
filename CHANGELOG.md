# Changelog

## 1.0.0

- First release.
- Emails waiver claims with the player added, the player dropped and the FAAB bid.
- Optional: free-agent pickups, drops and trades (`NOTIFY_TYPES`).
- Check at fixed times after waivers run (`CHECK_TIMES`, `CHECK_DAYS`), with a
  "No waiver claims today" email when nothing went through, or every few minutes.
- Retries when ESPN is slow, and one alert email if the league can't be reached
  (for example, expired cookies).
- Unraid Community Applications template; runs as nobody:users (PUID/PGID).
