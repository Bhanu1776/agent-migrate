"""claude-code -> prime-agent on a fake home. Each test guards a promise the tool makes to the user."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from agent_migrate.model import PARTS
from agent_migrate.readers import claude_code
from agent_migrate.writers import prime_agent as w
from tests.test_claude_to_pi import SECRET, _w, fake_home

ENV_SECRET = "env-secret-DO-NOT-LEAK"


class ClaudeToPrimeAgent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = fake_home(root)
        cj = json.loads((self.home / ".claude.json").read_text())
        cj["mcpServers"]["local"] = {"command": "node", "args": ["s.js"], "env": {"TOKEN": ENV_SECRET, "KEY": "${MY_KEY}"}}
        cj["mcpServers"]["linear"] = {"url": "https://linear/mcp"}
        cj["mcpServers"]["envauth"] = {"url": "https://e/mcp", "headers": {"Authorization": "Bearer ${E_TOKEN}"}}
        _w(self.home / ".claude.json", json.dumps(cj))
        self.target = root / "prime"
        _w(self.target / "AGENTS.md", "My own prime notes.")
        _w(self.target / "settings.json", json.dumps({"defaultModel": "x"}))

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self):
        return w.plan(claude_code.read(self.home), self.target, self.home, set(PARTS))

    def run_all(self):
        p = self.plan()
        for a in p.actions:
            if a.apply:
                a.apply()
        return p

    def shown(self, p):
        return "\n".join([a.desc for a in p.actions] + p.gaps)

    def snapshot(self):
        return sorted(str(p.relative_to(self.target)) for p in self.target.rglob("*"))

    def test_dry_run_writes_nothing(self):
        before = self.snapshot()
        p = self.plan()
        self.assertEqual(self.snapshot(), before, "planning must not touch the target")
        self.assertNotIn(SECRET, self.shown(p), "plans are shown on screen; secrets must never be printed")
        self.assertNotIn(ENV_SECRET, self.shown(p))

    def test_full_run(self):
        p = self.run_all()
        settings_path = self.target / "settings.json"
        self.assertEqual(stat.S_IMODE(os.stat(settings_path).st_mode), 0o600, "settings.json now holds MCP headers")
        s = json.loads(settings_path.read_text())
        self.assertEqual(s["defaultModel"], "x", "user's own settings must survive")
        self.assertTrue(list(self.target.glob("settings.json.bak-*")))

        mcp = s["mcpServers"]
        self.assertEqual(mcp["api"]["type"], "http", "prime-agent rejects servers without a type")
        self.assertEqual(mcp["local"]["env"], {"TOKEN": {"env": "TOKEN"}, "KEY": {"env": "MY_KEY"}},
                         "prime-agent only accepts env references; a literal would break the server")
        self.assertNotIn(ENV_SECRET, settings_path.read_text(), "literal env secrets have nowhere safe to go in prime-agent")
        self.assertEqual(mcp["envauth"], {"type": "http", "url": "https://e/mcp", "bearerTokenEnvVar": "E_TOKEN"},
                         "prime-agent sends headers literally; `${VAR}` must become its native env reference")
        self.assertNotIn("linear", mcp, "reserved built-in names are ignored by prime-agent, so report instead")
        gaps = "\n".join(p.gaps)
        self.assertIn("export TOKEN", gaps)
        self.assertIn("'linear'", gaps)

        agents = (self.target / "AGENTS.md").read_text()
        self.assertIn("My own prime notes.", agents)
        self.assertIn("Always be kind.", agents)
        self.assertIn("ipython", agents, "migrated instructions name Claude tools; the notes explain prime-agent's")

        self.assertIn("-skills/beta", s["skills"], "a skill switched off in Claude must stay off")
        self.assertTrue((self.target / "skills" / "alpha").is_symlink())

        bridge = json.loads((self.target / "agent-migrate" / "bridge.json").read_text())["sources"]["claude-code"]
        self.assertEqual([h["event"] for h in bridge["hooks"]], ["stop"], "TodoWrite hook can't fire here: report it")
        self.assertIn("TodoWrite", gaps)
        self.assertEqual(bridge["guards"][0]["action"], "deny")
        self.assertIn("ipython", gaps, "guards are best effort in a Python-only harness; the user must know")
        self.assertTrue((self.target / "extensions" / "agent-migrate-bridge.ts").is_file())
        self.assertTrue((self.target / "memory" / "-work-repo" / "MEMORY.md").is_file())

        [chat] = list((self.target / "sessions").glob("*.jsonl"))
        lines = [json.loads(l) for l in chat.read_text().splitlines()]
        self.assertEqual(lines[0]["id"], chat.stem, "prime-agent resumes sessions/<id>.jsonl by header id")
        self.assertEqual([l["message"]["role"] for l in lines if l["type"] == "message"], ["user", "assistant"])

    def test_second_run_is_idempotent(self):
        self.run_all()
        agents = (self.target / "AGENTS.md").read_text()
        settings = (self.target / "settings.json").read_text()
        p = self.run_all()
        self.assertEqual((self.target / "AGENTS.md").read_text(), agents, "owned blocks are replaced, not appended")
        self.assertEqual((self.target / "settings.json").read_text(), settings)
        self.assertEqual(len(list((self.target / "sessions").glob("*.jsonl"))), 1, "re-running must not duplicate chats")
        self.assertEqual(len(list(self.target.glob("AGENTS.md.bak-*"))), 1)
        self.assertFalse([a for a in p.actions if a.part in ("skills", "prompts", "mcp", "memory", "sessions") and a.kind != "skip"],
                         "nothing new to add on a second run")


if __name__ == "__main__":
    unittest.main()
