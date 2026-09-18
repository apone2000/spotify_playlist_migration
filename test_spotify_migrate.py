"""Offline tests for spotify_migrate.py against an in-memory fake of the Spotify API.

Run with:  python -m unittest -v test_spotify_migrate
No network access or Spotify accounts are needed.
"""

import argparse
import io
import itertools
import json
import os
import tempfile
import unittest
from collections import Counter

from spotipy.exceptions import SpotifyException

import spotify_migrate as sm


class Crash(BaseException):
    """Simulates the process dying mid-run (not caught by the script's handlers)."""


def track(n, **extra):
    t = {"type": "track", "uri": f"spotify:track:t{n}", "name": f"Song {n}", "artists": [{"name": f"Artist {n}"}], "is_local": False}
    t.update(extra)
    return t


def local_track(name):
    return {"type": "track", "uri": f"spotify:local:Someone:Album:{name}:180", "name": name, "artists": [{"name": "Someone"}], "is_local": True}


def episode(n):
    return {"type": "episode", "uri": f"spotify:episode:e{n}", "name": f"Episode {n}", "show": {"name": "A Podcast"}}


def entry(item):
    return {"item": item, "track": item, "is_local": bool(item and item.get("is_local"))}


class World:
    """Shared server-side state for every account."""

    def __init__(self):
        self.users = {}
        self.playlists = {}
        self.ids = itertools.count(1)
        self.writes = []  # (user_id, description)

    def add_user(self, uid, name):
        self.users[uid] = {"name": name, "playlists": [], "liked": [], "albums": [], "artists": []}

    def add_playlist(self, owner, name, entries, description="", public=False, collaborative=False, followers=()):
        pid = f"pl{next(self.ids)}"
        self.playlists[pid] = {
            "owner": owner,
            "name": name,
            "description": description,
            "public": public,
            "collaborative": collaborative,
            "entries": list(entries),
        }
        for uid in (owner, *followers):
            self.users[uid]["playlists"].append(pid)
        return pid

    def playlist_uris(self, pid):
        return [(e["item"] or {}).get("uri") for e in self.playlists[pid]["entries"]]

    def owned(self, uid):
        return [pid for pid in self.users[uid]["playlists"] if self.playlists[pid]["owner"] == uid]


def http_error(status, headers=None):
    return SpotifyException(status, -1, f"https://api.spotify.com/v1/fake:\n injected {status}", headers=headers or {})


class FakeSpotify:
    """Mimics the subset of spotipy.Spotify that the script uses, including API limits."""

    def __init__(self, world, uid):
        self.world = world
        self.uid = uid
        self.calls = Counter()
        self.faults = {}  # (method, nth call) -> ("before" | "after", status | "crash")
        self.reject_uris = set()  # URIs that PUT me/library rejects with 400

    # -- fault injection ------------------------------------------------------

    def _enter(self, method):
        self.calls[method] += 1
        self._fault(method, "before")

    def _fault(self, method, phase):
        spec = self.faults.get((method, self.calls[method]))
        if not spec or spec[0] != phase:
            return
        if spec[1] == "crash":
            raise Crash()
        headers = {"Retry-After": "2"} if spec[1] == 429 else {}
        raise http_error(spec[1], headers)

    # -- paging -----------------------------------------------------------------

    def _page(self, method, items, limit, offset, **args):
        if limit > 50:
            raise http_error(400)
        chunk = items[offset : offset + limit]
        nxt = None
        if offset + limit < len(items):
            nxt = json.dumps({"method": method, "limit": limit, "offset": offset + limit, "args": args})
        return {"items": chunk, "next": nxt, "total": len(items), "limit": limit, "offset": offset}

    def next(self, page):
        self._enter("next")
        spec = json.loads(page["next"])
        return getattr(self, spec["method"])(limit=spec["limit"], offset=spec["offset"], **spec["args"])

    # -- reads -----------------------------------------------------------------

    def me(self):
        self._enter("me")
        return {"id": self.uid, "display_name": self.world.users[self.uid]["name"]}

    def current_user_playlists(self, limit=50, offset=0):
        self._enter("current_user_playlists")
        items = []
        for pid in self.world.users[self.uid]["playlists"]:
            pl = self.world.playlists[pid]
            items.append(
                {
                    "id": pid,
                    "name": pl["name"],
                    "description": pl["description"],
                    "public": pl["public"],
                    "collaborative": pl["collaborative"],
                    "owner": {"id": pl["owner"], "display_name": self.world.users[pl["owner"]]["name"]},
                    "external_urls": {"spotify": f"https://open.spotify.com/playlist/{pid}"},
                    "items": {"total": len(pl["entries"])},
                }
            )
        return self._page("current_user_playlists", items, limit, offset)

    def playlist_items(self, playlist_id, fields=None, limit=50, offset=0, market=None, additional_types=("track", "episode")):
        self._enter("playlist_items")
        pl = self.world.playlists[playlist_id]
        if pl["owner"] != self.uid:
            raise http_error(403)
        return self._page("playlist_items", pl["entries"], limit, offset, playlist_id=playlist_id)

    def current_user_saved_tracks(self, limit=20, offset=0, market=None):
        self._enter("current_user_saved_tracks")
        items = [{"added_at": "x", "track": t} for t in self.world.users[self.uid]["liked"]]
        return self._page("current_user_saved_tracks", items, limit, offset)

    def current_user_saved_albums(self, limit=20, offset=0, market=None):
        self._enter("current_user_saved_albums")
        items = [{"added_at": "x", "album": {"uri": u, "name": u}} for u in self.world.users[self.uid]["albums"]]
        return self._page("current_user_saved_albums", items, limit, offset)

    def current_user_followed_artists(self, limit=20, after=None, offset=0):
        self._enter("current_user_followed_artists")
        items = [{"uri": u, "name": u} for u in self.world.users[self.uid]["artists"]]
        return {"artists": self._page("current_user_followed_artists", items, limit, offset)}

    # -- writes ----------------------------------------------------------------

    def current_user_playlist_create(self, name, public=True, collaborative=False, description=""):
        self._enter("current_user_playlist_create")
        pid = f"pl{next(self.world.ids)}"
        self.world.playlists[pid] = {
            "owner": self.uid,
            "name": name,
            "description": description,
            "public": public,
            "collaborative": collaborative,
            "entries": [],
        }
        self.world.users[self.uid]["playlists"].insert(0, pid)  # new playlists appear on top
        self.world.writes.append((self.uid, f"create {name}"))
        self._fault("current_user_playlist_create", "after")
        return {"id": pid, "name": name}

    def _post(self, url, args=None, payload=None, **kwargs):
        self._enter("_post")
        _, pid, what = url.split("/")
        assert what == "items", url
        uris = payload["uris"]
        pl = self.world.playlists[pid]
        if pl["owner"] != self.uid or len(uris) > 100:
            raise http_error(403 if pl["owner"] != self.uid else 400)
        pl["entries"].extend(entry(track(u.rsplit(":t", 1)[1])) for u in uris)
        self.world.writes.append((self.uid, f"add {len(uris)} to {pid}"))
        self._fault("_post", "after")
        return {"snapshot_id": "s"}

    def _put(self, url, args=None, payload=None, uris=""):
        self._enter("_put")
        assert url == "me/library", url
        batch = uris.split(",")
        if len(batch) > 40 or any(u in self.reject_uris for u in batch):
            raise http_error(400)
        user = self.world.users[self.uid]
        for u in batch:
            kind = u.split(":")[1]
            if kind == "track":
                if not any(t["uri"] == u for t in user["liked"]):
                    user["liked"].insert(0, track(u.rsplit(":t", 1)[1]))  # newest first
            elif kind == "album":
                if u not in user["albums"]:
                    user["albums"].insert(0, u)
            elif kind == "artist":
                if u not in user["artists"]:
                    user["artists"].append(u)
        self.world.writes.append((self.uid, f"save {len(batch)}"))
        self._fault("_put", "after")


def make_world():
    world = World()
    world.add_user("old", "Old Me")
    world.add_user("new", "New Me")
    world.add_user("friend", "A Friend")

    big = [entry(track(i)) for i in range(1, 131)]
    big[4] = entry(local_track("Demo Tape"))  # position 5
    big[9] = entry(None)  # position 10: removed from the catalogue
    big[19] = entry(episode(1))  # position 20
    big[49] = entry(track(1))  # deliberate duplicate must be preserved
    world.big = world.add_playlist("old", "Big Mix", big, description="Rock &amp; Roll", public=True)
    world.empty = world.add_playlist("old", "Empty One", [])
    world.collab = world.add_playlist("old", "Shared", [entry(track(500)), entry(track(501))], collaborative=True)
    world.followed = world.add_playlist("friend", "Friend's Picks", [entry(track(900))], public=True, followers=("old",))
    world.existing = world.add_playlist("new", "Already There", [entry(track(999))])

    liked = [track(1000 + i) for i in range(95)]  # newest first
    liked.insert(30, None)
    world.users["old"]["liked"] = liked
    world.users["old"]["albums"] = [f"spotify:album:a{i}" for i in range(45)]
    world.users["old"]["artists"] = [f"spotify:artist:r{i}" for i in range(43)]
    return world


def expected_uris(world, pid):
    return [u for u in world.playlist_uris(pid) if u and u.startswith("spotify:track:")]


class Harness:
    def __init__(self, world, tmpdir):
        self.world = world
        self.state_path = os.path.join(tmpdir, "state.json")
        self.src_fake = FakeSpotify(world, "old")
        self.dst_fake = FakeSpotify(world, "new")
        self.sleeps = []
        self.prompts = []

    def run(self, *, dry_run=False, verify=False, yes=True, only=sm.STAGES, pattern=None, confirm=True):
        args = argparse.Namespace(
            dry_run=dry_run, verify=verify, yes=yes, only=only, playlist_filter=pattern, state=self.state_path, report=None
        )
        console = sm.Console(io.StringIO())
        report = sm.Report("verify" if verify else "dry-run" if dry_run else "run", only, pattern)
        src = sm.Api(self.src_fake, "source", sm.SOURCE_CACHE, console, read_only=True, sleep=self.sleeps.append)
        dst = sm.Api(self.dst_fake, "destination", sm.DEST_CACHE, console, read_only=dry_run or verify, sleep=self.sleeps.append)
        src_me, dst_me = sm.identity(src.me()), sm.identity(dst.me())
        src.user_id, dst.user_id = src_me["id"], dst_me["id"]
        report.accounts = {"source": src_me, "destination": dst_me}

        def answer(question):
            self.prompts.append(question)
            return confirm

        code = sm.execute(args, src, dst, src_me, dst_me, console, report, confirm=answer)
        self.console_output = console.stream.getvalue()
        return code, report

    def state(self):
        with open(self.state_path) as fh:
            return json.load(fh)

    def dest_copy(self, source_pid):
        return self.state()["playlists"][source_pid]["dest_id"]


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.world = make_world()
        self.h = Harness(self.world, self.tmp.name)

    def assert_source_untouched(self):
        self.assertFalse([w for w in self.world.writes if w[0] == "old"], "source account was written to")

    def test_full_migration_verify_and_idempotent_rerun(self):
        code, report = self.h.run()
        self.assertEqual(code, 0, report.errors)
        self.assert_source_untouched()
        w = self.world

        # Playlists: same name/description/visibility and exact order, skips removed.
        big_copy = w.playlists[self.h.dest_copy(w.big)]
        self.assertEqual(big_copy["name"], "Big Mix")
        self.assertEqual(big_copy["description"], "Rock & Roll")
        self.assertTrue(big_copy["public"])
        self.assertEqual(w.playlist_uris(self.h.dest_copy(w.big)), expected_uris(w, w.big))
        self.assertEqual(len(expected_uris(w, w.big)), 127)
        self.assertEqual(w.playlist_uris(self.h.dest_copy(w.empty)), [])
        self.assertEqual(w.playlist_uris(self.h.dest_copy(w.collab)), expected_uris(w, w.collab))
        self.assertNotIn(w.followed, self.h.state()["playlists"])
        # Relative library order preserved: created bottom-up, so they end up top-down.
        new_order = [w.playlists[p]["name"] for p in w.users["new"]["playlists"]]
        self.assertEqual(new_order, ["Big Mix", "Empty One", "Shared", "Already There"])

        # Liked Songs: identical newest-first order on both sides.
        src_liked = [t["uri"] for t in w.users["old"]["liked"] if t]
        self.assertEqual([t["uri"] for t in w.users["new"]["liked"]], src_liked)
        self.assertEqual(w.users["new"]["albums"], w.users["old"]["albums"])
        self.assertEqual(w.users["new"]["artists"], w.users["old"]["artists"])

        # Every library write stayed within the API limits.
        self.assertTrue(all(int(d.split()[1]) <= 40 for _, d in w.writes if d.startswith("save")))

        text = report.render()
        self.assertIn("Demo Tape", text)
        self.assertIn("#10: (name unavailable)", text)
        self.assertIn("Episode 1", text)
        self.assertIn(f"https://open.spotify.com/playlist/{w.followed}", text)
        self.assertIn("cover images are not transferred", text)
        self.assertIn("Shared", text)  # collaborative note
        self.assertIn("| Liked Songs saved | 95 | 95 of 95 |", text)

        code, report = self.h.run(verify=True)
        self.assertEqual(code, 0, report.render())
        self.assertIn("Zero divergence", report.render())
        self.assertIn("date-added order matches", report.render())

        writes_before = len(w.writes)
        code, report = self.h.run()
        self.assertEqual(code, 0)
        self.assertEqual(len(w.writes), writes_before, "re-run wrote again")
        self.assertIn("Nothing to copy", self.h.console_output)

    def test_resume_after_crash_between_write_and_state_save(self):
        # Playlists are created bottom-up, so "Shared" takes add #1 and "Big Mix"
        # adds #2 and #3. Add #3 lands on Spotify, then the process dies before
        # the state file records it.
        self.h.dst_fake.faults[("_post", 3)] = ("after", "crash")
        with self.assertRaises(Crash):
            self.h.run()
        self.assertEqual(self.h.state()["playlists"][self.world.big]["tracks_added"], 100)
        self.h.dst_fake.faults.clear()

        code, report = self.h.run()
        self.assertEqual(code, 0, report.errors)
        dest = self.h.dest_copy(self.world.big)
        self.assertEqual(self.world.playlist_uris(dest), expected_uris(self.world, self.world.big))
        self.assertEqual(sum(1 for pl in self.world.playlists.values() if pl["name"] == "Big Mix"), 2)  # source + one copy
        self.assertEqual(self.h.run(verify=True)[0], 0)

    def test_resume_liked_songs_mid_stage(self):
        self.h.dst_fake.faults[("_put", 2)] = ("after", "crash")
        with self.assertRaises(Crash):
            self.h.run(only=("liked",))
        self.assertEqual(self.h.state()["liked_tracks_added"], 40)
        self.h.dst_fake.faults.clear()
        self.assertEqual(self.h.run(only=("liked",))[0], 0)
        src_liked = [t["uri"] for t in self.world.users["old"]["liked"] if t]
        self.assertEqual([t["uri"] for t in self.world.users["new"]["liked"]], src_liked)

    def test_server_error_after_add_applied_is_not_retried(self):
        self.h.dst_fake.faults[("_post", 2)] = ("after", 502)  # first Big Mix chunk
        code, report = self.h.run(only=("playlists",))
        self.assertEqual(code, 0, report.errors)
        dest = self.h.dest_copy(self.world.big)
        self.assertEqual(self.world.playlist_uris(dest), expected_uris(self.world, self.world.big))

    def test_server_error_before_add_applied_is_retried(self):
        self.h.dst_fake.faults[("_post", 2)] = ("before", 503)
        code, report = self.h.run(only=("playlists",))
        self.assertEqual(code, 0, report.errors)
        dest = self.h.dest_copy(self.world.big)
        self.assertEqual(self.world.playlist_uris(dest), expected_uris(self.world, self.world.big))

    def test_server_error_after_create_adopts_playlist(self):
        self.h.dst_fake.faults[("current_user_playlist_create", 1)] = ("after", 500)
        code, report = self.h.run(only=("playlists",))
        self.assertEqual(code, 0, report.errors)
        names = Counter(self.world.playlists[p]["name"] for p in self.world.users["new"]["playlists"])
        self.assertEqual(names, Counter({"Big Mix": 1, "Empty One": 1, "Shared": 1, "Already There": 1}))

    def test_rate_limit_honours_retry_after(self):
        self.h.src_fake.faults[("current_user_saved_tracks", 1)] = ("before", 429)
        self.h.dst_fake.faults[("_put", 1)] = ("before", 429)
        code, report = self.h.run(only=("liked",))
        self.assertEqual(code, 0, report.errors)
        self.assertEqual(self.h.sleeps, [2, 2])
        self.assertEqual(len(self.world.users["new"]["liked"]), 95)

    def test_long_retry_after_stops_run(self):
        def huge_429(*a, **k):
            raise http_error(429, {"Retry-After": "7200"})

        self.h.src_fake.current_user_saved_tracks = huge_429
        with self.assertRaises(sm.MigrationError):
            self.h.run(only=("liked",))

    def test_repeated_5xx_fails_one_playlist_but_not_the_run(self):
        for n in range(1, 6):
            self.h.src_fake.faults[("playlist_items", n)] = ("before", 502)
        code, report = self.h.run(only=("playlists",))
        self.assertEqual(code, 1)
        self.assertEqual(self.h.sleeps, [1, 2, 4, 8])
        self.assertEqual(len(report.errors), 1)
        self.assertIn("gave up after 5 attempts", report.errors[0][1])
        # The other playlists still copied.
        self.assertEqual(len(self.h.state()["playlists"]), 2)

    def test_unreadable_playlist_list_does_not_stop_other_stages(self):
        self.h.src_fake.faults[("current_user_playlists", 1)] = ("before", 403)
        code, report = self.h.run(only=("playlists", "artists"))
        self.assertEqual(code, 1)
        self.assertEqual([item for item, _ in report.errors], ["Playlists (reading source)"])
        self.assertEqual(self.world.users["new"]["artists"], self.world.users["old"]["artists"])

    def test_403_gets_specific_message(self):
        self.h.dst_fake.faults[("_put", 1)] = ("before", 403)
        code, report = self.h.run(only=("albums",))
        self.assertEqual(code, 1)
        self.assertIn("allowlist", report.errors[0][1])
        self.assertIn("scope", report.errors[0][1])

    def test_one_bad_library_item_does_not_block_the_rest(self):
        self.h.dst_fake.reject_uris = {"spotify:album:a7"}
        code, report = self.h.run(only=("albums",))
        self.assertEqual(code, 1)
        self.assertEqual(len(self.world.users["new"]["albums"]), 44)
        self.assertEqual([item for item, _ in report.errors], ["Albums: spotify:album:a7"])
        self.assertTrue(self.h.state()["albums_done"])

    def test_dry_run_writes_nothing(self):
        code, report = self.h.run(dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.world.writes, [])
        self.assertFalse(os.path.exists(self.h.state_path))
        self.assertEqual(self.h.prompts, [])
        text = report.render()
        self.assertIn("3 to create", text)
        self.assertIn("95 of 95 to save", text)

    def test_confirmation_prompts(self):
        code, _ = self.h.run(yes=False)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.h.prompts), 2)  # account check + start copying

    def test_account_check_is_asked_even_with_yes_and_declining_writes_nothing(self):
        with self.assertRaises(sm.MigrationError):
            self.h.run(yes=True, confirm=False)
        self.assertEqual(len(self.h.prompts), 1)
        self.assertEqual(self.world.writes, [])

    def test_playlist_filter(self):
        code, _ = self.h.run(only=("playlists",), pattern=sm.parse_regex("^Big"))
        self.assertEqual(code, 0)
        self.assertEqual(list(self.h.state()["playlists"]), [self.world.big])

    def test_state_for_other_accounts_is_refused(self):
        with open(self.h.state_path, "w") as fh:
            json.dump({"source_user_id": "someone", "destination_user_id": "new", "playlists": {}}, fh)
        with self.assertRaises(sm.MigrationError):
            self.h.run()

    def test_same_account_is_refused(self):
        self.h.dst_fake = FakeSpotify(self.world, "old")
        with self.assertRaises(sm.MigrationError):
            self.h.run()

    def test_verify_reports_divergence(self):
        self.h.run()
        dest = self.h.dest_copy(self.world.big)
        entries = self.world.playlists[dest]["entries"]
        entries[3], entries[4] = entries[4], entries[3]
        self.world.users["new"]["artists"].pop()
        code, report = self.h.run(verify=True)
        self.assertEqual(code, 1)
        text = report.render()
        self.assertIn("first difference at position 4", text)
        self.assertIn("1 of 43 missing", text)

    def test_resume_refuses_if_destination_copy_was_edited(self):
        self.h.dst_fake.faults[("_post", 3)] = ("before", "crash")
        with self.assertRaises(Crash):
            self.h.run(only=("playlists",))
        self.h.dst_fake.faults.clear()
        dest = self.h.dest_copy(self.world.big)
        self.world.playlists[dest]["entries"].pop(0)
        code, report = self.h.run(only=("playlists",))
        self.assertEqual(code, 1)
        self.assertIn("left untouched", report.errors[0][1])
        self.assertEqual(len(self.world.playlists[dest]["entries"]), 99)


class GuardTests(unittest.TestCase):
    def test_read_only_client_blocks_writes_before_any_request(self):
        client = sm.ReadOnlySpotify(auth="not-a-real-token")
        with self.assertRaises(sm.MigrationError):
            client.current_user_saved_tracks_add(["spotify:track:abc"])
        with self.assertRaises(sm.MigrationError):
            client.current_user_playlist_create("x")
        with self.assertRaises(sm.MigrationError):
            client.playlist_add_items("abc", ["spotify:track:abc"])

    def test_read_only_api_blocks_writes(self):
        api = sm.Api(object(), "source", sm.SOURCE_CACHE, sm.Console(io.StringIO()), read_only=True)
        with self.assertRaises(sm.MigrationError):
            api.save_to_library(["spotify:track:abc"])

    def test_stage_and_redirect_parsing(self):
        self.assertEqual(sm.parse_stages("artists,playlists"), ("playlists", "artists"))
        with self.assertRaises(argparse.ArgumentTypeError):
            sm.parse_stages("playlists,podcasts")
        os.environ.update(SPOTIPY_CLIENT_ID="id", SPOTIPY_CLIENT_SECRET="secret")
        try:
            os.environ["SPOTIPY_REDIRECT_URI"] = "http://localhost:8888/callback"
            with self.assertRaises(sm.MigrationError):
                sm.read_credentials()
            os.environ["SPOTIPY_REDIRECT_URI"] = ""
            self.assertEqual(sm.read_credentials().redirect_uri, sm.DEFAULT_REDIRECT_URI)
        finally:
            for key in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI"):
                os.environ.pop(key, None)


if __name__ == "__main__":
    unittest.main()
