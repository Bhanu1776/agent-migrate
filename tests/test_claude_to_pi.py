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


class ReviewRegressions(ClaudeToPi):
    """Fable review findings for the pi writer; each failed before the fix."""

    def _settings(self, extra):
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        s.update(extra)
        _w(self.home / ".claude" / "settings.json", json.dumps(s))

    def test_1_broken_json_is_never_clobbered(self):
        broken_settings = '{"packages": ["npm:pi-mcp-adapter"], "model": "x",}'
        broken_mcp = '{"mcpServers": {"mine": {"command": "x"}},}'
        _w(self.target / "settings.json", broken_settings)
        _w(self.target / "mcp.json", broken_mcp)
        code, out = self.run_cli()
        self.assertEqual((self.target / "settings.json").read_text(), broken_settings, "user's other keys would be lost")
        self.assertEqual((self.target / "mcp.json").read_text(), broken_mcp)
        self.assertIn("is not valid JSON", out)

    def test_1_first_change_to_settings_is_backed_up(self):
        self.run_cli()
        [bak] = self.target.glob("settings.json.bak-*")
        self.assertEqual(json.loads(bak.read_text()), {"packages": ["npm:pi-mcp-adapter"]})
        self.run_cli()
        self.assertEqual(len(list(self.target.glob("settings.json.bak-*"))), 1, "re-runs must not pile up backups")

    def test_3_invalid_hook_matcher_is_a_gap_not_a_crash(self):
        self._settings({"hooks": {"PreToolUse": [{"matcher": "mcp__(", "hooks": [{"type": "command", "command": "x"}]}]}})
        code, out = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("not a valid regex", out)

    def test_4_markers_inside_instructions_do_not_nest(self):
        self.run_cli()
        # User symlinks CLAUDE.md to the migrated file, then migrates again (twice).
        _w(self.home / ".claude" / "CLAUDE.md", (self.target / "AGENTS.md").read_text())
        self.run_cli()
        once = (self.target / "AGENTS.md").read_text()
        self.run_cli()
        self.assertEqual((self.target / "AGENTS.md").read_text(), once, "each run must not grow the file")

    def test_5_sse_servers_keep_their_transport(self):
        _w(self.home / ".claude.json", json.dumps({"mcpServers": {"old": {"type": "sse", "url": "https://x/sse"}}}))
        self.run_cli()
        cfg = json.loads((self.target / "mcp.json").read_text())["mcpServers"]["old"]
        self.assertEqual(cfg.get("httpTransport"), "sse")
        self.assertNotIn("type", cfg, "pi-mcp-adapter has no `type` key")

    def test_8_trial_target_does_not_run_npm(self):
        _w(self.target / "settings.json", "{}")
        code, out = self.run_cli("--dry-run")
        self.assertNotIn("run   pi install", out)
        self.assertIn("pi-mcp-adapter not installed in this target", out)

    def test_9_hook_commands_are_not_printed(self):
        self._settings({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "curl -H 'Authorization: Bearer tok-SECRET'"}]}],
                                  "Notification": [{"hooks": [{"type": "command", "command": "notify tok-SECRET"}]}]}})
        code, out = self.run_cli()
        self.assertNotIn("tok-SECRET", out, "hook commands can carry tokens")

    def test_12_chats_are_owner_only(self):
        self.run_cli()
        [chat] = list((self.target / "sessions").rglob("*.jsonl"))
        self.assertEqual(stat.S_IMODE(os.stat(chat).st_mode), 0o600, "chats can hold pasted secrets")

    def test_13_bare_bash_deny_is_migrated(self):
        self._settings({"permissions": {"deny": ["Bash"]}})
        self.run_cli()
        guards = json.loads((self.target / "agent-migrate" / "bridge.json").read_text())["sources"]["claude-code"]["guards"]
        self.assertEqual([g["action"] for g in guards], ["deny"])


if __name__ == "__main__":
    unittest.main()
