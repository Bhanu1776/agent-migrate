"""claude-code -> oh-my-pi on a fake home. Each test guards a promise the tool makes to the user."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from agent_migrate.model import Bundle, McpServer, Skill
from agent_migrate.readers import claude_code
from agent_migrate.writers import oh_my_pi as omp
from tests.test_claude_to_pi import SECRET, _w, fake_home

try:
    import yaml
except ImportError:
    yaml = None

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


    # --- regressions from the Sep 2026 review; each failed on the code before the fix ---

    def run_plan(self, b, parts):
        p = omp.plan(b, self.target, self.home, parts)
        for a in p.actions:
            if a.apply:
                a.apply()
        return p

    def test_1_unparseable_mcp_json_is_never_rewritten(self):
        """_load() turned a broken mcp.json into {} and the rewrite dropped the user's own servers."""
        broken = '{"mcpServers": {"mine": {"command": "x"},}}'
        _w(self.target / "mcp.json", broken)
        p = self.run_plan(Bundle(source="x", mcp=[McpServer("s", {"command": "run"})]), {"mcp"})
        self.assertEqual((self.target / "mcp.json").read_text(), broken)
        self.assertIn("mcp.json is not valid JSON", "\n".join(p.gaps))

    @unittest.skipUnless(yaml, "PyYAML not installed: the writer can't detect broken YAML without it")
    def test_1_unparseable_config_yml_is_never_edited(self):
        broken = "theme: [unclosed\ndisabledExtensions:\n  - skill:mine\n"
        _w(self.target / "config.yml", broken)
        p = self.run_plan(Bundle(source="x", skills=[Skill("gamma", self.outside, enabled=False)]), {"skills"})
        self.assertEqual((self.target / "config.yml").read_text(), broken)
        self.assertIn("skill:gamma", "\n".join(p.gaps), "the user is told what to add by hand")

    def test_5_sse_server_keeps_its_type(self):
        """omp's mcp.json takes Claude's shape incl. `type: sse`; dropping it would make omp try stdio/http."""
        self.run_plan(Bundle(source="x", mcp=[McpServer("s", {"type": "sse", "url": "https://s/sse"})]), {"mcp"})
        self.assertEqual(json.loads((self.target / "mcp.json").read_text())["mcpServers"]["s"], {"type": "sse", "url": "https://s/sse"})

    def test_6_comments_between_key_and_items_keep_valid_yaml(self):
        """The indent used to come from the comment line, giving `  - new` over `- old`: invalid YAML."""
        text = "disabledExtensions:\n    # my comment\n\n- skill:mine\nb: 1\n"
        out = omp.add_disabled(text, ["skill:x"])
        self.assertIn('- "skill:x"\n', out)
        self.assertNotIn('  - "skill:x"', out, "new items take the existing items' indent")
        if yaml:
            self.assertEqual(yaml.safe_load(out), {"disabledExtensions": ["skill:x", "skill:mine"], "b": 1})
        self.assertIsNone(omp.add_disabled("disabledExtensions:\n  # c\n  a: 1\n", ["skill:x"]),
                          "a value that isn't a list is left to the user")
        self.assertEqual(omp.add_disabled("disabledExtensions:\n# c\nb: 1\n", ["skill:x"]),
                         'disabledExtensions:\n  - "skill:x"\n# c\nb: 1\n', "a null value becomes our list")

    def test_10_bridge_skips_stop_hooks_when_headless(self):
        """omp runs subagents headless; Stop hooks must fire for the main agent only, like Claude."""
        src = omp.bridge_source()
        end = src[src.index('pi.on("agent_end"'):]
        self.assertIn("if (!ctx.hasUI) return;", end[:end.index("});")])


if __name__ == "__main__":
    unittest.main()
