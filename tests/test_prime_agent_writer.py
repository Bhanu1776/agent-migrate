"""claude-code -> prime-agent on a fake home. Each test guards a promise the tool makes to the user."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from agent_migrate.model import PARTS, Bundle, Guard, Hook, McpServer
from agent_migrate.readers import claude_code
from agent_migrate.writers import prime_agent as w
from tests.test_claude_to_pi import SECRET, _w, fake_home
from tests.test_opencode_writer import NODE, run_node

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


    # --- regressions from the Sep 2026 review; each failed on the code before the fix ---

    def test_1_unparseable_settings_are_never_rewritten(self):
        """_load() turned a broken settings.json into {} and the rewrite dropped every user key."""
        broken = '{"defaultModel": "x",}'
        _w(self.target / "settings.json", broken)
        p = self.run_all()
        self.assertEqual((self.target / "settings.json").read_text(), broken)
        self.assertIn("settings.json is not valid JSON", self.shown(p))

    def test_3_invalid_hook_matcher_is_a_gap_not_a_crash(self):
        p = w.plan(Bundle(source="x", hooks=[Hook("pre_tool", "echo", matcher="(", origin="o")]), self.target, self.home, {"hooks"})
        self.assertIn("not a valid regex", self.shown(p))

    def test_5_sse_server_is_reported_not_miswritten_as_http(self):
        """prime-agent only speaks stdio + streamable HTTP; an SSE server written as http just fails to connect."""
        b = Bundle(source="x", mcp=[McpServer("s", {"type": "sse", "url": "https://s/sse"})])
        p = w.plan(b, self.target, self.home, {"mcp"})
        self.assertIn("SSE", self.shown(p))
        self.assertFalse([a for a in p.actions if a.apply], "nothing to write for an SSE-only server")

    def test_9_hook_command_text_is_never_printed(self):
        cmd = "curl -H 'Authorization: " + SECRET + "' https://x"
        p = w.plan(Bundle(source="x", hooks=[Hook("stop", cmd, origin="o")]), self.target, self.home, {"hooks"})
        self.assertNotIn("curl", self.shown(p), "hook commands can embed tokens; plans are printed")

    def test_12_chats_are_owner_only(self):
        """Transcripts hold whatever was pasted into them, secrets included."""
        self.run_all()
        [chat] = list((self.target / "sessions").glob("*.jsonl"))
        self.assertEqual(stat.S_IMODE(os.stat(chat).st_mode), 0o600)

    @unittest.skipUnless(NODE, "node not installed")
    def test_2_3_10_bridge_runtime(self):
        """Compound commands used to slip past guards, a bad regex crashed every tool call, and
        SessionStart/Stop hooks fired in headless subagent runs."""
        marker = Path(self.tmp.name) / "fired"
        b = Bundle(source="x", guards=[Guard(r"^git\s+push", "deny", "push-rule")],
                   hooks=[Hook("session_start", f"echo x >> {marker}", origin="o"), Hook("stop", f"echo x >> {marker}", origin="o")])
        for a in w.plan(b, self.target, self.home, {"hooks", "guards"}).actions:
            if a.apply:
                a.apply()
        bj = self.target / "agent-migrate" / "bridge.json"
        data = json.loads(bj.read_text())
        data["sources"]["x"]["guards"].append({"pattern": "(", "action": "deny", "origin": "broken"})  # hand-edited
        bj.write_text(json.dumps(data))
        out = run_node(f"""
import {{ existsSync }} from "node:fs";
const {{ default: bridge }} = await import({json.dumps(str(self.target / "extensions" / "agent-migrate-bridge.ts"))});
const handlers = {{}};
bridge({{ on: (n, f) => (handlers[n] = f) }});
const notes = [];
const headless = {{ cwd: {json.dumps(self.tmp.name)}, hasUI: false }};
const ui = {{ cwd: {json.dumps(self.tmp.name)}, hasUI: true, ui: {{ notify: (m) => notes.push(m) }} }};
const call = (code) => handlers.tool_call({{ toolName: "ipython", input: {{ code }} }}, headless);
const compound = await call("!cd /repo && git push origin main");
const quoted = await call('await bash("FOO=1 git push")');
const plain = await call("!ls");
await handlers.session_start({{ reason: "startup" }}, headless);
await handlers.agent_end({{}}, headless);
await new Promise((r) => setTimeout(r, 400));
const headlessFired = existsSync({json.dumps(str(marker))});
await handlers.session_start({{ reason: "startup" }}, ui);
console.log(JSON.stringify({{ compound: compound?.block ?? false, quoted: quoted?.block ?? false, plain: plain?.block ?? false,
  headlessFired, uiFired: existsSync({json.dumps(str(marker))}), warned: notes.some((n) => n.includes("broken")) }}));
""", Path(self.tmp.name))
        self.assertEqual(json.loads(out), {"compound": True, "quoted": True, "plain": False,
                                           "headlessFired": False, "uiFired": True, "warned": True})


if __name__ == "__main__":
    unittest.main()
