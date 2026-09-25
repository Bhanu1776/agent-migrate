import json
import re
import tempfile
import unittest
from pathlib import Path

from agent_migrate.readers.codex import read

SECRET = "sk-test-SECRET-123"

CONFIG = f"""
model = "gpt-5.6-terra"
notify = ["/usr/bin/say", "done"]

[mcp_servers.local]
command = "npx"
args = ["-y", "some-mcp"]
env = {{ API_KEY = "{SECRET}" }}
startup_timeout_sec = 60

[mcp_servers.remote]
url = "https://example.com/mcp"
http_headers = {{ "X-Key" = "{SECRET}" }}
bearer_token_env_var = "REMOTE_TOKEN"

[mcp_servers.off]
command = "nope"
enabled = false
"""

RULES = """
prefix_rule(pattern=["git", "push", "--force"], decision="forbidden")
prefix_rule(pattern=["rm", ["-rf", "-fr"]], decision="prompt")
prefix_rule(pattern=["ls"], decision="allow")
"""


def rollout(user_text):
    recs = [
        {"timestamp": "2026-09-25T07:41:14.172Z", "type": "session_meta",
         "payload": {"id": "sid-1", "cwd": "/work", "timestamp": "2026-09-25T07:41:13.411Z",
                     "thread_source": "user"}},
        {"timestamp": "2026-09-25T07:41:17.121Z", "type": "turn_context", "payload": {"model": "gpt-x"}},
        {"timestamp": "2026-09-25T07:41:17.200Z", "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "# AGENTS.md instructions\n\n<INSTRUCTIONS>\nbe nice\n</INSTRUCTIONS>"},
                {"type": "input_text", "text": "<environment_context>\n  <cwd>/work</cwd>\n</environment_context>"}]}},
        {"timestamp": "2026-09-25T07:41:20.000Z", "type": "response_item", "payload": {
            "type": "reasoning", "summary": [], "encrypted_content": "zzz"}},
        {"timestamp": "2026-09-25T07:41:21.000Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "shell", "arguments": json.dumps({"cmd": "x" * 500})}},
        {"timestamp": "2026-09-25T07:41:22.000Z", "type": "response_item", "payload": {
            "type": "function_call_output", "output": "HUGE TOOL OUTPUT"}},
        {"timestamp": "2026-09-25T07:41:23.000Z", "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]}},
    ]
    if user_text:
        recs.insert(3, {"timestamp": "2026-09-25T07:41:18.000Z", "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": user_text}]}})
    return "\n".join(json.dumps(r) for r in recs) + "\n"


class CodexReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        c = self.home / ".codex"
        (c / "skills" / "my-skill").mkdir(parents=True)
        (c / "skills" / "my-skill" / "SKILL.md").write_text("---\nname: renamed\ndescription: d\n---\nbody")
        (c / "skills" / ".system" / "builtin").mkdir(parents=True)
        (c / "skills" / ".system" / "builtin" / "SKILL.md").write_text("x")
        (c / "AGENTS.md").write_text("global rules")
        (c / "config.toml").write_text(CONFIG)
        (c / "rules").mkdir()
        (c / "rules" / "default.rules").write_text(RULES)
        (c / "memories").mkdir()
        (c / "memories" / "MEMORY.md").write_text("facts")
        day = c / "sessions" / "2026" / "09" / "25"
        day.mkdir(parents=True)
        (day / "rollout-2026-09-25T07-41-13-sid-1.jsonl").write_text(rollout("fix the bug please"))
        (day / "rollout-2026-09-25T08-00-00-sid-2.jsonl").write_text(rollout(None).replace("sid-1", "sid-2"))
        (c / "session_index.jsonl").write_text(
            json.dumps({"id": "sid-1", "thread_name": "old"}) + "\n" + json.dumps({"id": "sid-1", "thread_name": "Fix bug"}) + "\n")
        self.b = read(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_basic_parts(self):
        self.assertEqual(self.b.instructions, "global rules")
        # Codex's own .system skills ship with Codex; migrating them would duplicate built-ins.
        self.assertEqual([s.name for s in self.b.skills], ["renamed"])
        self.assertEqual([(m.project, m.path.name) for m in self.b.memory], [("_global", "memories")])

    def test_secrets_stay_inside_mcp_config(self):
        # Writers put McpServer.config only in 0600 files; a secret anywhere else would leak into reports.
        self.assertEqual(sorted(s.name for s in self.b.mcp), ["local", "remote"])
        mcp = {s.name: s.config for s in self.b.mcp}
        self.assertEqual(mcp["local"]["env"]["API_KEY"], SECRET)
        self.assertEqual(mcp["remote"]["headers"]["Authorization"], "Bearer ${REMOTE_TOKEN}")
        outside = [self.b.instructions or "", *self.b.gaps, *(h.command for h in self.b.hooks),
                   *(g.pattern for g in self.b.guards)]
        self.assertFalse(any(SECRET in x for x in outside))
        # Disabled server is dropped but the human is told.
        self.assertTrue(any("off" in g and "disabled" in g for g in self.b.gaps))

    def test_forbidden_rule_blocks_only_that_command(self):
        deny = [g for g in self.b.guards if g.action == "deny"]
        self.assertEqual(len(deny), 1)
        rx = re.compile(deny[0].pattern)
        self.assertTrue(rx.search("git push --force origin main"))
        self.assertTrue(rx.search("git  push --force"))
        self.assertFalse(rx.search("git push origin main"))
        self.assertFalse(rx.search("git push --force-with-lease"))
        ask = [g for g in self.b.guards if g.action == "ask"]
        self.assertEqual(len(ask), 1)
        self.assertTrue(re.search(ask[0].pattern, "rm -fr /tmp/x"))
        self.assertTrue(any("1 allow" in g for g in self.b.gaps))

    def test_notify_becomes_stop_hook_with_gap(self):
        self.assertEqual([(h.event, h.command) for h in self.b.hooks], [("stop", "/usr/bin/say done")])
        self.assertTrue(any(g.startswith("notify:") for g in self.b.gaps))

    def test_sessions(self):
        sessions = list(self.b.sessions())
        # sid-2 has only injected context: it is not a real conversation and must be dropped.
        self.assertEqual([s.id for s in sessions], ["sid-1"])
        s = sessions[0]
        self.assertEqual(s.title, "Fix bug")
        users = [m for m in s.messages if m.role == "user"]
        # AGENTS.md and <environment_context> injections must not pose as things the user said.
        self.assertEqual([m.text for m in users], ["fix the bug please"])
        asst = [m for m in s.messages if m.role == "assistant"]
        self.assertEqual(len(asst), 1)  # tool call line + reply merged
        self.assertTrue(asst[0].text.startswith("→ shell: "))
        self.assertLessEqual(len(asst[0].text.split("\n")[0]), len("→ shell: ") + 200)
        self.assertNotIn("HUGE TOOL OUTPUT", asst[0].text)
        self.assertEqual(asst[0].model, "gpt-x")
        self.assertEqual(users[0].ts_ms, 1790322078000)


    def _one(self, text, meta_ts=True):
        day = self.home / ".codex" / "sessions" / "2026" / "09" / "26"
        day.mkdir(parents=True, exist_ok=True)
        body = rollout(text).replace("sid-1", "sid-9")
        if not meta_ts:
            body = body.replace(', "timestamp": "2026-09-25T07:41:13.411Z"', "")
        (day / "rollout-2026-09-26T00-00-00-sid-9.jsonl").write_text(body)
        return next(s for s in read(self.home).sessions() if s.id == "sid-9")

    def test_user_typed_xml_is_kept(self):
        # Only tags Codex injects are context; a user's own <task>..</task> is a real message.
        s = self._one("<task>ship it</task>")
        self.assertIn("<task>ship it</task>", [m.text for m in s.messages if m.role == "user"])

    def test_missing_meta_timestamp_still_has_a_start_date(self):
        # Writers build file names and dates from `started`; "" crashed the opencode export.
        s = self._one("hi", meta_ts=False)
        self.assertTrue(s.started.startswith("2026-09-25"), s.started)


if __name__ == "__main__":
    unittest.main()
