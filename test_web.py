import os
import tempfile
import time
import unittest

import web


class WebSecurityTests(unittest.TestCase):
    def setUp(self):
        self.old_storage = web._qbt.STORAGE_DIR
        self.old_current_orphans = web._current_orphan_realpaths
        self.old_client = web._qbt.QBittorrentClient
        self.old_last_activity_days = web._qbt.LAST_ACTIVITY_DAYS
        self.old_scan_orphans = web._qbt.scan_orphans
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = os.path.join(self.tmp.name, "storage")
        self.outside = os.path.join(self.tmp.name, "outside")
        os.mkdir(self.storage)
        os.mkdir(self.outside)
        web._qbt.STORAGE_DIR = self.storage

    def tearDown(self):
        web._qbt.STORAGE_DIR = self.old_storage
        web._current_orphan_realpaths = self.old_current_orphans
        web._qbt.QBittorrentClient = self.old_client
        web._qbt.LAST_ACTIVITY_DAYS = self.old_last_activity_days
        web._qbt.scan_orphans = self.old_scan_orphans
        self.tmp.cleanup()

    def write_file(self, rel_path, content="x"):
        path = os.path.join(self.storage, *rel_path.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def scan_with_torrents(self, torrents):
        class FakeClient:
            def get_torrents(self):
                return torrents

            def get_trackers(self, torrent_hash):
                return [{"status": 2}]

        return web._qbt.scan_orphans(FakeClient())

    def test_compute_sizes_ignores_paths_outside_storage(self):
        secret = os.path.join(self.outside, "secret.txt")
        with open(secret, "wb") as f:
            f.write(b"secret")

        self.assertEqual(web.compute_sizes([secret]), {})

    def test_browse_does_not_follow_symlink_outside_storage(self):
        os.symlink(self.outside, os.path.join(self.storage, "link"))

        self.assertEqual(web.browse_dir(os.path.join(self.storage, "link")), [])

    def test_delete_rejects_traversal_through_symlink(self):
        victim = os.path.join(self.outside, "victim.txt")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("keep me")
        os.symlink(self.outside, os.path.join(self.storage, "link"))
        web._current_orphan_realpaths = lambda: {}

        result = web.do_delete([os.path.join(self.storage, "link", "victim.txt")])

        self.assertTrue(os.path.exists(victim))
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["errors"][0]["error"], "Chemin non autorisé")

    def test_delete_revalidates_current_orphans(self):
        managed = os.path.join(self.storage, "managed.txt")
        with open(managed, "w", encoding="utf-8") as f:
            f.write("still managed")
        web._current_orphan_realpaths = lambda: {}

        result = web.do_delete([managed])

        self.assertTrue(os.path.exists(managed))
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["errors"][0]["error"], "Chemin non orphelin")

    def test_delete_allows_current_orphan_inside_storage(self):
        orphan = os.path.join(self.storage, "orphan.txt")
        with open(orphan, "w", encoding="utf-8") as f:
            f.write("delete me")
        web._current_orphan_realpaths = lambda: {web._safe_storage_path(orphan): {}}

        result = web.do_delete([orphan])

        self.assertFalse(os.path.exists(orphan))
        self.assertEqual(result["deleted"], [orphan])
        self.assertEqual(result["errors"], [])

    def test_delete_torrent_requires_inactive_hash(self):
        calls = []

        class FakeClient:
            def login(self):
                pass

            def get_torrents(self):
                return [{"hash": "abc", "last_activity": int(time.time()), "size": 1}]

            def delete_torrent(self, torrent_hash, delete_files=True):
                calls.append((torrent_hash, delete_files))

        web._qbt.QBittorrentClient = FakeClient
        web._qbt.LAST_ACTIVITY_DAYS = 30

        result = web.delete_torrent_with_files("abc")

        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])

    def test_delete_torrent_allows_inactive_hash(self):
        calls = []

        class FakeClient:
            def login(self):
                pass

            def get_torrents(self):
                return [{"hash": "abc", "last_activity": 0, "size": 1}]

            def delete_torrent(self, torrent_hash, delete_files=True):
                calls.append((torrent_hash, delete_files))

        web._qbt.QBittorrentClient = FakeClient
        web._qbt.LAST_ACTIVITY_DAYS = 30

        result = web.delete_torrent_with_files("abc")

        self.assertTrue(result["ok"])
        self.assertEqual(calls, [("abc", True)])

    def test_config_validation_rejects_bad_port_before_save(self):
        with self.assertRaises(web.WebError):
            web._validate_config({"QB_PORT": "not-a-port"})

    def test_config_validation_rejects_env_injection(self):
        with self.assertRaises(web.WebError):
            web._validate_config({"QB_USER": "admin\nQB_PASS=pwned"})

    def test_scoped_detection_handles_duplicate_name_in_two_categories(self):
        self.write_file("radarr/movie.mkv")
        self.write_file("sonarr/movie.mkv")
        torrents = [
            {
                "hash": "radarr-movie",
                "name": "movie.mkv",
                "content_path": "/remote/radarr/movie.mkv",
                "save_path": "/remote/radarr",
            },
            {
                "hash": "sonarr-other",
                "name": "other.mkv",
                "content_path": "/remote/sonarr/other.mkv",
                "save_path": "/remote/sonarr",
            },
        ]

        scan = self.scan_with_torrents(torrents)

        self.assertEqual([entry.rel_path for entry in scan["orphans"]], ["sonarr/movie.mkv"])

    def test_root_torrent_does_not_protect_same_name_in_category(self):
        self.write_file("movie.mkv")
        self.write_file("radarr/movie.mkv")
        self.write_file("radarr/known.mkv")
        torrents = [
            {
                "hash": "root-movie",
                "name": "movie.mkv",
                "content_path": "/remote/movie.mkv",
                "save_path": "/remote",
            },
            {
                "hash": "radarr-known",
                "name": "known.mkv",
                "content_path": "/remote/radarr/known.mkv",
                "save_path": "/remote/radarr",
            },
        ]

        scan = self.scan_with_torrents(torrents)

        self.assertEqual([entry.rel_path for entry in scan["orphans"]], ["radarr/movie.mkv"])

    def test_known_name_in_radarr_does_not_protect_sonarr(self):
        self.write_file("radarr/episode.mkv")
        self.write_file("sonarr/episode.mkv")
        self.write_file("sonarr/known.mkv")
        torrents = [
            {
                "hash": "radarr-episode",
                "name": "episode.mkv",
                "content_path": "/remote/radarr/episode.mkv",
                "save_path": "/remote/radarr",
            },
            {
                "hash": "sonarr-known",
                "name": "known.mkv",
                "content_path": "/remote/sonarr/known.mkv",
                "save_path": "/remote/sonarr",
            },
        ]

        scan = self.scan_with_torrents(torrents)

        self.assertEqual([entry.rel_path for entry in scan["orphans"]], ["sonarr/episode.mkv"])

    def test_web_scan_uses_shared_scan_orphans_function(self):
        calls = []

        class FakeClient:
            def login(self):
                pass

        def fake_scan_orphans(client):
            calls.append(type(client).__name__)
            return {
                "known_by_scope": {"": set()},
                "category_dirs": {},
                "torrents": [],
                "tracker_warnings": [],
                "inactive_torrents": [],
                "entries": [],
                "orphans": [],
            }

        web._qbt.QBittorrentClient = FakeClient
        web._qbt.scan_orphans = fake_scan_orphans

        result = web.run_scan()

        self.assertEqual(calls, ["FakeClient"])
        self.assertEqual(result["orphans"], [])


if __name__ == "__main__":
    unittest.main()
