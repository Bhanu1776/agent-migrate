"""claude-code -> pi on a fake home. Each test guards a promise the tool makes to the user."""
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from agent_migrate.cli import main

SECRET = "Bearer sk-live-DO-NOT-LEAK"


def _w(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def fake_home(root: Path) -> Path:
    home = root / "home"
    c = home / ".claude"
    _w(c / "CLAUDE.md", "Always be kind.")
    _w(c / "skills" / "alpha" / "SKILL.md", "---\nname: alpha\ndescription: a\n---\nbody")
    _w(c / "skills" / "beta" / "SKILL.md", "---\nname: beta\ndescription: b\n---\nbody")
    _w(c / "settings.json", json.dumps({
        "skillOverrides": {"beta": "off"},
        "permissions": {"deny": ["Bash(security:*)"]},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo done"}]}],
            "PostToolUse": [{"matcher": "TodoWrite", "hooks": [{"type": "command", "command": "echo todo"}]}],
        },
    }))
    _w(home / ".claude.json", json.dumps({"mcpServers": {"api": {"type": "http", "url": "https://x/mcp", "headers": {"Authorization": SECRET}}}}))
    _w(c / "projects" / "-work-repo" / "memory" / "MEMORY.md", "- [fact](fact.md) — a fact")
    _w(c / "projects" / "-work-repo" / "memory" / "fact.md", "fact")
    recs = [
        {"type": "user", "cwd": "/work/repo", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "fix the bug"}},
        {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z", "message": {"model": "m", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "timestamp": "2026-01-01T00:00:02Z", "message": {"content": [{"type": "tool_result", "content": "a.py"}]}},
        {"type": "assistant", "timestamp": "2026-01-01T00:00:03Z", "message": {"model": "m", "content": [{"type": "text", "text": "fixed"}]}},
        {"type": "user", "isSidechain": True, "timestamp": "2026-01-01T00:00:04Z", "message": {"content": "subagent chatter"}},
    ]
    _w(c / "projects" / "-work-repo" / "s1.jsonl", "\n".join(json.dumps(r) for r in recs))
    return home


class ClaudeToPi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = fake_home(root)
        self.target = root / "pi"
        # Pretend the MCP adapter is installed so tests never shell out to `pi install`.
        _w(self.target / "settings.json", json.dumps({"packages": ["npm:pi-mcp-adapter"]}))
        _w(self.target / "AGENTS.md", "My own pi notes.")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["claude-code", "pi", "--home", str(self.home), "--target", str(self.target), *extra])
        return code, out.getvalue()

    def snapshot(self):
        return sorted(str(p.relative_to(self.target)) for p in self.target.rglob("*"))

    def test_dry_run_writes_nothing(self):
        before = self.snapshot()
        code, out = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.snapshot(), before, "a dry run must not touch the target")
        self.assertNotIn(SECRET, out, "plans are shown on screen; secrets must never be printed")

    def test_full_run(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0, out)
        self.assertNotIn(SECRET, out)

        mcp = self.target / "mcp.json"
        self.assertEqual(stat.S_IMODE(os.stat(mcp).st_mode), 0o600, "secret-bearing config must be owner-only")
        self.assertIn("api", json.loads(mcp.read_text())["mcpServers"])

        agents = (self.target / "AGENTS.md").read_text()
        self.assertIn("My own pi notes.", agents, "user's existing instructions must survive")
        self.assertIn("Always be kind.", agents)
        self.assertTrue(list(self.target.glob("AGENTS.md.bak-*")), "pre-existing AGENTS.md gets a backup")

        self.assertIn("-skills/beta", json.loads((self.target / "settings.json").read_text())["skills"],
                      "a skill switched off in Claude must stay off")
        self.assertTrue((self.target / "skills" / "alpha").is_symlink())

        bridge = json.loads((self.target / "agent-migrate" / "bridge.json").read_text())["sources"]["claude-code"]
        self.assertEqual([h["event"] for h in bridge["hooks"]], ["stop"], "TodoWrite hook can't fire in pi: report, don't keep")
        self.assertIn("TodoWrite", out)
        self.assertEqual(bridge["guards"][0]["action"], "deny")

        [chat] = list((self.target / "sessions").rglob("*.jsonl"))
        msgs = [json.loads(l)["message"] for l in chat.read_text().splitlines() if json.loads(l)["type"] == "message"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"],
                         "tool plumbing and subagent chatter must not show up as turns")
        self.assertIn("→ Bash", msgs[1]["content"][0]["text"])
        self.assertIn("fixed", msgs[1]["content"][0]["text"])

    def test_second_run_is_idempotent(self):
        self.run_cli()
        agents_before = (self.target / "AGENTS.md").read_text()
        self.run_cli()
        self.assertEqual(len(list((self.target / "sessions").rglob("*.jsonl"))), 1, "re-running must not duplicate chats")
        self.assertEqual((self.target / "AGENTS.md").read_text(), agents_before, "owned blocks are replaced, not appended")
        self.assertEqual(len(list(self.target.glob("AGENTS.md.bak-*"))), 1)


if __name__ == "__main__":
    unittest.main()
