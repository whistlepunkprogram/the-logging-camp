import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["SECRET_KEY"] = "test-secret"
os.environ["COOKIE_SECURE"] = "0"

import app as camp
from werkzeug.security import generate_password_hash


class CampTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        camp.app.config.update(TESTING=True, DATABASE=os.path.join(self.tmp.name, "test.sqlite3"), WTF_CSRF_ENABLED=False)
        camp._nexus_cache.clear()
        camp._team_data_cache.clear()
        with camp.app.app_context():
            camp.app.test_cli_runner().invoke(args=["init-db"])
            camp.db().execute("INSERT INTO users (username, password_hash, approved, is_admin) VALUES (?, ?, 1, 1)",
                              ("admin", generate_password_hash("password-for-admin")))
            camp.db().commit()
        self.client = camp.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def login(self, username, password):
        return self.client.post("/login", data={"username": username, "password": password})

    def test_approval_and_moderation(self):
        self.client.post("/register", data={"username": "member", "password": "password-for-member"})
        self.assertIn(b"not yet approved", self.login("member", "password-for-member").data)
        self.assertEqual(self.client.post("/topic/1", data={"body": "hello"}).status_code, 404)
        self.login("admin", "password-for-admin")
        self.client.post("/admin/users/2/approve")
        self.client.post("/logout")
        self.login("member", "password-for-member")
        self.assertEqual(self.client.get("/admin").status_code, 403)
        response = self.client.post("/new", data={"title": "Build ideas", "category": "Build", "body": "<script>alert(1)</script>"})
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response.location)
        self.assertIn(b"&lt;script&gt;", page.data)
        self.assertNotIn(b"<script>alert(1)</script>", page.data)
        self.assertEqual(self.client.post("/topic/1", data={"body": "Good idea"}).status_code, 302)
        self.client.post("/logout")
        self.login("admin", "password-for-admin")
        self.client.post("/admin/topics/1/lock")
        self.client.post("/admin/users/2/revoke")
        self.client.post("/logout")
        self.assertIn(b"not yet approved", self.login("member", "password-for-member").data)
        self.login("admin", "password-for-admin")
        self.assertEqual(self.client.post("/topic/1", data={"body": "blocked"}).status_code, 403)
        self.client.post("/admin/topics/1/delete")
        self.assertEqual(self.client.get("/topic/1").status_code, 404)
        with camp.app.app_context():
            self.assertEqual(camp.db().execute("SELECT COUNT(*) FROM replies").fetchone()[0], 0)

    def test_public_board_and_scouting_link(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"https://whistlepunkprogram.github.io/rebuilt-scouting/", response.data)
        self.assertEqual(self.client.get("/new").status_code, 302)
        self.assertEqual(self.client.post("/admin/settings", data={}).status_code, 302)

    def test_discussions_are_paginated_and_searchable(self):
        with camp.app.app_context():
            camp.db().executemany("INSERT INTO topics (title, category, body, author_id) VALUES (?, 'Build', 'body', 1)",
                                  [(f"Topic {n}",) for n in range(21)])
            camp.db().commit()
        self.assertIn(b"Next", self.client.get("/").data)
        self.assertIn(b"Topic 0", self.client.get("/?page=2").data)
        self.assertIn(b"Topic 0", self.client.get("/?q=Topic+0").data)
        self.assertNotIn(b"Topic 1</h3>", self.client.get("/?q=Topic+0").data)

    @patch.dict(os.environ, {"NEXUS_API_KEY": "private-key"})
    @patch("app.requests.get")
    def test_nexus_filters_team_and_keeps_key_server_side(self, get):
        get.return_value.json.return_value = {"dataAsOfTime": 1000, "matches": [
            {"label": "Qual 1", "status": "Now queuing", "redTeams": ["254"], "blueTeams": ["123"]},
            {"label": "Qual 2", "status": "On deck", "redTeams": ["999"], "blueTeams": []},
        ]}
        self.login("admin", "password-for-admin")
        self.client.post("/admin/settings", data={"event": "2026test", "team": "254"})
        page = self.client.get("/").data
        self.assertIn(b"Qual 1", page)
        self.assertNotIn(b"Qual 2", page)
        self.assertNotIn(b"private-key", page)
        self.client.get("/")
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.kwargs["headers"], {"Nexus-Api-Key": "private-key"})

    @patch.dict(os.environ, {"NEXUS_API_KEY": "private-key"})
    @patch("app.requests.get", side_effect=camp.requests.ConnectionError("unavailable"))
    def test_nexus_failure_is_visible(self, get):
        result, error = camp.queue_status("2026test", "254")
        self.assertIsNone(result)
        self.assertIn("unavailable", error)

    @patch.dict(os.environ, {"MATCH13_API_KEY": "match-secret", "TBA_API_KEY": "tba-secret"})
    @patch("app.requests.get")
    def test_team_data_sources_render_and_cache_independently(self, get):
        def respond(url, **kwargs):
            response = unittest.mock.Mock()
            if "match13" in url:
                self.assertEqual(kwargs["headers"], {"Authorization": "Bearer match-secret"})
                response.json.return_value = {"rating": 107.7, "details": {"auto": 16.3}}
            else:
                self.assertEqual(kwargs["headers"], {"X-TBA-Auth-Key": "tba-secret"})
                response.json.return_value = {"2026mn": {"qual": {"ranking": {"rank": 12}}}}
            return response
        get.side_effect = respond
        page = self.client.get("/team-data").data
        self.assertIn(b"107.7", page)
        self.assertIn(b"2026mn", page)
        self.assertIn(b"12", page)
        self.assertNotIn(b"match-secret", page)
        self.assertNotIn(b"tba-secret", page)
        self.client.get("/team-data")
        self.assertEqual(get.call_count, 2)

    @patch.dict(os.environ, {"MATCH13_API_KEY": "", "TBA_API_KEY": ""})
    def test_team_data_missing_keys_are_visible(self):
        page = self.client.get("/team-data").data
        self.assertIn(b"Set MATCH13_API_KEY", page)
        self.assertIn(b"Set TBA_API_KEY", page)
        self.assertIn(b"thebluealliance.com/team/4674/2026", page)

    def test_csrf_required_for_posts(self):
        camp.app.config["WTF_CSRF_ENABLED"] = True
        try:
            self.assertEqual(self.client.post("/register", data={"username": "member", "password": "password-for-member"}).status_code, 400)
        finally:
            camp.app.config["WTF_CSRF_ENABLED"] = False


if __name__ == "__main__":
    unittest.main()
