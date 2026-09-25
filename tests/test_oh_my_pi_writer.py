"""claude-code -> oh-my-pi on a fake home. Each test guards a promise the tool makes to the user."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from agent_migrate.model import Skill
from agent_migrate.readers import claude_code
from agent_migrate.writers import oh_my_pi as omp
from tests.test_claude_to_pi import SECRET, _w, fake_home

PARTS = {"instructions", "skills", "prompts", "mcp", "memory", "hooks", "guards", "sessions"}


class ClaudeToOmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = fake_home(root)
        # A project-scoped server: omp only reads top-level ~/.claude.json servers, so this one must be written.
        cj = json.loads((self.home / ".claude.json").read_text())
        cj["projects"] = {"/work/repo": {"mcpServers": {"proj": {"command": "x", "env": {"TOKEN": SECRET}}}}}
        _w(self.home / ".claude.json", json.dumps(cj))
        self.outside = root / "elsewhere" / "gamma"  # a skill omp can't discover by itself
        _w(self.outside / "SKILL.md", "---\nname: gamma\n---\n")
        self.target = root / "omp"
        _w(self.target / "config.yml", "theme: \n  dark: x\ndisabledExtensions: \n  - skill:mine\nautoResume: true\n")
        _w(self.target / "AGENTS.md", "My own omp notes.")

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self):
        b = claude_code.read(self.home)
        b.skills.append(Skill("gamma", self.outside, enabled=False))
        return omp.plan(b, self.target, self.home, PARTS)

    def apply(self):
        p = self.plan()
        for a in p.actions:
            if a.apply:
                a.apply()
        return p

    def snapshot(self):
        return {str(x.relative_to(self.target)): x.read_bytes() if x.is_file() else None for x in self.target.rglob("*")}

    def test_dry_run_writes_nothing_and_never_shows_secrets(self):
        before = self.snapshot()
        p = self.plan()
        self.assertEqual(self.snapshot(), before, "planning must not touch the target")
        shown = " ".join([a.desc for a in p.actions] + p.gaps)
        self.assertNotIn(SECRET, shown, "plans are printed; secret values must never appear")

    def test_full_run(self):
        p = self.apply()
        # omp already reads ~/.claude skills, commands and top-level MCP: duplicating them would shadow live config.
        self.assertFalse((self.target / "skills" / "alpha").exists(), "native Claude skills must not be re-linked")
        self.assertTrue((self.target / "skills" / "gamma").is_symlink(), "skills omp can't see get linked")
        mcp = json.loads((self.target / "mcp.json").read_text())["mcpServers"]
        self.assertEqual(set(mcp), {"proj"}, "only servers omp can't read natively go in mcp.json")
        self.assertEqual(stat.S_IMODE(os.stat(self.target / "mcp.json").st_mode), 0o600, "secret-bearing config must be owner-only")

        cfg = (self.target / "config.yml").read_text()
        for sid in ("skill:beta", "skill:gamma", "skill:mine"):
            self.assertIn(sid, cfg, "skills off in Claude must stay off in omp; the user's own entries survive")
        self.assertIn("theme: \n  dark: x", cfg, "unrelated user settings are untouched")
        self.assertTrue(list(self.target.glob("config.yml.bak-*")), "config.yml is backed up before first change")

        agents = (self.target / "AGENTS.md").read_text()
        self.assertIn("My own omp notes.", agents)
        self.assertIn("Always be kind.", agents, "omp's AGENTS.md shadows CLAUDE.md, so the text must be carried")

        ext = (self.target / "extensions" / "agent-migrate-bridge.ts").read_text()
        self.assertIn('"@oh-my-pi/pi-coding-agent"', ext)
        self.assertNotIn("systemPromptOptions", ext, "pi-only API would crash in omp's before_agent_start")
        self.assertNotIn("agent_settled", ext, "omp has no agent_settled event; Stop hooks would never run")
        bridge = json.loads((self.target / "agent-migrate" / "bridge.json").read_text())["sources"]["claude-code"]
        self.assertEqual([h["event"] for h in bridge["hooks"]], ["stop"])
        self.assertTrue(any("TodoWrite" in g for g in p.gaps), "hooks omp can't fire are reported")
        self.assertTrue((self.target / "memory" / "-work-repo" / "MEMORY.md").is_file())

        self.assertFalse((self.target / "sessions").exists(), "omp imports Claude chats itself; don't write a lossy copy")
        self.assertTrue(any("--from-claude" in a.desc for a in p.actions if a.part == "sessions"))

    def test_second_run_is_idempotent(self):
        self.apply()
        before = self.snapshot()
        p = self.apply()
        self.assertEqual(self.snapshot(), before, "a second run must change nothing")
        self.assertFalse([a for a in p.actions if a.part in ("skills", "prompts", "mcp", "settings") and a.kind != "skip"])

    def test_disabled_list_edits(self):
        self.assertEqual(omp.add_disabled("a: 1\n", ["skill:x"]), 'a: 1\ndisabledExtensions:\n  - "skill:x"\n')
        self.assertEqual(omp.add_disabled("disabledExtensions: []\nb: 2\n", ["skill:x"]), 'disabledExtensions:\n  - "skill:x"\nb: 2\n')
        self.assertIsNone(omp.add_disabled("disabledExtensions: [skill:y]\n", ["skill:x"]), "flow lists are left to the user")
        self.assertEqual(omp.add_disabled("disabledExtensions:\n- skill:x\n", ["skill:x"]), "disabledExtensions:\n- skill:x\n")


if __name__ == "__main__":
    unittest.main()
