#!/usr/bin/env python3
"""Copy a Spotify library from one account (source) to another (destination).

Tracks are copied by Spotify URI only; nothing is ever looked up by name.
The source account is only ever read from. See README.md for setup and the
recommended run order: --dry-run, then a real run, then --verify.
"""

from __future__ import annotations

import argparse
import errno
import html
import json
import logging
import math
import os
import re
import secrets
import shutil
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from spotipy.oauth2 import SpotifyOAuth, start_local_http_server

DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
SOURCE_CACHE = ".cache-source"
DEST_CACHE = ".cache-dest"

SOURCE_SCOPES = (
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
    "user-follow-read",
)
DEST_SCOPES = (
    "playlist-modify-private",
    "playlist-modify-public",
    "user-library-modify",
    "user-follow-modify",
    # Read access to the destination is needed to resume a half-copied
    # playlist without duplicating tracks, and for --verify.
    "playlist-read-private",
    "user-library-read",
    "user-follow-read",
)

STAGES = ("playlists", "liked", "albums", "artists")
STAGE_LABELS = {
    "playlists": "Playlists",
    "liked": "Liked Songs",
    "albums": "Albums",
    "artists": "Artists",
}

# Spotify's February 2026 Web API changes lowered two limits below the
# classic values: playlist items page at most 50 at a time (was 100), and
# PUT /me/library, which replaced the per-type save/follow endpoints, takes
# at most 40 URIs per call (was 50).
PAGE_SIZE = 50
PLAYLIST_ADD_CHUNK = 100
LIBRARY_SAVE_CHUNK = 40

MAX_ATTEMPTS = 5  # for 5xx and network errors
MAX_RATE_LIMIT_RETRIES = 10
MAX_RETRY_AFTER = 15 * 60  # longer waits stop the run instead; progress is saved
AUTH_TIMEOUT = 5 * 60

AUTH_HELP = """
If sign-in keeps failing, check:
  * Development-mode apps only work for accounts added under "User Management"
    in the app's settings at https://developer.spotify.com/dashboard. Add BOTH
    accounts. The app owner also needs an active Spotify Premium subscription.
  * The app's Redirect URI must be exactly {redirect_uri}
    (the loopback IP 127.0.0.1, not "localhost").
  * Port {port} must be free. See what is using it with:
        lsof -nP -iTCP:{port} -sTCP:LISTEN
  * To sign in to one account again, delete its cache file ({cache_path}) and re-run."""


# --------------------------------------------------------------------------
# Errors


class MigrationError(Exception):
    """Stops the run. The report is still written and saved progress is kept."""


class ApiError(Exception):
    """A request failed for one item. Recorded in the report; the run continues."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class AmbiguousWrite(Exception):
    """A non-idempotent write hit a server or network error, so it may or may not have applied."""


# --------------------------------------------------------------------------
# Console output


class Console:
    """One self-updating line per stage on a terminal; plain lines otherwise."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.tty = self.stream.isatty()
        self._live = False

    def progress(self, text):
        if not self.tty:
            return
        width = shutil.get_terminal_size((100, 20)).columns - 1
        self.stream.write("\r\x1b[K" + text[:width])
        self.stream.flush()
        self._live = True

    def line(self, text=""):
        """Print a line, replacing the current progress line if there is one."""
        if self._live:
            self.stream.write("\r\x1b[K")
            self._live = False
        self.stream.write(text + "\n")
        self.stream.flush()

    def note(self, text):
        """Print a line below the current progress line instead of over it."""
        if self._live:
            self.stream.write("\n")
            self._live = False
        self.stream.write(text + "\n")
        self.stream.flush()


# --------------------------------------------------------------------------
# Spotify access


class ReadOnlySpotify(spotipy.Spotify):
    """A spotipy client that refuses every request except GET.

    Used for the source account always, and for the destination during
    --dry-run and --verify.
    """

    def _internal_call(self, method, url, payload, params):
        if method != "GET":
            raise MigrationError(f"Blocked {method} {url}: this client is read-only.")
        return super()._internal_call(method, url, payload, params)


def _error_text(exc):
    """'endpoint: message' from a SpotifyException, without query strings."""
    where, _, message = (exc.msg or "").partition(":\n")
    message = message.strip()
    if not message:
        return (exc.msg or "no details").strip()
    path = urlparse(where.strip()).path.replace("/v1/", "", 1)
    return f"{path}: {message}" if path else message


def _retry_after_seconds(exc):
    try:
        return max(1, math.ceil(float((exc.headers or {}).get("Retry-After"))))
    except (TypeError, ValueError):
        return 5


def _entry_item(entry):
    """The track/episode inside a playlist or saved-track entry.

    Since February 2026 playlist entries carry it in "item" ("track" is
    deprecated); saved-track entries still use "track".
    """
    if "item" in entry:
        return entry["item"]
    return entry.get("track")


class Api:
    """Retrying wrapper around one account's spotipy client."""

    def __init__(self, client, role, cache_path, console, read_only, sleep=time.sleep):
        self.client = client
        self.role = role
        self.cache_path = cache_path
        self.console = console
        self.read_only = read_only
        self.sleep = sleep
        self.user_id = None

    # -- request plumbing --------------------------------------------------

    def call(self, fn, *args, idempotent=True, **kwargs):
        """Call fn, honouring 429 Retry-After and retrying 5xx/network errors.

        Non-idempotent calls raise AmbiguousWrite on 5xx/network errors
        instead of retrying, so the caller can check whether the write
        landed before trying again.
        """
        transient_failures = 0
        rate_limited = 0
        while True:
            try:
                return fn(*args, **kwargs)
            except SpotifyException as exc:
                if exc.http_status == 429:
                    rate_limited += 1
                    self._wait_for_rate_limit(exc, rate_limited)
                    continue
                if not 500 <= exc.http_status < 600:
                    raise self._error(exc) from None
                failure = f"HTTP {exc.http_status} ({_error_text(exc)})"
            except (requests.ConnectionError, requests.Timeout) as exc:
                failure = f"network error ({type(exc).__name__})"
            if not idempotent:
                raise AmbiguousWrite(failure)
            transient_failures += 1
            if transient_failures >= MAX_ATTEMPTS:
                raise ApiError(f"gave up after {MAX_ATTEMPTS} attempts: {failure}")
            self.sleep(2 ** (transient_failures - 1))

    def _wait_for_rate_limit(self, exc, attempt):
        wait = _retry_after_seconds(exc)
        if wait > MAX_RETRY_AFTER:
            raise MigrationError(
                f"Spotify's rate limit asks for a {wait // 60}-minute pause before the next "
                "request. Progress is saved; re-run later to continue."
            )
        if attempt > MAX_RATE_LIMIT_RETRIES:
            raise MigrationError(
                "Spotify kept rate-limiting requests. Progress is saved; re-run later to continue."
            )
        self.console.note(f"  Rate limited by Spotify; waiting {wait}s as instructed (Retry-After).")
        self.sleep(wait)

    def _error(self, exc):
        text = _error_text(exc)
        if exc.http_status == 401:
            return MigrationError(
                f"Spotify rejected the {self.role} account's access token ({text}). "
                f"Delete {self.cache_path} and re-run to sign in again."
            )
        if exc.http_status == 403:
            return ApiError(
                f"403 Forbidden ({text}). For the {self.role} account this usually means a "
                f"missing OAuth scope (delete {self.cache_path} and re-run to re-authorise), "
                "the account not being on the app's User Management allowlist in the "
                "developer dashboard, or the app owner's Spotify Premium having lapsed "
                "(development-mode apps require it).",
                403,
            )
        return ApiError(f"HTTP {exc.http_status} ({text})", exc.http_status)

    def _pages(self, first_page, key=None):
        page = first_page
        while page:
            if key:
                page = page.get(key) or {}
            yield from page.get("items") or []
            if not page.get("next"):
                return
            page = self.call(self.client.next, page)

    # -- reads ---------------------------------------------------------------

    def me(self):
        return self.call(self.client.me)

    def playlists(self):
        return self._pages(self.call(self.client.current_user_playlists, limit=PAGE_SIZE))

    def playlist_entries(self, playlist_id):
        return self._pages(
            self.call(
                self.client.playlist_items,
                playlist_id,
                limit=PAGE_SIZE,
                additional_types=("track", "episode"),
            )
        )

    def playlist_uris(self, playlist_id):
        """Every entry's URI in order (None where Spotify returns no item)."""
        return [(_entry_item(e) or {}).get("uri") for e in self.playlist_entries(playlist_id)]

    def playlist_length(self, playlist_id):
        page = self.call(
            self.client.playlist_items, playlist_id, limit=1, additional_types=("track", "episode")
        )
        return page["total"]

    def saved_tracks(self):
        return self._pages(self.call(self.client.current_user_saved_tracks, limit=PAGE_SIZE))

    def saved_albums(self):
        return self._pages(self.call(self.client.current_user_saved_albums, limit=PAGE_SIZE))

    def followed_artists(self):
        return self._pages(
            self.call(self.client.current_user_followed_artists, limit=PAGE_SIZE), key="artists"
        )

    # -- writes --------------------------------------------------------------

    def _check_writable(self):
        if self.read_only:
            raise MigrationError(f"Internal error: attempted a write through the read-only {self.role} client.")

    def create_playlist(self, name, public, description, existing_ids):
        """Create a playlist and return its ID.

        existing_ids holds every playlist ID the account had before this call,
        so a request that failed but actually went through is adopted rather
        than repeated.
        """
        self._check_writable()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                created = self.call(
                    self.client.current_user_playlist_create,
                    name,
                    public=public,
                    collaborative=False,
                    description=description,
                    idempotent=False,
                )
                return created["id"]
            except AmbiguousWrite as failure:
                last_failure = failure
            self.sleep(2 ** (attempt - 1))
            for pl in self.playlists():
                if (
                    pl
                    and pl["id"] not in existing_ids
                    and pl.get("name") == name
                    and (pl.get("owner") or {}).get("id") == self.user_id
                ):
                    return pl["id"]
        raise ApiError(f"creating the playlist failed after {MAX_ATTEMPTS} attempts: {last_failure}")

    def add_to_playlist(self, playlist_id, uris, expected_before):
        """Append uris (at most 100). expected_before is the playlist's current length."""
        self._check_writable()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                self.call(
                    self.client._post,
                    f"playlists/{playlist_id}/items",
                    payload={"uris": uris},
                    idempotent=False,
                )
                return
            except AmbiguousWrite as failure:
                last_failure = failure
            self.sleep(2 ** (attempt - 1))
            # Only retry if the failed request demonstrably did not apply.
            length = self.playlist_length(playlist_id)
            if length == expected_before + len(uris):
                return
            if length != expected_before:
                raise ApiError(
                    f"after a failed request the destination playlist has {length} items "
                    f"(expected {expected_before} or {expected_before + len(uris)}); "
                    "not retrying, to avoid duplicates"
                )
        raise ApiError(f"adding tracks failed after {MAX_ATTEMPTS} attempts: {last_failure}")

    def save_to_library(self, uris):
        """Save tracks/albums or follow artists (at most 40 URIs). PUT is idempotent."""
        self._check_writable()
        self.call(self.client._put, "me/library", uris=",".join(uris))


# --------------------------------------------------------------------------
# Authentication


@dataclass
class Credentials:
    client_id: str
    client_secret: str
    redirect_uri: str

    @property
    def port(self):
        return urlparse(self.redirect_uri).port


def load_dotenv(path=".env"):
    """Minimal .env reader. Variables already set in the environment win."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def read_credentials():
    client_id = os.environ.get("SPOTIPY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIPY_CLIENT_SECRET", "").strip()
    redirect_uri = os.environ.get("SPOTIPY_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT_URI
    missing = [
        name
        for name, value in (("SPOTIPY_CLIENT_ID", client_id), ("SPOTIPY_CLIENT_SECRET", client_secret))
        if not value
    ]
    if missing:
        raise MigrationError(
            f"Missing {' and '.join(missing)}. Copy .env.example to .env and fill in the values "
            "from your app at https://developer.spotify.com/dashboard (see README.md)."
        )
    parsed = urlparse(redirect_uri)
    if parsed.hostname == "localhost":
        raise MigrationError(
            f"SPOTIPY_REDIRECT_URI is {redirect_uri}, but Spotify rejects 'localhost'. Use "
            f"{DEFAULT_REDIRECT_URI} and register exactly that string in the dashboard."
        )
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
        raise MigrationError(
            f"SPOTIPY_REDIRECT_URI must be a loopback address with a port, such as "
            f"{DEFAULT_REDIRECT_URI} (got {redirect_uri})."
        )
    return Credentials(client_id, client_secret, redirect_uri)


def authenticate(role, cache_path, scopes, creds, console, read_only, hint, pause=False):
    oauth = SpotifyOAuth(
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        redirect_uri=creds.redirect_uri,
        scope=" ".join(scopes),
        cache_handler=CacheFileHandler(cache_path=cache_path),
        state=secrets.token_urlsafe(16),
        show_dialog=True,
        open_browser=False,
    )
    help_text = AUTH_HELP.format(redirect_uri=creds.redirect_uri, port=creds.port, cache_path=cache_path)
    try:
        token = oauth.validate_token(oauth.cache_handler.get_cached_token())
    except SpotifyOauthError:
        token = None  # e.g. the refresh token was revoked; sign in again
    except requests.RequestException as exc:
        raise MigrationError(f"Could not reach Spotify ({type(exc).__name__}). Check your connection.") from None
    if token:
        console.line(f"Using the saved {role} sign-in from {cache_path}.")
    else:
        browser_sign_in(oauth, role, creds, console, hint, help_text, pause)
    client_class = ReadOnlySpotify if read_only else spotipy.Spotify
    # A plain Session has no automatic retries, so Api.call sees every 429
    # and 5xx itself and can honour Retry-After and avoid duplicate writes.
    return client_class(auth_manager=oauth, requests_session=requests.Session(), requests_timeout=30)


def browser_sign_in(oauth, role, creds, console, hint, help_text, pause=False):
    try:
        server = start_local_http_server(creds.port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise MigrationError(
                f"Port {creds.port} is already in use, so the sign-in redirect cannot be received. "
                "Stop whatever is using it and re-run." + help_text
            ) from None
        raise
    console.line(hint)
    if pause and sys.stdin.isatty():
        try:
            input("  Press Enter to open the sign-in page... ")
        except EOFError:
            pass
    console.line(f"  Waiting up to {AUTH_TIMEOUT // 60} minutes for Spotify to redirect to {creds.redirect_uri} ...")
    try:
        auth_url = oauth.get_authorize_url()
        if not webbrowser.open(auth_url):
            raise MigrationError("Could not open a web browser for the Spotify sign-in.")
        deadline = time.monotonic() + AUTH_TIMEOUT
        while server.auth_code is None and server.error is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()
    if server.error is not None:
        raise MigrationError(f"Spotify sign-in for the {role} account failed: {server.error}." + help_text)
    if server.auth_code is None:
        raise MigrationError(
            f"No sign-in response for the {role} account within {AUTH_TIMEOUT // 60} minutes. "
            "If the browser showed 'INVALID_CLIENT: Invalid redirect URI', the redirect URI "
            "is not registered on the app." + help_text
        )
    if getattr(server, "state", None) != oauth.state:
        raise MigrationError("The sign-in response did not match this session (state mismatch). Please re-run.")
    try:
        oauth.get_access_token(code=server.auth_code, as_dict=False, check_cache=False)
    except SpotifyOauthError as exc:
        raise MigrationError(f"Spotify rejected the sign-in for the {role} account: {exc}." + help_text) from None


def identity(me):
    return {
        "id": me["id"],
        "name": me.get("display_name") or me["id"],
        # Spotify stopped returning email for development-mode apps in 2026.
        "email": me.get("email"),
    }


def describe_identity(ident):
    email = ident["email"] or "email not provided by Spotify"
    return f"{ident['name']}  [user id: {ident['id']}, {email}]"


def whoami(api, role, report, console, cache_path, creds):
    try:
        me = api.me()
    except ApiError as exc:
        help_text = AUTH_HELP.format(redirect_uri=creds.redirect_uri, port=creds.port, cache_path=cache_path)
        raise MigrationError(f"Could not read the {role} account's profile: {exc}" + help_text) from None
    api.user_id = me["id"]
    ident = identity(me)
    report.accounts[role] = ident
    console.line(f"  {role.upper()}: {describe_identity(ident)}")
    return ident


def ask(question):
    if not sys.stdin.isatty():
        raise MigrationError("A confirmation is needed but stdin is not a terminal. Run this interactively.")
    try:
        answer = input(question)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


# --------------------------------------------------------------------------
# State file


def new_state():
    return {
        "source_user_id": None,
        "destination_user_id": None,
        "playlists": {},
        "liked_tracks_added": 0,
        "albums_done": False,
        "artists_done": False,
    }


def load_state(path):
    state = new_state()
    if not os.path.exists(path):
        return state
    try:
        with open(path, encoding="utf-8") as fh:
            state.update(json.load(fh))
    except (OSError, ValueError) as exc:
        raise MigrationError(f"Could not read the state file {path}: {exc}") from None
    return state


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def check_state_accounts(state, path, source_id, dest_id):
    recorded = (state.get("source_user_id"), state.get("destination_user_id"))
    if recorded == (None, None) or recorded == (source_id, dest_id):
        return
    raise MigrationError(
        f"The state file {path} belongs to a different pair of accounts "
        f"(source {recorded[0]}, destination {recorded[1]}), but you are signed in as "
        f"source {source_id}, destination {dest_id}. Refusing to reuse it. If you signed "
        f"in to the wrong accounts, delete {SOURCE_CACHE} and/or {DEST_CACHE} and re-run; "
        "otherwise pass --state with a different file."
    )


# --------------------------------------------------------------------------
# Report


def plural(count, word):
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def md(text):
    """Escape characters that would change markdown rendering."""
    return re.sub(r"([\\`*_\[\]<>|#])", r"\\\1", str(text))


SKIP_SECTIONS = (
    ("local", "Skipped local files", "Local files are not stored on Spotify and cannot be copied by URI."),
    ("unavailable", "Unavailable or removed tracks", "Spotify returned no track for these entries (removed from the catalogue or otherwise unavailable)."),
    ("episode", "Skipped podcast episodes", "Podcasts and shows are out of scope."),
)


class Report:
    def __init__(self, mode, stages, pattern):
        self.started = datetime.now(timezone.utc)
        self.mode = mode  # "run", "dry-run" or "verify"
        self.stages = stages
        self.pattern = pattern
        self.accounts = {}
        self.counts = dict.fromkeys(("playlists", "tracks", "liked", "albums", "artists"), 0)
        self.totals = None
        self.skipped = {kind: {} for kind, _, _ in SKIP_SECTIONS}
        self.not_owned = []
        self.collaborative = []
        self.plan = []
        self.playlist_plan = []
        self.verify = []
        self.errors = []
        self.warnings = []
        self.fatal = None

    def skip(self, kind, group_id, group_name, label):
        self.skipped[kind].setdefault(group_id, (group_name, []))[1].append(label)

    def error(self, item, message):
        self.errors.append((item, message))

    def render(self):
        mode_label = {
            "run": "migration",
            "dry-run": "dry run (nothing was written)",
            "verify": "verification (read-only)",
        }[self.mode]
        out = ["# Spotify migration report", ""]
        out.append(f"- **Run:** {self.started:%Y-%m-%d %H:%M:%S} UTC, {mode_label}")
        for role in ("source", "destination"):
            ident = self.accounts.get(role)
            text = md(describe_identity(ident)) if ident else "not signed in"
            out.append(f"- **{role.capitalize()}:** {text}")
        stages = ", ".join(STAGE_LABELS[s] for s in self.stages)
        out.append(f"- **Stages:** {stages}")
        if self.pattern:
            out.append(f"- **Playlist filter:** `{self.pattern.pattern}`")
        if self.fatal:
            out += ["", f"> **The run stopped early:** {md(self.fatal)}"]
            if self.mode == "run":
                out.append("> Everything finished before this point is recorded in the state file; re-run to resume.")

        if self.mode == "run":
            out += ["", "## Counts", "", "| | This run | Total copied so far |", "|---|---:|---:|"]
            totals = self.totals or {}
            rows = (
                ("Playlists copied", "playlists"),
                ("Tracks added to playlists", "tracks"),
                ("Liked Songs saved", "liked"),
                ("Albums saved", "albums"),
                ("Artists followed", "artists"),
            )
            for label, key in rows:
                total = totals.get(key, "")
                out.append(f"| {label} | {self.counts[key]:,} | {total} |")
        if self.plan:
            out += ["", "## Plan (dry run: nothing was written)", "", "```"] + self.plan + ["```"]
            if self.playlist_plan:
                out += ["", "### Playlists", ""] + [f"- {line}" for line in self.playlist_plan]
        if self.verify:
            diverged = [v for v in self.verify if v[1] is False]
            out += ["", "## Verification", ""]
            out.append(
                "Zero divergence: every copied item matches the source."
                if not diverged
                else f"**{len(diverged)} item(s) diverge from the source.**"
            )
            out.append("")
            for item, ok, detail in self.verify:
                status = {True: "OK", False: "DIVERGES", None: "not copied yet"}[ok]
                out.append(f"- **{status}**: {md(item)}: {md(detail)}")

        if self.errors:
            out += ["", "## Errors", ""]
            out += [f"- **{md(item)}**: {md(message)}" for item, message in self.errors]
        if self.warnings:
            out += ["", "## Warnings", ""] + [f"- {md(w)}" for w in self.warnings]

        for kind, title, blurb in SKIP_SECTIONS:
            groups = self.skipped[kind]
            if not groups:
                continue
            total = sum(len(labels) for _, labels in groups.values())
            out += ["", f"## {title} ({total})", "", blurb]
            for name, labels in groups.values():
                out += ["", f"### {md(name)}", ""] + [f"- {md(label)}" for label in labels]

        if self.not_owned:
            out += [
                "",
                f"## Playlists you follow but do not own ({len(self.not_owned)})",
                "",
                "These were not copied. Re-follow them manually while signed in to the destination account:",
                "",
            ]
            for pl in self.not_owned:
                extra = " (collaborative: ask the owner to invite the new account)" if pl["collaborative"] else ""
                out.append(f"- [{md(pl['name'])}]({pl['url']}) by {md(pl['owner'])}{extra}")

        out += ["", "## Notes", ""]
        out.append(
            "- Custom playlist cover images are not transferred. Spotify shows its "
            "automatic mosaic cover on the copies; re-upload custom covers by hand."
        )
        if self.collaborative:
            names = ", ".join(md(n) for n in self.collaborative)
            out.append(
                f"- These playlists were collaborative on the source and were copied as regular "
                f"playlists; collaborators are not transferred: {names}"
            )
        out.append(
            "- Not transferable by design: local files, podcasts/shows, listening history, "
            "top tracks and recommendations."
        )
        return "\n".join(out) + "\n"

    def write(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.render())


# --------------------------------------------------------------------------
# Reading the source


def describe_track(track):
    if not track or not track.get("name"):
        return "(name unavailable)"
    artists = ", ".join(a["name"] for a in track.get("artists") or [] if a and a.get("name"))
    if not artists and track.get("show"):
        artists = track["show"].get("name") or ""
    return f"{artists} - {track['name']}" if artists else track["name"]


def classify_entry(entry):
    """Return (uri, None) for a copyable track, or (None, (kind, description)) to skip it."""
    track = _entry_item(entry)
    uri = (track or {}).get("uri") or ""
    if entry.get("is_local") or (track or {}).get("is_local") or uri.startswith("spotify:local:"):
        return None, ("local", describe_track(track))
    if not uri:
        return None, ("unavailable", describe_track(track))
    if (track or {}).get("type") == "episode" or uri.startswith("spotify:episode:"):
        return None, ("episode", describe_track(track))
    if not uri.startswith("spotify:track:"):
        return None, ("unavailable", f"{describe_track(track)} ({uri})")
    return uri, None


@dataclass
class SourcePlaylist:
    id: str
    name: str
    description: str
    public: bool
    uris: list  # copyable track URIs, in source order


@dataclass
class Snapshot:
    playlists: list = None
    liked: list = None  # oldest first
    albums: list = None  # oldest first
    artists: list = None


def compare_lists(expected, actual):
    if expected == actual:
        return True, f"{len(actual):,} tracks, same order"
    detail = f"source has {len(expected):,} copyable tracks, destination has {len(actual):,}"
    first = next(
        (i for i, (a, b) in enumerate(zip(expected, actual)) if a != b),
        min(len(expected), len(actual)),
    )
    src = expected[first] if first < len(expected) else "(end)"
    dst = actual[first] if first < len(actual) else "(end)"
    return False, f"{detail}; first difference at position {first + 1}: source {src}, destination {dst}"


class Migration:
    def __init__(self, src, dst, state, state_path, report, console, stages, pattern):
        self.src = src
        self.dst = dst
        self.state = state
        self.state_path = state_path
        self.report = report
        self.console = console
        self.stages = stages
        self.pattern = pattern
        self.quiet_reads = False

    def save(self):
        save_state(self.state_path, self.state)

    # -- read ------------------------------------------------------------------

    def read(self):
        snap = Snapshot()
        if "playlists" in self.stages:
            snap.playlists = self._read_stage("Playlists", self.read_playlists)
        if "liked" in self.stages:
            snap.liked = self._read_stage("Liked Songs", self.read_liked)
        if "albums" in self.stages:
            snap.albums = self._read_stage("Albums", self.read_albums)
        if "artists" in self.stages:
            snap.artists = self._read_stage("Artists", self.read_artists)
        return snap

    def _read_line(self, text):
        if not self.quiet_reads:
            self.console.line(text)

    def _read_stage(self, label, reader):
        try:
            return reader()
        except ApiError as exc:
            self.report.error(f"{label} (reading source)", str(exc))
            self.console.line(f"  {label:<12} could not be read: {exc}")
            return None

    def read_playlists(self):
        owned, followed = [], []
        for pl in self.src.playlists():
            if not pl:
                continue
            if self.pattern and not self.pattern.search(pl.get("name") or ""):
                continue
            owner = pl.get("owner") or {}
            (owned if owner.get("id") == self.src.user_id else followed).append(pl)
        for pl in followed:
            owner = pl.get("owner") or {}
            self.report.not_owned.append(
                {
                    "name": pl.get("name") or pl["id"],
                    "owner": owner.get("display_name") or owner.get("id") or "unknown",
                    "url": (pl.get("external_urls") or {}).get("spotify")
                    or f"https://open.spotify.com/playlist/{pl['id']}",
                    "collaborative": bool(pl.get("collaborative")),
                }
            )
        result = []
        for i, pl in enumerate(owned, 1):
            self.console.progress(f"  Playlists    reading {i}/{len(owned)}: {pl.get('name')}")
            try:
                result.append(self.read_playlist(pl))
            except ApiError as exc:
                self.report.error(f"Playlist '{pl.get('name')}' (reading source)", str(exc))
        tracks = sum(len(p.uris) for p in result)
        self._read_line(
            f"  Playlists    {len(result)} owned ({tracks:,} tracks), "
            f"{len(followed)} followed but not owned"
        )
        return result

    def read_playlist(self, pl):
        name = pl.get("name") or pl["id"]
        uris = []
        for position, entry in enumerate(self.src.playlist_entries(pl["id"]), 1):
            uri, skipped = classify_entry(entry)
            if uri:
                uris.append(uri)
            else:
                kind, text = skipped
                self.report.skip(kind, pl["id"], name, f"#{position}: {text}")
        if pl.get("collaborative"):
            self.report.collaborative.append(name)
        return SourcePlaylist(
            id=pl["id"],
            name=name,
            # Spotify returns descriptions HTML-escaped; unescape so the copy isn't double-escaped.
            description=html.unescape(pl.get("description") or ""),
            public=bool(pl.get("public")),
            uris=uris,
        )

    def read_liked(self):
        uris = []
        for position, entry in enumerate(self.src.saved_tracks(), 1):
            self.console.progress(f"  Liked Songs  reading {position:,}")
            uri, skipped = classify_entry(entry)
            if uri:
                uris.append(uri)
            else:
                kind, text = skipped
                self.report.skip(kind, "liked", "Liked Songs", f"#{position}: {text}")
        # Spotify lists saved tracks newest first. Save oldest first so the
        # destination's "date added" order matches the source.
        uris.reverse()
        self._read_line(f"  Liked Songs  {len(uris):,}")
        return uris

    def read_albums(self):
        uris = []
        for entry in self.src.saved_albums():
            album = (entry or {}).get("album") or {}
            if album.get("uri"):
                uris.append(album["uri"])
            else:
                self.report.skip("unavailable", "albums", "Saved albums", describe_track(album))
        uris.reverse()  # newest first -> oldest first, as for Liked Songs
        self._read_line(f"  Albums       {len(uris):,}")
        return uris

    def read_artists(self):
        uris = [a["uri"] for a in self.src.followed_artists() if a and a.get("uri")]
        self._read_line(f"  Artists      {len(uris):,}")
        return uris

    # -- plan ------------------------------------------------------------------

    def plan(self, snap):
        """Describe the pending work. Returns (lines, number of pending writes)."""
        lines = []
        pending = 0
        if snap.playlists is not None:
            new = resume = done = tracks = 0
            for pl in snap.playlists:
                entry = self.state["playlists"].get(pl.id)
                if entry and entry.get("complete"):
                    done += 1
                    self.report.playlist_plan.append(f"{md(pl.name)}: already copied")
                elif entry:
                    resume += 1
                    remaining = max(0, len(pl.uris) - entry.get("tracks_added", 0))
                    tracks += remaining
                    self.report.playlist_plan.append(
                        f"{md(pl.name)}: resume, {remaining:,} of {len(pl.uris):,} tracks left"
                    )
                else:
                    new += 1
                    tracks += len(pl.uris)
                    self.report.playlist_plan.append(f"{md(pl.name)}: create, {len(pl.uris):,} tracks")
            pending += new + resume + tracks
            lines.append(
                f"  Playlists    {new} to create, {resume} to resume, {done} already copied: "
                f"{tracks:,} tracks to add"
            )
            skipped = {kind: sum(len(v[1]) for v in groups.values()) for kind, groups in self.report.skipped.items()}
            if any(skipped.values()):
                lines.append(
                    f"               skipping {plural(skipped['local'], 'local file')}, "
                    f"{plural(skipped['unavailable'], 'unavailable track')}, "
                    f"{plural(skipped['episode'], 'podcast episode')} (listed in the report)"
                )
            if self.report.not_owned:
                lines.append(
                    f"               {plural(len(self.report.not_owned), 'followed playlist')} you don't own "
                    "will be listed in the report to re-follow by hand"
                )
        library = (
            ("Liked Songs", snap.liked, "liked_tracks_added", None, "save"),
            ("Albums", snap.albums, "albums_added", "albums_done", "save"),
            ("Artists", snap.artists, "artists_added", "artists_done", "follow"),
        )
        for label, uris, count_key, done_key, verb in library:
            if uris is None:
                continue
            if done_key and self.state.get(done_key):
                lines.append(f"  {label:<12} already complete")
                continue
            remaining = max(0, len(uris) - self.state.get(count_key, 0))
            pending += remaining
            lines.append(f"  {label:<12} {remaining:,} of {len(uris):,} to {verb}")
        return lines, pending

    # -- write -----------------------------------------------------------------

    def write(self, snap):
        if snap.playlists is not None:
            self.copy_playlists(snap.playlists)
        if snap.liked is not None:
            self.copy_library("Liked Songs", "liked", snap.liked, "liked_tracks_added", "liked_last_uri", None)
        if snap.albums is not None:
            self.copy_library("Albums", "albums", snap.albums, "albums_added", "albums_last_uri", "albums_done")
        if snap.artists is not None:
            self.copy_library("Artists", "artists", snap.artists, "artists_added", "artists_last_uri", "artists_done")

    def copy_playlists(self, playlists):
        todo = [pl for pl in playlists if not self.state["playlists"].get(pl.id, {}).get("complete")]
        if not todo:
            self.console.line("  Playlists    already complete")
            return
        existing_ids = set()
        if any(pl.id not in self.state["playlists"] for pl in todo):
            try:
                existing_ids = {pl["id"] for pl in self.dst.playlists() if pl}
            except ApiError as exc:
                self.report.error("Playlists (listing destination)", str(exc))
                self.console.line(f"  Playlists    could not list destination playlists: {exc}")
                return
        failed = 0
        # New playlists appear at the top of the destination's library, so
        # create them bottom-up to keep the source's relative order.
        for i, pl in enumerate(reversed(todo), 1):
            try:
                self.copy_playlist(pl, existing_ids, f"  Playlists    {i}/{len(todo)}")
            except ApiError as exc:
                failed += 1
                self.report.error(f"Playlist '{pl.name}'", str(exc))
        summary = f"  Playlists    {len(todo) - failed}/{len(todo)} copied, {self.report.counts['tracks']:,} tracks added"
        if failed:
            summary += f" ({failed} failed, see the report)"
        self.console.line(summary)

    def copy_playlist(self, pl, existing_ids, prefix):
        self.console.progress(f"{prefix}: {pl.name}")
        entry = self.state["playlists"].get(pl.id)
        if entry is None:
            dest_id = self.dst.create_playlist(pl.name, pl.public, pl.description, existing_ids)
            existing_ids.add(dest_id)
            entry = {"name": pl.name, "dest_id": dest_id, "tracks_added": 0, "complete": False}
            self.state["playlists"][pl.id] = entry
            self.save()
            done = 0
        else:
            # Resuming. Trust what is actually on the destination rather than
            # the recorded count: a chunk may have landed just before a crash.
            current = self.dst.playlist_uris(entry["dest_id"])
            if current != pl.uris[: len(current)]:
                raise ApiError(
                    "the partial copy on the destination no longer matches the start of the "
                    "source playlist, so it was left untouched. Delete the destination copy and "
                    "remove this playlist's entry from the state file to copy it again"
                )
            done = len(current)
            if done != entry["tracks_added"]:
                entry["tracks_added"] = done
                self.save()
        for start in range(done, len(pl.uris), PLAYLIST_ADD_CHUNK):
            chunk = pl.uris[start : start + PLAYLIST_ADD_CHUNK]
            self.dst.add_to_playlist(entry["dest_id"], chunk, expected_before=start)
            entry["tracks_added"] = start + len(chunk)
            self.save()
            self.report.counts["tracks"] += len(chunk)
            self.console.progress(f"{prefix}: {pl.name} ({entry['tracks_added']:,}/{len(pl.uris):,})")
        entry["complete"] = True
        self.save()
        self.report.counts["playlists"] += 1

    def _resume_index(self, label, uris, count_key, last_key):
        count = min(self.state.get(count_key, 0), len(uris))
        last = self.state.get(last_key)
        if count == 0 or not last or uris[count - 1] == last:
            return count
        if last in uris:
            # The source list shifted since the last run; resume after the
            # last item actually saved.
            return uris.index(last) + 1
        self.report.warnings.append(
            f"{label}: the source changed since the last run and the resume point could not be "
            f"located; resumed from item {count + 1}. Run --verify to check for gaps."
        )
        return count

    def copy_library(self, label, count_name, uris, count_key, last_key, done_key):
        if done_key and self.state.get(done_key):
            self.console.line(f"  {label:<12} already complete")
            return
        start = self._resume_index(label, uris, count_key, last_key)
        for i in range(start, len(uris), LIBRARY_SAVE_CHUNK):
            chunk = uris[i : i + LIBRARY_SAVE_CHUNK]
            try:
                self._save_chunk(label, chunk)
            except ApiError as exc:
                self.report.error(f"{label} (stopped at item {i + 1} of {len(uris)})", str(exc))
                self.console.line(f"  {label:<12} {i:,}/{len(uris):,}, stopped: {exc}")
                return
            self.state[count_key] = i + len(chunk)
            self.state[last_key] = chunk[-1]
            self.save()
            self.report.counts[count_name] += len(chunk)
            self.console.progress(f"  {label:<12} {i + len(chunk):,}/{len(uris):,}")
        if done_key:
            self.state[done_key] = True
            self.save()
        self.console.line(f"  {label:<12} {len(uris):,}/{len(uris):,} done")

    def _save_chunk(self, label, chunk):
        try:
            self.dst.save_to_library(chunk)
            return
        except ApiError as exc:
            if exc.status not in (400, 404) or len(chunk) == 1:
                raise
            chunk_error = exc
        # One bad item (e.g. withdrawn from the catalogue) can fail a whole
        # chunk. Save items one by one and record just the ones that fail.
        failed = []
        for uri in chunk:
            try:
                self.dst.save_to_library([uri])
            except ApiError as exc:
                failed.append((uri, exc))
        if len(failed) == len(chunk):
            raise chunk_error
        for uri, exc in failed:
            self.report.error(f"{label}: {uri}", str(exc))

    def totals(self, snap):
        totals = {}
        if snap.playlists is not None:
            entries = [self.state["playlists"].get(pl.id) or {} for pl in snap.playlists]
            complete = sum(1 for e in entries if e.get("complete"))
            totals["playlists"] = f"{complete:,} of {len(snap.playlists):,}"
            totals["tracks"] = f"{sum(e.get('tracks_added', 0) for e in entries):,}"
        for key, uris, count_key in (
            ("liked", snap.liked, "liked_tracks_added"),
            ("albums", snap.albums, "albums_added"),
            ("artists", snap.artists, "artists_added"),
        ):
            if uris is not None:
                totals[key] = f"{min(self.state.get(count_key, 0), len(uris)):,} of {len(uris):,}"
        return totals

    # -- verify ------------------------------------------------------------------

    def verify(self):
        """Compare the destination with the source. Returns the number of divergences."""
        diverged = 0
        self.quiet_reads = True
        if "playlists" in self.stages:
            diverged += self.verify_playlists()
        library = (
            ("liked", "Liked Songs", self.read_liked, self.dst.saved_tracks, lambda e: (e.get("track") or {}).get("uri")),
            ("albums", "Albums", self.read_albums, self.dst.saved_albums, lambda e: (e.get("album") or {}).get("uri")),
            ("artists", "Artists", self.read_artists, self.dst.followed_artists, lambda e: e.get("uri")),
        )
        for stage, label, read_source, read_dest, uri_of in library:
            if stage in self.stages:
                diverged += self.verify_library(label, read_source, read_dest, uri_of)
        return diverged

    def verify_playlists(self):
        try:
            sources = self.read_playlists()
        except ApiError as exc:
            self.report.verify.append(("Playlists", False, f"source could not be read: {exc}"))
            self.console.line(f"  Playlists    source could not be read: {exc}")
            return 1
        matched = diverged = not_copied = 0
        for i, pl in enumerate(sources, 1):
            self.console.progress(f"  Playlists    checking {i}/{len(sources)}: {pl.name}")
            entry = self.state["playlists"].get(pl.id)
            if not entry:
                not_copied += 1
                self.report.verify.append((pl.name, None, "no copy recorded in the state file"))
                continue
            try:
                actual = self.dst.playlist_uris(entry["dest_id"])
            except ApiError as exc:
                ok, detail = False, f"could not read the destination copy: {exc}"
            else:
                ok, detail = compare_lists(pl.uris, actual)
                if not entry.get("complete"):
                    detail += " (copy not finished yet)"
            if ok:
                matched += 1
            else:
                diverged += 1
            self.report.verify.append((pl.name, ok, detail))
        checked = matched + diverged
        line = f"  Playlists    {matched}/{checked} copied playlists match"
        if diverged:
            line += f", {diverged} DIVERGE"
        if not_copied:
            line += f", {not_copied} not copied yet"
        self.console.line(line)
        return diverged

    def verify_library(self, label, read_source, read_dest, uri_of):
        try:
            expected = read_source()
            actual = [uri_of(e) for e in read_dest() if e]
        except ApiError as exc:
            self.report.verify.append((label, False, f"could not be read: {exc}"))
            self.console.line(f"  {label:<12} could not be read: {exc}")
            return 1
        present = set(actual)
        missing = [u for u in expected if u not in present]
        if missing:
            detail = f"{len(missing):,} of {len(expected):,} missing on the destination, e.g. {', '.join(missing[:5])}"
        else:
            detail = f"all {len(expected):,} present"
        if label == "Liked Songs" and not missing:
            wanted = set(expected)
            dest_order = [u for u in reversed(actual) if u in wanted]  # oldest first
            if dest_order == expected:
                detail += "; date-added order matches the source"
            else:
                detail += (
                    "; date-added order differs from the source (expected if some of these "
                    "songs were already liked on the destination before the migration)"
                )
        self.report.verify.append((label, not missing, detail))
        self.console.line(f"  {label:<12} {'OK' if not missing else 'DIVERGES'}: {detail}")
        return 1 if missing else 0


# --------------------------------------------------------------------------
# CLI


def parse_stages(value):
    stages = [s.strip().lower() for s in value.split(",") if s.strip()]
    unknown = [s for s in stages if s not in STAGES]
    if unknown or not stages:
        raise argparse.ArgumentTypeError(
            f"unknown stage(s) {', '.join(unknown) or '(none given)'}; choose from {','.join(STAGES)}"
        )
    return tuple(s for s in STAGES if s in stages)


def parse_regex(value):
    try:
        return re.compile(value)
    except re.error as exc:
        raise argparse.ArgumentTypeError(f"invalid regex: {exc}") from None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="spotify_migrate.py",
        description=(
            "Copy playlists, Liked Songs, saved albums and followed artists from one Spotify "
            "account to another, by URI. The source account is only ever read."
        ),
        epilog="Recommended order: --dry-run, then a real run, then --verify. See README.md.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="sign in and read everything, but write nothing")
    mode.add_argument("--verify", action="store_true", help="re-read both accounts and compare the copies with the source")
    parser.add_argument(
        "--only",
        type=parse_stages,
        default=STAGES,
        metavar="STAGES",
        help="comma-separated subset of playlists,liked,albums,artists (default: all)",
    )
    parser.add_argument(
        "--playlist-filter",
        type=parse_regex,
        metavar="REGEX",
        help="only playlists whose name matches this Python regex (case-sensitive; start with (?i) to ignore case)",
    )
    parser.add_argument("--state", default=".migrate-state.json", metavar="FILE", help="progress file for resuming (default: %(default)s)")
    parser.add_argument(
        "--report",
        metavar="FILE",
        help="markdown report path (default: migration-report.md, or verify-report.md with --verify)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the 'start copying?' prompt (the source/destination account check is always asked)",
    )
    return parser.parse_args(argv)


def run(args, console, report):
    creds = read_credentials()
    read_only_dest = args.dry_run or args.verify

    console.line("Step 1 of 2: sign in to the SOURCE account (the one you are copying FROM).")
    src_client = authenticate(
        "source",
        SOURCE_CACHE,
        SOURCE_SCOPES,
        creds,
        console,
        read_only=True,
        hint=(
            "  Make sure your browser is signed in to Spotify as the SOURCE account, or logged out\n"
            "  (https://www.spotify.com/logout/). Check which account the permission page shows\n"
            "  before agreeing."
        ),
        pause=True,
    )
    src = Api(src_client, "source", SOURCE_CACHE, console, read_only=True)
    src_me = whoami(src, "source", report, console, SOURCE_CACHE, creds)

    console.line("Step 2 of 2: sign in to the DESTINATION account (the one you are copying TO).")
    dst_client = authenticate(
        "destination",
        DEST_CACHE,
        DEST_SCOPES,
        creds,
        console,
        read_only=read_only_dest,
        hint=(
            "  Your browser is probably still signed in to the source account. First log out of\n"
            "  Spotify in your browser (https://www.spotify.com/logout/), then sign in to the\n"
            "  destination account when the permission page opens."
        ),
        pause=True,
    )
    dst = Api(dst_client, "destination", DEST_CACHE, console, read_only=read_only_dest)
    dst_me = whoami(dst, "destination", report, console, DEST_CACHE, creds)
    if src_me["id"] == dst_me["id"]:
        # The saved destination sign-in is for the wrong account; clear it so
        # the next run asks again instead of silently reusing it.
        if os.path.exists(DEST_CACHE):
            os.remove(DEST_CACHE)
        raise MigrationError(
            f"Both sign-ins are the same Spotify user ({src_me['id']}), so nothing was done. "
            f"The destination sign-in has been cleared. Log out of Spotify in your browser "
            "(https://www.spotify.com/logout/), then re-run and sign in to the destination account."
        )
    return execute(args, src, dst, src_me, dst_me, console, report)


def execute(args, src, dst, src_me, dst_me, console, report, confirm=ask):
    """Everything after sign-in. Returns the process exit code."""
    if src_me["id"] == dst_me["id"]:
        raise MigrationError(
            f"Both sign-ins are the same Spotify user ({src_me['id']}). Delete {DEST_CACHE}, "
            "re-run, and sign in to the destination account (use 'Not you?' on Spotify's page)."
        )

    state = load_state(args.state)
    check_state_accounts(state, args.state, src_me["id"], dst_me["id"])
    migration = Migration(src, dst, state, args.state, report, console, args.only, args.playlist_filter)

    if args.verify:
        if not os.path.exists(args.state):
            raise MigrationError(f"Nothing to verify: there is no state file at {args.state}. Run the migration first.")
        console.line("Verifying (read-only):")
        diverged = migration.verify()
        console.line("Zero divergence." if not diverged else f"{diverged} item(s) diverge; see the report.")
        return 0 if not diverged else 1

    if not args.dry_run:
        console.line("")
        console.line("Check these carefully. Nothing is ever written to the source account.")
        console.line(f"  Copy FROM (source, read-only): {describe_identity(src_me)}")
        console.line(f"  Copy TO   (destination):       {describe_identity(dst_me)}")
        if not confirm("Is this the right way round? [y/N] "):
            raise MigrationError(
                f"Stopped at the account check; nothing was written. To change accounts, delete "
                f"{SOURCE_CACHE} and/or {DEST_CACHE} and re-run."
            )

    console.line("Reading the source library:")
    snap = migration.read()
    lines, pending = migration.plan(snap)
    console.line("Plan:")
    for line in lines:
        console.line(line)

    if args.dry_run:
        report.plan = lines
        console.line("Dry run: nothing was written.")
        return 0
    report.totals = migration.totals(snap)
    if pending == 0:
        console.line("Nothing to copy: everything in scope is already on the destination.")
        return 1 if report.errors else 0
    if not args.yes and not confirm("Start copying to the destination account? [y/N] "):
        raise MigrationError("Cancelled before writing; nothing was written.")

    state["source_user_id"] = src_me["id"]
    state["destination_user_id"] = dst_me["id"]
    migration.save()
    console.line("Copying to the destination:")
    try:
        migration.write(snap)
    finally:
        report.totals = migration.totals(snap)
    console.line("Done. Next, run with --verify to check the copies.")
    return 1 if report.errors else 0


def main(argv=None):
    args = parse_args(argv)
    # spotipy logs every HTTP error itself; this script reports them instead.
    logging.getLogger("spotipy").setLevel(logging.CRITICAL)
    load_dotenv()
    mode = "verify" if args.verify else "dry-run" if args.dry_run else "run"
    report_path = args.report or ("verify-report.md" if args.verify else "migration-report.md")
    console = Console()
    report = Report(mode, args.only, args.playlist_filter)
    exit_code = 1
    try:
        exit_code = run(args, console, report)
    except (MigrationError, ApiError) as exc:
        report.fatal = str(exc)
        console.note(f"\nStopped: {exc}")
    except KeyboardInterrupt:
        report.fatal = "Interrupted (Ctrl-C)."
        console.note("\nInterrupted. Progress up to the last completed chunk is saved; re-run to resume.")
        exit_code = 130
    except Exception as exc:
        report.fatal = f"Unexpected error: {type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            report.write(report_path)
            console.note(f"Report written to {report_path}")
        except OSError as exc:
            console.note(f"Could not write the report to {report_path}: {exc}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
