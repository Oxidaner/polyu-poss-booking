import unittest
from types import SimpleNamespace
from unittest.mock import mock_open, patch

import poss_ui


class WindowsRunnerStatusTests(unittest.TestCase):
    def test_windows_status_checks_tasklist_without_signaling_runner(self):
        tasklist = SimpleNamespace(
            returncode=0,
            stdout="python.exe 1234 Console 1 12,000 K\n",
        )
        with (
            patch.object(poss_ui.os, "name", "nt"),
            patch("builtins.open", mock_open(read_data="1234\n")),
            patch("poss_ui.subprocess.run", return_value=tasklist),
            patch.object(poss_ui.os, "kill") as kill,
        ):
            status = poss_ui.runner_status()

        self.assertTrue(status["running"])
        self.assertEqual(status["pid"], 1234)
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
