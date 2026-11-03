"""Regression coverage for commit-gated updates against the actual profile templates."""

from contextlib import redirect_stdout
from copy import deepcopy
from http.client import RemoteDisconnected
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import today


class FakeGitHub:
    """Model paged public metadata and immutable, author-filtered commit histories."""

    def __init__(self):
        self.followers = 4
        self.calls = []
        self.repos = [
            {"id": "repo-a", "nameWithOwner": "adzzse/alpha", "isPrivate": False, "owner": {"login": "adzzse"},
             "stargazerCount": 3, "defaultBranchRef": {"target": {"oid": "head-a"}},
             "commits": [{"oid": "a2", "additions": 10, "deletions": 2},
                         {"oid": "a1", "additions": 5, "deletions": 1}]},
            {"id": "repo-b", "nameWithOwner": "peer/beta", "isPrivate": False, "owner": {"login": "peer"},
             "stargazerCount": 100, "defaultBranchRef": {"target": {"oid": "head-b"}},
             "commits": [{"oid": "b1", "additions": 7, "deletions": 0}]},
        ]

    def __call__(self, document, variables):
        """Honor page sizes/cursors so truncation bugs cannot look like complete totals."""
        self.calls.append((document, deepcopy(variables)))
        offset = int(variables.get("cursor") or 0)
        if document == today.REPOSITORIES:
            nodes = [{key: value for key, value in repo.items() if key != "commits"}
                     for repo in self.repos[offset:offset + 1]]
            end = offset + len(nodes)
            return {"user": {"id": "owner-id", "followers": {"totalCount": self.followers},
                    "repositories": {"nodes": nodes, "pageInfo": {
                        "hasNextPage": end < len(self.repos), "endCursor": str(end)}}}}
        if variables["author"] != "owner-id":
            raise AssertionError("History must be filtered to the profile owner's identity")
        repo = next(repo for repo in self.repos
                    if repo["nameWithOwner"] == variables["owner"] + "/" + variables["name"])
        nodes = repo["commits"][offset:offset + variables["first"]]
        end = offset + len(nodes)
        return {"repository": {"object": {"history": {
            "totalCount": len(repo["commits"]), "nodes": nodes,
            "pageInfo": {"hasNextPage": end < len(repo["commits"]), "endCursor": str(end)},
        }}}}


class UpdaterTests(unittest.TestCase):
    """Exercise disk-visible outcomes rather than the updater's internal call structure."""

    def setUp(self):
        """Each test starts from the real SVGs/README, with its own disposable cache."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = Path(today.__file__).resolve().parent
        shutil.copytree(source / "assets", self.root / "assets")
        shutil.copyfile(source / "README.md", self.root / "README.md")
        self.github = FakeGitHub()

    def update(self, date="2026-11-03", query=None):
        """Suppress routine per-repo progress while retaining assertions and errors."""
        with redirect_stdout(io.StringIO()):
            return today.update_profile(self.root, "adzzse", query or self.github, date)

    def outputs(self):
        """Capture exact bytes so no-op, uptime and failure checks detect accidental writes."""
        paths = ("README.md", "assets/profile-dark.svg", "assets/profile-light.svg", "cache/metrics.json")
        return {name: (self.root / name).read_bytes() if (self.root / name).exists() else None
                for name in paths}

    def cache(self):
        """Read the same persisted baseline that the next scheduled run will receive."""
        return json.loads((self.root / "cache/metrics.json").read_text(encoding="utf-8"))

    def test_bootstrap_populates_both_themes_readme_and_cache(self):
        before = self.outputs()
        self.assertTrue(self.update())
        expected = {"repos": 1, "contributed": 2, "stars": 3, "followers": 4,
                    "commits": 3, "additions": 22, "deletions": 3, "loc": 19}
        self.assertEqual(self.cache()["stats"], expected)
        self.assertNotIn("adzzse/alpha", json.dumps(self.cache()))
        for name in ("assets/profile-dark.svg", "assets/profile-light.svg"):
            after = (self.root / name).read_text(encoding="utf-8")
            original = before[name].decode("utf-8")
            start, end = original.index('<g id="portrait"'), original.index("</g>") + 4
            self.assertIn(original[start:end], after)
            self.assertIn('class="positive">22++', after)
            self.assertIn('class="negative">3--', after)
        readme = (self.root / "README.md").read_bytes()
        self.assertIn(b"Commits: 3.", readme)
        self.assertEqual(b"\r\n" in readme, b"\r\n" in before["README.md"])

    def test_readme_preserves_lf_and_crlf_inputs(self):
        """The same updater must preserve Windows and Ubuntu checkout conventions."""
        path = self.root / "README.md"
        source = path.read_bytes().replace(b"\r\n", b"\n")
        for newline in (b"\n", b"\r\n"):
            with self.subTest(newline=newline):
                cache = self.root / "cache/metrics.json"
                if cache.exists():
                    cache.unlink()
                path.write_bytes(source.replace(b"\n", newline))
                self.assertTrue(self.update())
                result = path.read_bytes()
                self.assertEqual(result.count(b"\r\n"), result.count(b"\n") if newline == b"\r\n" else 0)

    def test_unchanged_commits_ignore_date_followers_and_stars(self):
        self.update()
        before = self.outputs()
        self.github.followers += 10
        self.github.repos[0]["stargazerCount"] += 10
        self.assertFalse(self.update("2026-12-20"))
        self.assertEqual(self.outputs(), before)

    def test_uptime_alone_never_generates_an_update(self):
        self.update()
        path = self.root / "assets/profile-dark.svg"
        svg = path.read_text(encoding="utf-8")
        position = svg.index("Uptime")
        svg = svg[:position] + svg[position:].replace("NULL", "1 day", 1)
        path.write_text(svg, encoding="utf-8")
        before = self.outputs()
        self.assertFalse(self.update("2027-01-01"))
        self.assertEqual(self.outputs(), before)

    def test_new_commit_updates_totals_and_reuses_unchanged_repo_cache(self):
        self.update()
        self.github.calls.clear()
        self.github.repos[0]["commits"].insert(0, {"oid": "a3", "additions": 9, "deletions": 4})
        self.assertTrue(self.update("2026-11-04"))
        self.assertEqual(self.cache()["stats"]["commits"], 4)
        self.assertEqual(self.cache()["stats"]["loc"], 24)
        self.assertEqual(self.cache()["updated_at"], "2026-11-04")
        unchanged_requests = [variables for document, variables in self.github.calls
                              if document == today.HISTORY and variables["name"] == "beta"]
        self.assertEqual(len(unchanged_requests), 1)
        self.assertEqual(unchanged_requests[0]["first"], 1)

    def test_changed_latest_commit_with_equal_count_refreshes_loc(self):
        self.update()
        self.github.repos[0]["commits"][0] = {"oid": "replacement", "additions": 20, "deletions": 2}
        self.assertTrue(self.update("2026-11-04"))
        self.assertEqual(self.cache()["stats"]["commits"], 3)
        self.assertEqual(self.cache()["stats"]["loc"], 29)

    def test_other_authors_branch_updates_do_not_generate_commits(self):
        self.update()
        before = self.outputs()
        self.github.repos[0]["defaultBranchRef"]["target"]["oid"] = "another-authors-head"
        self.assertFalse(self.update("2026-11-04"))
        self.assertEqual(self.outputs(), before)

    def test_repository_rename_alone_does_not_generate_an_update(self):
        self.update()
        before = self.outputs()
        self.github.repos[0]["nameWithOwner"] = "adzzse/renamed"
        self.assertFalse(self.update("2026-11-04"))
        self.assertEqual(self.outputs(), before)

    def test_metadata_and_large_authored_history_are_fully_paginated(self):
        self.github.repos[0]["commits"] = [
            {"oid": f"a-{n}", "additions": 2, "deletions": 1} for n in range(205)]
        self.update()
        self.assertEqual(self.cache()["stats"]["commits"], 206)
        self.assertEqual(self.cache()["stats"]["additions"], 417)
        self.assertEqual(self.cache()["stats"]["deletions"], 205)

    def test_private_repos_are_excluded_and_empty_repos_have_no_history(self):
        private = deepcopy(self.github.repos[0])
        private["isPrivate"] = True
        private["id"] = "repo-private"
        private["nameWithOwner"] = "adzzse/private"
        empty = deepcopy(self.github.repos[0])
        empty["nameWithOwner"] = "adzzse/empty"
        empty["id"] = "repo-empty"
        empty["defaultBranchRef"] = None
        self.github.repos.extend((private, empty))
        self.update()
        self.assertEqual(self.cache()["stats"]["repos"], 2)
        self.assertEqual(self.cache()["stats"]["contributed"], 3)
        queried = [variables["name"] for document, variables in self.github.calls if document == today.HISTORY]
        self.assertNotIn("private", queried)
        self.assertNotIn("empty", queried)

    def test_failed_history_keeps_previous_good_files(self):
        self.update()
        before = self.outputs()

        def failing_query(document, variables):
            """Fail after some repositories succeeded, before a snapshot can be published."""
            if document == today.HISTORY and variables["name"] == "beta":
                raise RuntimeError("GitHub unavailable")
            return self.github(document, variables)

        with self.assertRaisesRegex(RuntimeError, "GitHub unavailable"):
            self.update(query=failing_query)
        self.assertEqual(self.outputs(), before)

    def test_bad_second_svg_is_detected_before_any_file_is_written(self):
        path = self.root / "assets/profile-light.svg"
        path.write_bytes(path.read_bytes().replace(b'id="loc_data"', b'id="missing"'))
        before = self.outputs()
        with self.assertRaisesRegex(ValueError, "loc_data"):
            self.update()
        self.assertEqual(self.outputs(), before)

    def test_missing_readme_markers_preserves_both_svgs(self):
        path = self.root / "README.md"
        path.write_bytes(path.read_bytes().replace(b"<!-- metrics:start -->", b"<!-- missing -->"))
        before = self.outputs()
        with self.assertRaisesRegex(ValueError, "README metrics"):
            self.update()
        self.assertEqual(self.outputs(), before)


class ApiTests(unittest.TestCase):
    """Check transient retries and rejection of incomplete GraphQL responses."""

    @patch.dict("os.environ", {"ACCESS_TOKEN": "test-token"})
    @patch("today.time.sleep")
    @patch("today.urlopen")
    def test_gateway_failure_is_retried(self, request, sleep):
        """The observed 502 must not immediately discard an otherwise recoverable run."""
        request.side_effect = [HTTPError("https://api.github.com/graphql", 502, "gateway", None, None),
                               io.BytesIO(b'{"data":{"ok":true}}')]
        self.assertEqual(today.graphql("query {}", {}), {"ok": True})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch.dict("os.environ", {"ACCESS_TOKEN": "test-token"})
    @patch("today.time.sleep")
    @patch("today.urlopen")
    def test_closed_connection_is_retried(self, request, sleep):
        """The real bootstrap lost a connection during a history page."""
        request.side_effect = [RemoteDisconnected("connection closed"),
                               io.BytesIO(b'{"data":{"ok":true}}')]
        self.assertEqual(today.graphql("query {}", {}), {"ok": True})
        self.assertEqual(request.call_count, 2)

    @patch.dict("os.environ", {"ACCESS_TOKEN": "test-token"})
    @patch("today.time.sleep")
    @patch("today.urlopen")
    def test_connection_retry_is_bounded(self, request, sleep):
        """A bad upstream must fail instead of keeping the scheduled runner alive indefinitely."""
        request.side_effect = RemoteDisconnected("connection closed")
        with self.assertRaises(RemoteDisconnected):
            today.graphql("query {}", {})
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch.dict("os.environ", {"ACCESS_TOKEN": "test-token"})
    @patch("today.time.sleep")
    @patch("today.urlopen")
    def test_auth_failure_is_not_retried(self, request, sleep):
        """Invalid permissions cannot be repaired by hammering the API."""
        request.side_effect = HTTPError("https://api.github.com/graphql", 401, "auth", None, None)
        with self.assertRaises(HTTPError):
            today.graphql("query {}", {})
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    @patch.dict("os.environ", {"ACCESS_TOKEN": "test-token"})
    @patch("today.urlopen")
    def test_partial_graphql_data_is_rejected(self, request):
        """Partial data cannot become a plausible but incorrect published total."""
        request.return_value = io.BytesIO(b'{"errors":[{"message":"incomplete"}],"data":{"partial":true}}')
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            today.graphql("query {}", {})


if __name__ == "__main__":
    unittest.main()
