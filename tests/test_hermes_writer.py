"""claude-code -> hermes on a fake home. Each test guards a promise the tool makes to the user."""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_migrate import guards as guard_lib
from agent_migrate.model import Bundle, Guard, Hook, McpServer
from agent_migrate.readers import claude_code
from agent_migrate.writers import hermes
from tests.test_claude_to_pi import SECRET, _w, fake_home

PARTS = {"instructions", "skills", "prompts", "mcp", "memory", "hooks", "guards", "sessions"}

try:  # Hermes itself parses config.yaml with a YAML library; use one when present to prove it parses.
    import yaml
except ImportError:
    yaml = None


class ClaudeToHermes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = fake_home(root)
        _w(self.home / ".claude" / "commands" / "review.md", "---\ndescription: Review the diff\n---\nReview $ARGUMENTS")
        self.target = root / "hermes"
        _w(self.target / "SOUL.md", "You are my calm helper.")
        # Shape of Hermes's seeded config: a block mapping with user children + comments.
        _w(self.target / "config.yaml", "model:\n  default: x\n\nskills:\n  # nudge\n  creation_nudge_interval: 15\n")

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self):
        return hermes.plan(claude_code.read(self.home), self.target, self.home, PARTS)

    def apply(self):
        p = self.plan()
        for a in p.actions:
            if a.apply:
                a.apply()
        return p

    def snapshot(self):
        return {str(f.relative_to(self.target)): f.read_bytes() for f in self.target.rglob("*") if f.is_file()}

    def test_dry_run_writes_nothing_and_never_shows_secrets(self):
        before = self.snapshot()
        p = self.plan()
        self.assertEqual(self.snapshot(), before, "planning alone must not touch the target")
        shown = "\n".join([a.desc for a in p.actions] + p.gaps)
        self.assertNotIn(SECRET, shown, "plans are printed; secrets must never be")

    def test_full_run(self):
        p = self.apply()
        cfg = (self.target / "config.yaml").read_text()
        self.assertNotIn(SECRET, cfg, "config.yaml must only hold ${VAR} references, not secret values")
        env = self.target / ".env"
        self.assertEqual(stat.S_IMODE(os.stat(env).st_mode), 0o600, "secret-bearing .env must be owner-only")
        self.assertIn(SECRET, env.read_text())
        self.assertIn("creation_nudge_interval: 15", cfg, "the user's own settings must survive")
        self.assertTrue(list(self.target.glob("config.yaml.bak-*")), "first edit of config.yaml gets a backup")

        mcp = hermes._owned(cfg, "mcp_servers")["api"]
        var = mcp["headers"]["Authorization"]
        self.assertRegex(var, r"^\$\{AGENT_MIGRATE_MCP_[A-Z0-9_]+\}$", "Hermes interpolates ${VAR} from .env")
        self.assertIn(var[2:-1] + "=", env.read_text())
        self.assertEqual(hermes._owned(cfg, "skills")["disabled"], ["beta"], "a skill off in Claude must stay off")
        self.assertEqual(hermes._owned(cfg, "approvals")["deny"], ["security", "security *"],
                         "Bash(security:*) is a prefix rule: bare command and command + args")
        hooks = hermes._owned(cfg, "hooks")
        self.assertEqual(list(hooks), ["on_session_end"], "Stop maps to Hermes's per-turn end event")
        self.assertIn("TodoWrite", "\n".join(p.gaps), "a hook for a tool Hermes lacks is reported, not kept")
        if yaml:
            data = yaml.safe_load(cfg)
            self.assertEqual(data["skills"]["creation_nudge_interval"], 15)
            self.assertEqual(data["mcp_servers"]["api"]["url"], "https://x/mcp")

        soul = (self.target / "SOUL.md").read_text()
        self.assertIn("You are my calm helper.", soul, "user's own SOUL.md text must survive")
        self.assertIn("Always be kind.", soul)
        self.assertTrue((self.target / "skills" / "claude-code" / "alpha").is_symlink())
        review = (self.target / "skills" / "agent-migrate-prompts" / "review" / "SKILL.md").read_text()
        self.assertIn('description: "Review the diff"', review, "commands become skills Hermes can index")
        self.assertIn("$ARGUMENTS", "\n".join(p.gaps))
        mem = self.target / "skills" / "agent-migrate-memory" / "memory-work-repo"
        self.assertTrue((mem / "SKILL.md").is_file() and (mem / "fact.md").is_file())
        self.assertIn("state.db", "\n".join(p.gaps), "chats aren't written, and the user is told")

    def test_second_run_is_idempotent(self):
        self.apply()
        before = self.snapshot()
        p = self.apply()
        self.assertEqual(self.snapshot(), before, "re-running must not change or duplicate anything")
        writes = [a for a in p.actions if a.apply and a.part not in ("instructions",)]
        self.assertEqual(writes, [], "nothing new to do on a second run")

    def test_user_owned_keys_go_to_a_snippet_not_into_their_file(self):
        _w(self.target / "config.yaml", "skills:\n  disabled: [mine]\nmcp_servers: {}\n")
        p = self.apply()
        cfg = (self.target / "config.yaml").read_text()
        self.assertIn("disabled: [mine]", cfg, "never rewrite a list the user owns")
        self.assertEqual(cfg.count("disabled"), 1, "a duplicate key would make Hermes reject config.yaml")
        self.assertIn("api", hermes._owned(cfg, "mcp_servers"), "an empty {} mapping is safe to fill")
        snippet = (self.target / "agent-migrate" / "config-snippet.yaml").read_text()
        self.assertIn('"disabled": ["beta"]', snippet)
        self.assertIn("skills.disabled", "\n".join(p.gaps))
        if yaml:
            yaml.safe_load(cfg)

    def test_guard_script_blocks_and_asks(self):
        b = Bundle(source="codex", guards=[Guard(r"^rm\s+-rf\b", "deny", "rules/a"), Guard(r"^git\s+push(\s|$)", "ask", "rules/b")])
        p = hermes.plan(b, self.target, self.home, {"guards"})
        for a in p.actions:
            if a.apply:
                a.apply()
        guard = self.target / "agent-migrate" / "guard.py"

        def run(cmd):
            out = subprocess.run([sys.executable, str(guard)], input=json.dumps({"tool_input": {"command": cmd}}),
                                 capture_output=True, text=True, check=True).stdout
            return json.loads(out).get("action")

        self.assertEqual(run("rm -rf /"), "block", "a deny regex that can't be a glob is still enforced")
        self.assertEqual(run("git push origin"), "approve", "ask rules escalate to Hermes's approval prompt")
        self.assertIsNone(run("ls"))
        self.assertIn("guard.py", json.dumps(hermes._owned((self.target / "config.yaml").read_text(), "hooks")))

    def test_regex_to_glob(self):
        self.assertEqual(hermes._regex_to_globs(r"^git\ push(\s.*)?$"), ["git push", "git push *"])
        self.assertEqual(hermes._regex_to_globs(r"^git\s+(?:push|fetch)(\s|$)"),
                         ["git push", "git push *", "git fetch", "git fetch *"])
        self.assertEqual(hermes._regex_to_globs(r"^.*prod.*$"), ["*prod*"])
        self.assertIsNone(hermes._regex_to_globs(r"^rm\s+-rf\b"), "unknown regex syntax must not become a wrong glob")


    # --- regressions from the Sep 2026 review; each failed on the code before the fix ---

    def run_plan(self, b, parts):
        p = hermes.plan(b, self.target, self.home, parts)
        for a in p.actions:
            if a.apply:
                a.apply()
        return p

    @unittest.skipUnless(yaml, "PyYAML not installed: the writer can't detect broken YAML without it")
    def test_1_unparseable_config_yaml_is_left_alone(self):
        """Splicing our block into a file Hermes can't parse would make it look like our fault, and hide the user's fix."""
        broken = "model: [unclosed\nskills:\n  x: 1\n"
        _w(self.target / "config.yaml", broken)
        p = self.run_plan(Bundle(source="x", mcp=[McpServer("s", {"command": "run"})]), {"mcp"})
        self.assertEqual((self.target / "config.yaml").read_text(), broken)
        self.assertIn("does not parse", "\n".join(p.gaps))
        self.assertIn('"s"', (self.target / "agent-migrate" / "config-snippet.yaml").read_text(), "the entry still reaches the user")

    def test_2_guard_script_checks_every_subcommand(self):
        """`cd x && rm -rf /` used to pass a `^rm\\s+-rf` rule, which only saw the whole string."""
        b = Bundle(source="codex", guards=[Guard(r"^rm\s+-rf\b", "deny", "rules/a"), Guard("(", "deny", "broken")])
        self.run_plan(b, {"guards"})
        gdir = self.target / "agent-migrate"
        self.assertEqual((gdir / "am_guards.py").read_text(), Path(guard_lib.__file__).read_text(),
                         "the hook must split commands exactly like the tested guards module")

        def run(cmd):
            r = subprocess.run([sys.executable, str(gdir / "guard.py")], input=json.dumps({"tool_input": {"command": cmd}}),
                               capture_output=True, text=True, check=True)
            return json.loads(r.stdout).get("action")

        self.assertEqual(run("cd /tmp && rm -rf /"), "block")
        self.assertEqual(run("sudo rm -rf /"), "block")
        self.assertIsNone(run("ls"), "a broken rule is skipped, not fail-closed on every command")

    def test_4_markers_inside_source_text_do_not_nest_on_rerun(self):
        text = "mine\n<!-- agent-migrate:x:start -->\nold\n<!-- agent-migrate:x:end -->\ntail"
        b = Bundle(source="x", instructions=text)
        self.run_plan(b, {"instructions"})
        once = (self.target / "SOUL.md").read_text()
        self.run_plan(b, {"instructions"})
        self.assertEqual((self.target / "SOUL.md").read_text(), once, "a re-run replaces our block, not nests in it")

    def test_5_sse_server_gets_hermes_transport_and_no_type_key(self):
        """Claude's `type: sse` must become Hermes's `transport: sse`, or Hermes tries streamable HTTP and fails."""
        self.run_plan(Bundle(source="x", mcp=[McpServer("s", {"type": "sse", "url": "https://s/sse"})]), {"mcp"})
        entry = hermes._owned((self.target / "config.yaml").read_text(), "mcp_servers")["s"]
        self.assertEqual(entry, {"url": "https://s/sse", "transport": "sse"})

    def test_9_hook_command_text_is_never_printed(self):
        cmd = "curl -H 'Authorization: " + SECRET + "' https://x"
        p = hermes.plan(Bundle(source="x", hooks=[Hook("stop", cmd, origin="o")]), self.target, self.home, {"hooks"})
        self.assertNotIn("curl", "\n".join([a.desc for a in p.actions] + p.gaps), "hook commands can embed tokens")

    def test_11_env_values_load_back_verbatim(self):
        """Hermes expands ${NAME} in every .env value (quoted or not); a secret containing `${HOME}` must
        not turn into the user's home path. Reference: hermes_cli/env_loader.py + python-dotenv rules."""
        value = "it's ${HOME} \\ \"q\""
        self.run_plan(Bundle(source="x", mcp=[McpServer("s", {"command": "run", "env": {"TOKEN": value}})]), {"mcp"})
        line = next(l for l in (self.target / ".env").read_text().splitlines() if l.startswith("AGENT_MIGRATE_MCP_S_ENV_TOKEN="))
        raw = line.split("=", 1)[1]
        if raw.startswith("'"):
            raw = raw[1:-1]
        else:  # python-dotenv's double-quote escapes
            raw = re.sub(r"\\([\\'\"abfnrtv])", lambda m: {"n": "\n"}.get(m.group(1), m.group(1)), raw[1:-1])
        rx = re.compile(r"\$\{(?P<name>[^\}:]*)(?::-(?P<default>[^\}]*))?\}")  # dotenv.variables._posix_variable
        env = {"HOME": "/Users/someone"}
        loaded = rx.sub(lambda m: env.get(m["name"], m["default"] or ""), raw)
        self.assertEqual(loaded, value)

    def test_14_symlink_loop_in_skills_does_not_hang(self):
        """os.walk(followlinks=True) over skills/a/{l1,l2} -> skills never finished."""
        skills = self.target / "skills"
        (skills / "a").mkdir(parents=True)
        _w(skills / "a" / "SKILL.md", "---\nname: a\n---\n")
        (skills / "a" / "l1").symlink_to(skills)
        (skills / "a" / "l2").symlink_to(skills)
        code = f"from pathlib import Path; from agent_migrate.writers import hermes; print(sorted(hermes._visible_skills(Path({str(skills)!r}))))"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20, check=True,
                             cwd=Path(__file__).resolve().parent.parent).stdout
        self.assertEqual(out.strip(), "['a']")


if __name__ == "__main__":
    unittest.main()
