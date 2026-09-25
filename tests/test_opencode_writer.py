"""claude-code -> opencode on a fake home. Each test guards a promise the tool makes to the user."""
import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_migrate.model import Bundle, Guard, Hook, McpServer, Message, Session
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


NODE = shutil.which("node")


def run_node(script: str, cwd: Path) -> str:
    """Run an .mjs driver against a generated TS runtime (node >= 23 strips types itself)."""
    (cwd / "driver.mjs").write_text(script)
    return subprocess.run([NODE, "driver.mjs"], cwd=cwd, capture_output=True, text=True, timeout=60, check=True).stdout


class ReviewFindings(unittest.TestCase):
    """Regressions from the Sep 2026 review; each test failed on the code before the fix."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.target = Path(self.tmp.name) / "opencode"
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, b, parts):
        return w.plan(b, self.target, self.home, parts)

    def test_1_non_object_config_is_left_alone_not_crashed_or_clobbered(self):
        """Valid JSON that isn't an object used to crash plan(); it must be a gap and stay byte-identical."""
        _w(self.target / "opencode.json", "[1, 2]")
        p = self.plan(Bundle(source="x", guards=[Guard(r"^rm(\s.*)?$", "deny", "o")]), {"guards"})
        apply(p)
        self.assertEqual((self.target / "opencode.json").read_text(), "[1, 2]")
        self.assertTrue(any("opencode.json" in g for g in p.gaps))

    def test_3_invalid_hook_matcher_is_a_gap_not_a_crash(self):
        """A matcher like `(` used to raise re.error and abort the whole run."""
        p = self.plan(Bundle(source="x", hooks=[Hook("pre_tool", "echo", matcher="(", origin="settings.json")]), {"hooks"})
        self.assertTrue(any("not a valid regex" in g for g in p.gaps))

    def test_4_markers_inside_source_text_do_not_nest_on_rerun(self):
        """CLAUDE.md symlinked to a migrated AGENTS.md carries our markers; re-runs must not grow the file."""
        text = "mine\n<!-- agent-migrate:x:start -->\nold\n<!-- agent-migrate:x:end -->\ntail"
        b = Bundle(source="x", instructions=text)
        apply(self.plan(b, {"instructions"}))
        once = (self.target / "AGENTS.md").read_text()
        apply(self.plan(b, {"instructions"}))
        twice = (self.target / "AGENTS.md").read_text()
        self.assertEqual(twice, once, "a second run must replace our block, not nest inside it")
        self.assertEqual(once.count("<!-- agent-migrate:x:start -->"), 1)

    def test_5_sse_server_becomes_remote_without_a_type_opencode_rejects(self):
        """opencode's remote client falls back to SSE itself; `type: sse` would fail its config schema."""
        b = Bundle(source="x", mcp=[McpServer("s", {"type": "sse", "url": "https://s/sse"})])
        apply(self.plan(b, {"mcp"}))
        cfg = json.loads((self.target / "opencode.json").read_text())
        self.assertEqual(cfg["mcp"]["s"], {"type": "remote", "url": "https://s/sse", "enabled": True})

    def test_7_blank_start_time_and_one_bad_chat_do_not_sink_the_rest(self):
        """An empty `started` used to crash _export, and one bad chat aborted every later one."""
        msgs = [Message("user", "hi", 1_700_000_000_000), Message("assistant", "yo", 1_700_000_001_000)]
        good = Session("good", "/w", "", msgs)
        bad = Session("bad", "/w", "not-a-date", msgs)
        b = Bundle(source="claude-code", sessions=lambda: iter([bad, good]))
        p = self.plan(b, {"sessions"})
        self.assertTrue(any(g.startswith("1 chats") for g in p.gaps), "the skipped chat is counted for the user")
        apply(p)
        data = json.loads((self.target / "agent-migrate" / "sessions" / "good.json").read_text())
        self.assertEqual(data["info"]["time"]["created"], 1_700_000_000_000, "blank start falls back to the first message")

    def test_9_hook_command_text_is_never_printed(self):
        """Hook commands can embed tokens (curl -H ...); plans are printed, so only event/matcher/origin show."""
        cmd = "curl -H 'Authorization: " + SECRET + "' https://x"
        p = self.plan(Bundle(source="x", hooks=[Hook("stop", cmd, origin="settings.json")]), {"hooks"})
        shown = "\n".join([a.desc for a in p.actions] + p.gaps)
        self.assertNotIn("curl", shown)
        self.assertNotIn(SECRET, shown)

    @unittest.skipUnless(NODE, "node not installed")
    def test_3_10_plugin_skips_subagent_idle_and_survives_bad_matcher(self):
        """Stop hooks fired on every subagent's session.idle; a bad matcher regex threw on every tool call."""
        marker = Path(self.tmp.name) / "stopped"
        hooks = [Hook("stop", f"echo x >> {marker}", origin="s"), Hook("pre_tool", "exit 2", matcher="Bash", origin="s")]
        apply(self.plan(Bundle(source="x", hooks=hooks), {"hooks"}))
        # A bad matcher can't come through plan() (it's a gap there), but a hand-edited bridge.json can hold one.
        bj = self.target / "agent-migrate" / "bridge.json"
        data = json.loads(bj.read_text())
        data["sources"]["x"]["hooks"].append({"event": "pre_tool", "command": "exit 2", "matcher": "(", "env": {}})
        bj.write_text(json.dumps(data))
        out = run_node(f"""
import {{ existsSync }} from "node:fs";
const {{ AgentMigrateBridge }} = await import({json.dumps(str(self.target / "plugins" / "agent-migrate-bridge.ts"))});
const h = await AgentMigrateBridge({{ directory: {json.dumps(self.tmp.name)} }});
const wait = () => new Promise((r) => setTimeout(r, 400));
await h.event({{ event: {{ type: "session.created", properties: {{ info: {{ id: "child", parentID: "main" }} }} }} }});
await h.event({{ event: {{ type: "session.idle", properties: {{ sessionID: "child" }} }} }});
await wait();
const afterChild = existsSync({json.dumps(str(marker))});
let blocked = false;
try {{ await h["tool.execute.before"]({{ tool: "read" }}, {{ args: {{}} }}); }} catch {{ blocked = true; }}
await h.event({{ event: {{ type: "session.idle", properties: {{ sessionID: "main" }} }} }});
await wait();
console.log(JSON.stringify({{ afterChild, blocked, afterMain: existsSync({json.dumps(str(marker))}) }}));
""", Path(self.tmp.name))
        self.assertEqual(json.loads(out), {"afterChild": False, "blocked": False, "afterMain": True})


if __name__ == "__main__":
    unittest.main()
