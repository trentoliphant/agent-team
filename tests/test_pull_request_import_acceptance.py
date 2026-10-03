"""Import-order acceptance of the existing-PR modules, each order in a fresh interpreter. Offline."""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ImportAcceptanceTests(unittest.TestCase):
    def test_existing_pr_modules_import_safely_in_either_order(self):
        for first, second in (("agent_team.pull_requests", "agent_team.coordinator"),
                              ("agent_team.coordinator", "agent_team.pull_requests")):
            with self.subTest(first=first):
                code = (f"import {first}, {second}, agent_team.cli\n"
                        "from agent_team import coordinator, pull_requests\n"
                        "assert pull_requests.core is coordinator\n"
                        "assert coordinator.PullRequests is pull_requests.PullRequests\n"
                        "assert coordinator.pull_number is pull_requests.pull_number\n")
                result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                                        timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
