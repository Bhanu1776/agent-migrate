"""claude-code -> opencode on a fake home. Each test guards a promise the tool makes to the user."""
import contextlib
import io
import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path

from agent_migrate.model import Bundle, Guard, Hook, McpServer
from agent_migrate.readers import claude_code
from agent_migrate.writers import opencode as w
from tests.test_claude_to_pi import SECRET, _w, fake_home

ALL = {"instructions", "skills", "prompts", "mcp", "memory", "hooks", "guards", "sessions"}


def apply(plan):
    for a in plan.actions:
        if a.apply:
            a.apply()


class ClaudeToOpencode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = fake_home(root)
        # A Claude command whose `model: sonnet` would fail opencode's command schema.
        _w(self.home / ".claude" / "commands" / "ship.md", "---\ndescription: Ship it\nmodel: sonnet\nallowed-tools: Bash\n---\nShip $ARGUMENTS")
        self.target = root / "opencode"
        _w(self.target / "AGENTS.md", "My own opencode notes.")
        _w(self.target / "opencode.json", json.dumps({"permission": {"bash": "allow"}, "theme": "mine"}))

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, bundle=None, parts=ALL):
        return w.plan(bundle or claude_code.read(self.home), self.target, self.home, parts)

    def snapshot(self):
        return sorted((str(p.relative_to(self.target)), p.stat().st_mtime_ns) for p in self.target.rglob("*"))

    def test_dry_run_writes_nothing_and_never_prints_secrets(self):
        before = self.snapshot()
        p = self.plan()
        self.assertEqual(self.snapshot(), before, "planning must not touch the target")
        shown = "\n".join([a.desc for a in p.actions] + p.gaps)
        self.assertNotIn(SECRET, shown, "plans are shown on screen; secrets must never be printed")

    def test_full_run(self):
        p = self.plan()
        apply(p)
        cfg_path = self.target / "opencode.json"
        cfg = json.loads(cfg_path.read_text())
        self.assertEqual(stat.S_IMODE(os.stat(cfg_path).st_mode), 0o600, "opencode.json now holds MCP headers: owner-only")
        self.assertEqual(cfg["theme"], "mine", "user's own settings survive the merge")
        self.assertTrue([f for f in self.target.iterdir() if re.fullmatch(r"opencode\.json\.bak-\d{8}-\d{6}", f.name)])
        self.assertEqual(cfg["mcp"]["api"], {"type": "remote", "url": "https://x/mcp", "enabled": True,
                                             "headers": {"Authorization": SECRET}})

        # Guards become native rules; the user's blanket allow stays first so the deny (last match) wins.
        self.assertEqual(list(cfg["permission"]["bash"].items()), [("*", "allow"), ("security *", "deny")])
        # beta was off in Claude: it must stay off. alpha is in ~/.claude/skills, which opencode reads itself.
        self.assertEqual(cfg["permission"]["skill"], {"beta": "deny"})
        self.assertFalse((self.target / "skills" / "alpha").exists(), "don't duplicate skills opencode already sees")

        agents = (self.target / "AGENTS.md").read_text()
        self.assertIn("My own opencode notes.", agents)
        self.assertIn("Always be kind.", agents)
        self.assertTrue(list(self.target.glob("AGENTS.md.bak-*")))

        ship = (self.target / "commands" / "ship.md").read_text()
        self.assertTrue(ship.startswith('---\ndescription: "Ship it"\n---\n'), "only opencode-valid frontmatter is kept")
        self.assertNotIn("sonnet", ship)

        bridge = json.loads((self.target / "agent-migrate" / "bridge.json").read_text())["sources"]["claude-code"]
        self.assertEqual([h["event"] for h in bridge["hooks"]], ["stop", "post_tool"], "opencode has todowrite: keep it")
        plugin = (self.target / "plugins" / "agent-migrate-bridge.ts").read_text()
        self.assertNotRegex(plugin, r"export (const|let|var) (?!AgentMigrateBridge)", "opencode calls every export as a plugin")
        self.assertTrue((self.target / "memory" / "-work-repo" / "fact.md").is_file())

        [chat] = list((self.target / "agent-migrate" / "sessions").glob("*.json"))
        self.assertEqual(stat.S_IMODE(os.stat(chat).st_mode), 0o600, "transcripts can hold pasted secrets")
        data = json.loads(chat.read_text())
        self.assertTrue(data["info"]["id"].startswith("ses_"))
        self.assertEqual([m["info"]["role"] for m in data["messages"]], ["user", "assistant"])
        self.assertEqual(data["messages"][1]["info"]["parentID"], data["messages"][0]["info"]["id"])
        self.assertTrue(any("opencode import" in g for g in p.gaps), "a --target run doesn't import: tell the user how")

    def test_second_run_is_idempotent(self):
        apply(self.plan())
        snap = {f: f.read_bytes() for f in self.target.rglob("*") if f.is_file()}
        p2 = self.plan()
        self.assertEqual([a for a in p2.actions if a.apply and a.part not in ("hooks", "instructions")], [],
                         "nothing new to do on a second run")
        apply(p2)
        after = {f: f.read_bytes() for f in self.target.rglob("*") if f.is_file()}
        self.assertEqual(after, snap, "re-running must not change or duplicate anything")

    def test_session_ids_are_stable(self):
        """`opencode import` upserts by id, so the same chat must always get the same ids."""
        [s] = list(claude_code.read(self.home).sessions())
        self.assertEqual(w._export(s, "claude-code"), w._export(s, "claude-code"))

    def test_unsupported_bits_land_in_gaps(self):
        b = Bundle(source="x", guards=[Guard(r"^a[bc]$", "deny", "odd")],
                   hooks=[Hook("pre_tool", "echo nb", matcher="NotebookEdit", origin="settings.json")],
                   mcp=[McpServer("m", {"command": "run", "args": ["-v"], "env": {"TOKEN": "${TOKEN:-dflt}"}})])
        p = self.plan(b, {"guards", "mcp", "hooks"})
        self.assertTrue(any("odd" in g for g in p.gaps), "a guard we can't express must be reported, not dropped")
        self.assertTrue(any("NotebookEdit" in g for g in p.gaps), "a hook opencode can never fire is reported")
        self.assertTrue(any("TOKEN" in g and "default" in g for g in p.gaps))
        apply(p)
        cfg = json.loads((self.target / "opencode.json").read_text())
        self.assertEqual(cfg["mcp"]["m"], {"type": "local", "command": ["run", "-v"], "enabled": True,
                                           "environment": {"TOKEN": "{env:TOKEN}"}})

    def test_jsonc_config_is_never_clobbered(self):
        text = '{\n  // my comment\n  "theme": "mine"\n}\n'
        (self.target / "opencode.json").write_text(text)
        p = self.plan()
        apply(p)
        self.assertEqual((self.target / "opencode.json").read_text(), text)
        self.assertTrue(any("opencode.json" in g for g in p.gaps))

    def test_guard_regexes_become_wildcards(self):
        self.assertEqual(w._wildcards(r"^git\s+(?:push|fetch)(\s|$)"), ["git push *", "git fetch *"])  # codex
        self.assertEqual(w._wildcards(r"^.*prod.*$"), ["*prod*"])  # claude Bash(*prod*)
        self.assertEqual(w._wildcards(r"^rm\ \-rf(\s.*)?$"), ["rm -rf *"])  # claude Bash(rm -rf:*)
        self.assertIsNone(w._wildcards(r"^a[bc]$"))


if __name__ == "__main__":
    unittest.main()
