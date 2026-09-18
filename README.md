# Spotify account-to-account migrator

Copies your owned playlists, Liked Songs, saved albums and followed artists from
one Spotify account (the **source**) to another (the **destination**). Tracks are
copied by Spotify URI, so nothing is matched by name. The source account is only
ever read.

## 1. Create a Spotify developer app

1. Sign in at <https://developer.spotify.com/dashboard>. The account that owns
   the app needs an active **Premium** subscription. Spotify has required this
   for development-mode apps since February 2026.
2. Click **Create app**. Give it any name and description, tick **Web API**, and
   under **Redirect URIs** add exactly:

   ```
   http://127.0.0.1:8888/callback
   ```

   It must be the loopback IP `127.0.0.1`, not `localhost`, because Spotify
   rejects `localhost`. There must be no trailing slash and no `https`.
3. Open the app's **Settings** and copy the **Client ID** and **Client secret**.

## 2. Add both accounts to the app's allowlist

Development-mode apps only work for Spotify accounts that are explicitly added.
In the app's settings, open **User Management** and add **both** the source and
the destination account (name and the email address each account signs in
with). A new app allows up to 5 users.

If you skip this, sign-in or the first API call fails with a 403 error.

## 3. Install

You need Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then paste your Client ID and Client secret into .env
```

## 4. Run: dry run, then the real run, then verify

```bash
python spotify_migrate.py --dry-run
python spotify_migrate.py
python spotify_migrate.py --verify
```

- **`--dry-run`** signs in to both accounts, reads everything, and prints and
  reports what would be copied. It writes nothing.
- **The real run** asks you to confirm which account is the source and which is
  the destination. It then shows the plan and asks once more before the first
  write. `--yes` skips that second question but never the account check.
- **`--verify`** re-reads both accounts. It compares every copied playlist
  track by track, in order, and checks that every Liked Song, album and artist
  is present on the destination. The migration is correct when it reports
  **zero divergence**. Its report goes to `verify-report.md`.

### Signing in to two accounts

Your browser opens twice: first for the source account, then for the
destination. Before each one, the script pauses and asks you to press Enter.
Use that pause to log out of Spotify in your browser
(<https://www.spotify.com/logout/>), otherwise Spotify silently reuses whichever
account is already signed in. Check which account the permission page shows
before clicking **Agree**. After each sign-in the script prints the account's
name and user ID. If both sign-ins are the same user, it stops and clears the
destination sign-in so you can try again.

Sign-ins are cached in `.cache-source` and `.cache-dest`. Delete one of these
files to sign in to that account again.

If sign-in fails, the script prints what to check: that both accounts are in
User Management, that the redirect URI is registered exactly as above, and that
port 8888 is free (`lsof -nP -iTCP:8888 -sTCP:LISTEN` shows what is using it).

## Options

```
--only playlists,liked,albums,artists   run only some stages (default: all)
--playlist-filter REGEX                 only playlists whose name matches, e.g. '^Road trip' or '(?i)chill'
--state FILE                            progress file (default .migrate-state.json)
--report FILE                           report file (default migration-report.md)
```

## Resuming and re-running

Progress is saved to `.migrate-state.json` after every batch. If a run stops
(Ctrl-C, network loss, a long Spotify rate limit), run the same command again
and it continues where it left off. A partly copied playlist is resumed by
checking what is actually on the destination, so no tracks are added twice.
Playlists that were already copied are not created again. The state file is tied
to the two accounts it was created with, and the script refuses to use it with
any other pair.

## What is not copied

The report (`migration-report.md`) lists everything that was skipped, and why:

- **Playlists you follow but don't own.** These are listed with their links so
  you can re-follow them from the destination account.
- **Local files and removed or unavailable tracks.** These are listed by
  playlist.
- **Podcast episodes** inside playlists, and podcasts or shows in general.
- **Custom playlist cover images.** The copies get Spotify's automatic mosaic
  cover instead.
- **Collaborators.** A collaborative playlist is copied as a regular playlist.
- **Listening history, top tracks and recommendations.** These cannot be
  transferred.

## Known limitation: Liked Songs order

Spotify records "date added" only to the nearest second, and its save endpoint
cannot set that date. Songs saved within the same second therefore share a
timestamp, and Spotify may list them in a different order from the source.
Every song is still copied. `--verify` reports whether the order matches.

## Tests

`test_spotify_migrate.py` runs the whole flow against an in-memory fake of the
Spotify API. The tests cover pagination, rate limits, server errors, crashes
mid-run, resume, dry run and verify. They need no network access:

```bash
python -m unittest -v test_spotify_migrate
```

## Licence

MIT. See [LICENSE](LICENSE).
