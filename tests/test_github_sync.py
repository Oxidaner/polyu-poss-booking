import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import poss_ui


def git(directory, *args):
    result = subprocess.run(
        ["git", *args], cwd=directory, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


class GitHubSyncTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.remote = root / "remote.git"
        self.local = root / "local"
        self.bot = root / "bot"
        git(root, "init", "--bare", "--initial-branch=main", str(self.remote))
        git(root, "clone", str(self.remote), str(self.local))
        git(self.local, "config", "user.name", "Test User")
        git(self.local, "config", "user.email", "test@example.com")
        (self.local / "config.json").write_text('{"date":"2026-10-21"}\n')
        (self.local / "poss_ui.py").write_text("original\n")
        git(self.local, "add", "config.json", "poss_ui.py")
        git(self.local, "commit", "-m", "initial")
        git(self.local, "push", "origin", "main")

        git(root, "clone", str(self.remote), str(self.bot))
        git(self.bot, "config", "user.name", "Bot")
        git(self.bot, "config", "user.email", "bot@example.com")
        (self.bot / "state.json").write_text('{"processed":{"old":"booked"}}\n')
        git(self.bot, "add", "state.json")
        git(self.bot, "commit", "-m", "save booking state")
        git(self.bot, "push", "origin", "main")

        (self.local / "poss_ui.py").write_text("local UI changes\n")
        (self.local / "state.json").write_text('{"local":"keep"}\n')
        self.root_patch = patch.object(poss_ui, "ROOT", str(self.local))
        self.config_patch = patch.object(
            poss_ui, "CONFIG_FILE", str(self.local / "config.json")
        )
        self.root_patch.start()
        self.config_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.config_patch.stop)

    def assert_remote_config_and_local_files(self):
        remote_config = git(self.local, "--git-dir", str(self.remote),
                            "show", "main:config.json")
        self.assertEqual({"date": "2026-10-22"}, json.loads(remote_config))
        remote_state = git(self.local, "--git-dir", str(self.remote),
                           "show", "main:state.json")
        self.assertEqual({"processed": {"old": "booked"}}, json.loads(remote_state))
        self.assertEqual("local UI changes\n", (self.local / "poss_ui.py").read_text())
        self.assertEqual('{"local":"keep"}\n', (self.local / "state.json").read_text())

    def test_upload_preserves_remote_state_when_config_is_uncommitted(self):
        (self.local / "config.json").write_text('{"date":"2026-10-22"}\n')

        result = poss_ui.push_config_to_github()

        self.assertTrue(result["pushed"])
        self.assert_remote_config_and_local_files()
        self.assertFalse(poss_ui.github_status()["configDirty"])

    def test_upload_retries_config_committed_before_remote_advanced(self):
        (self.local / "config.json").write_text('{"date":"2026-10-22"}\n')
        git(self.local, "add", "config.json")
        git(self.local, "commit", "-m", "local config")

        result = poss_ui.push_config_to_github()

        self.assertTrue(result["pushed"])
        self.assert_remote_config_and_local_files()
        self.assertFalse(poss_ui.push_config_to_github()["pushed"])

    def test_upload_retries_when_action_updates_state_during_push(self):
        (self.local / "config.json").write_text('{"date":"2026-10-22"}\n')
        original_git = poss_ui._git
        raced = False

        def git_with_race(args, *other, **options):
            nonlocal raced
            if args[0] == "push" and not raced:
                raced = True
                (self.bot / "state.json").write_text('{"processed":{"new":"booked"}}\n')
                git(self.bot, "add", "state.json")
                git(self.bot, "commit", "-m", "new booking state")
                git(self.bot, "push", "origin", "main")
            return original_git(args, *other, **options)

        with patch.object(poss_ui, "_git", side_effect=git_with_race):
            result = poss_ui.push_config_to_github()

        self.assertTrue(raced)
        self.assertTrue(result["pushed"])
        remote_state = git(self.local, "--git-dir", str(self.remote),
                           "show", "main:state.json")
        self.assertEqual({"processed": {"new": "booked"}}, json.loads(remote_state))
        self.assertEqual({"date": "2026-10-22"}, json.loads(
            git(self.local, "--git-dir", str(self.remote), "show", "main:config.json")
        ))


if __name__ == "__main__":
    unittest.main()
