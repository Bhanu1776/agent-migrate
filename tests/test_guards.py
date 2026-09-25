"""Guards must be at least as strict as the harness they came from (review finding #2)."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_migrate.guards import SEGMENTS_TS, command_segments, matches
from agent_migrate.readers.claude_code import _bash_rule_to_regex

GIT_PUSH = _bash_rule_to_regex("Bash(git push:*)")
PROD = _bash_rule_to_regex("Bash(*prod*)")

# Ways people (and models) actually run a command; a deny on `git push` must catch all.
BYPASSES = [
    "git push", " git push", "cd x && git push", "ls; git push origin", "ls\ngit push origin",
    "env A=1 git push", "A=1 B=2 git push", "sudo git push", "command git push", "time git push -f",
    "sh -c 'git push'", 'bash -c "cd x && git push"', "echo $(git push)", "echo `git push`",
    "true || git push", "(git push)", "nohup git push &",
    # second review: wrappers whose options take a value, login shells, eval/xargs
    "sudo -u root git push", "nice -n 10 git push", "env -u HOME git push", "exec -a x git push",
    "bash -lc 'git push'", "bash --login -c 'git push'", 'eval "git push"', "echo . | xargs git push",
    "echo . | xargs -I{} git push",
]
ALLOWED = ["git status", "echo git push is blocked", "git pushx", "ls", "sudo -u root ls", "bash -lc 'ls'", "time -p make"]


class Guards(unittest.TestCase):
    def test_compound_and_wrapped_commands_are_caught(self):
        for cmd in BYPASSES:
            self.assertTrue(matches(GIT_PUSH, cmd), cmd)

    def test_unrelated_commands_pass(self):
        for cmd in ALLOWED:
            self.assertFalse(matches(GIT_PUSH, cmd), cmd)

    def test_wildcard_rule_sees_later_lines(self):
        self.assertTrue(matches(PROD, "ls\ndeploy prod"))

    def test_bare_bash_rule_means_all_shell(self):
        self.assertTrue(matches(_bash_rule_to_regex("Bash"), "ls"))

    def test_pi_bridge_carries_the_current_segmenter(self):
        # The asset is a static .ts file with a pasted copy; a stale copy silently weakens pi guards.
        asset = Path(__file__).resolve().parent.parent / "agent_migrate" / "assets" / "pi-bridge.ts"
        self.assertIn(SEGMENTS_TS.strip(), asset.read_text())

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_typescript_copy_is_identical(self):
        cases = BYPASSES + ALLOWED + ["ls\ndeploy prod", "sh -c 'sh -c \"sh -c x\"'"]
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "seg.ts"
            f.write_text(SEGMENTS_TS + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map((c) => commandSegments(c))));\n")
            out = subprocess.run(["node", str(f)], capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), [command_segments(c) for c in cases])


if __name__ == "__main__":
    unittest.main()
