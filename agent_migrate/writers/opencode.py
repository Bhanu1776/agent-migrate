"""opencode writer: Bundle -> Plan of file changes under opencode's global config dir.

Mapped from opencode 1.18 source (github.com/sst/opencode, packages/opencode/src):
- instructions: <config>/AGENTS.md (session/instruction.ts). NB: opencode reads ~/.claude/CLAUDE.md
  only while that file is absent, so the migrated text goes into an owned block there.
- skills: opencode already scans ~/.claude/skills and ~/.agents/skills (skill/index.ts); only the
  rest are linked into <config>/skills. "Off" skills become `permission.skill.<name> = "deny"`,
  which hides them from the agent.
- prompts: <config>/commands/<name>.md (config/command.ts). Only `description` frontmatter is kept:
  Claude's `model: sonnet` etc. would fail opencode's schema and break config loading.
- mcp: `mcp` in <config>/opencode.json (local: command array + environment; remote: url + headers).
- guards: native `permission.bash` wildcard rules, appended so they win (last match wins).
- hooks + memory: a generated plugin, <config>/plugins/agent-migrate-bridge.ts, reading
  <config>/agent-migrate/bridge.json and <config>/memory/<project>/.
- sessions: written as `opencode export` JSON and loaded with `opencode import` (cli/cmd/import.ts),
  which upserts by id, so deterministic ids keep re-runs idempotent.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from ..model import Bundle, Plan
from .pi import _read_json, _upsert_block

DEFAULT_TARGET = "~/.config/opencode"
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")

# opencode tool id -> Claude name, so migrated hook matchers ("Bash", "Edit|Write") keep working.
OC_TOOLS_AS_CLAUDE = {"bash": "Bash", "read": "Read", "edit": "Edit", "write": "Write", "grep": "Grep", "glob": "Glob",
                      "list": "LS", "webfetch": "WebFetch", "websearch": "WebSearch", "todowrite": "TodoWrite",
                      "task": "Task", "skill": "Skill"}
PROVIDER_FOR_SOURCE = {"claude-code": "anthropic", "codex": "openai"}

OC_NOTES = """## opencode mechanics (added by agent-migrate — the instructions below were written for another harness)

- Tools: `bash`, `read`, `edit`, `write`, `grep`, `glob`, `list`, `webfetch`, `todowrite`, `skill`, and `task` for subagents (Claude's Task/Agent). MCP tools are named `<server>_<tool>`.
- Skills load through the `skill` tool. Memory: follow the memory section of this prompt when present."""

BRIDGE_TS = r'''// agent-migrate bridge for opencode — generated file, rewritten on every `agent-migrate ... opencode` run.
// Gives opencode what the source harness had natively:
//  - file memory: <config>/memory/<project-slug>/MEMORY.md (+ _global) added to the system prompt
//  - hooks: Claude-style shell hooks (JSON on stdin, exit 2 = block) on opencode's plugin events
// Data lives in <config>/agent-migrate/bridge.json so this file stays generic.
// Only function exports: opencode treats every export of a plugin file as a plugin.
import { spawn, execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

type Hook = { event: string; command: string; matcher?: string | null; env?: Record<string, string> };

const CONFIG_DIR = dirname(dirname(fileURLToPath(import.meta.url)));
const MEMORY_ROOT = join(CONFIG_DIR, "memory");
const CLAUDE_TOOL: Record<string, string> = __TOOLS__;

function loadHooks(): Hook[] {
  try {
    const data = JSON.parse(readFileSync(join(CONFIG_DIR, "agent-migrate", "bridge.json"), "utf8"));
    return Object.values(data.sources ?? {}).flatMap((s: any) => s.hooks ?? []);
  } catch {
    return [];
  }
}

// Claude's project slug; memory dirs migrated from Claude keep that name.
const slug = (p: string) => p.replace(/[^a-zA-Z0-9]/g, "-");

function gitRoot(cwd: string): string | undefined {
  try {
    return execFileSync("git", ["-C", cwd, "rev-parse", "--show-toplevel"], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  } catch {
    return undefined;
  }
}

function memoryDir(cwd: string): string {
  const direct = join(MEMORY_ROOT, slug(cwd));
  if (existsSync(direct)) return direct;
  const root = gitRoot(cwd);
  return root ? join(MEMORY_ROOT, slug(root)) : direct;
}

const readIndex = (dir: string) => {
  const f = join(dir, "MEMORY.md");
  return existsSync(f) ? readFileSync(f, "utf8").trim() : undefined;
};

// Global memory (from Codex) can be tens of KB: inject its summary when it has one, capped.
const GLOBAL_CAP = 12_000;
function globalMemory(): string | undefined {
  const dir = join(MEMORY_ROOT, "_global");
  const f = ["memory_summary.md", "MEMORY.md"].map((n) => join(dir, n)).find(existsSync);
  if (!f) return undefined;
  const text = readFileSync(f, "utf8").trim();
  return text.length > GLOBAL_CAP ? `${text.slice(0, GLOBAL_CAP)}\n… (truncated — read ${f} for the rest)` : text;
}

function memorySection(cwd: string): string {
  const dir = memoryDir(cwd);
  const global = globalMemory();
  return `# Memory

You have a persistent file-based memory at \`${dir}/\`. Write to it with the write tool (create the directory if missing). Each memory is one file holding one fact, with frontmatter (name: kebab-case slug, description: one line used for recall, metadata.type: user | feedback | project | reference), then the fact. For feedback/project add **Why:** and **How to apply:** lines.

After writing a memory file, add a one-line pointer to \`MEMORY.md\` in that directory (\`- [Title](file.md) — hook\`). MEMORY.md is the index shown to you each session: one line per memory, never memory content. Update an existing file rather than duplicate; delete memories that turn out wrong; don't save what the repo or git history already records. Read a memory file only when its index line looks relevant, and verify named files/flags still exist before relying on them.

Current MEMORY.md:
${readIndex(dir) ?? "(empty — no memories yet for this project)"}${global ? `\n\nGlobal memory (read-mostly; full files in ${join(MEMORY_ROOT, "_global")}/):\n${global}` : ""}`;
}

function runHook(h: Hook, payload: object, cwd: string, timeoutMs: number): Promise<{ code: number; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    const child = spawn("sh", ["-c", h.command], { cwd, env: { ...process.env, CLAUDE_PROJECT_DIR: cwd, ...(h.env ?? {}) } });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => child.kill("SIGKILL"), timeoutMs);
    child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => (stderr += d));
    child.on("error", () => {});
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? 1, stdout, stderr });
    });
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(payload));
  });
}

function claudeInput(args: Record<string, unknown> = {}): Record<string, unknown> {
  const { filePath, oldString, newString, ...rest } = args;
  return { ...rest, ...(filePath !== undefined && { file_path: filePath }), ...(oldString !== undefined && { old_string: oldString }), ...(newString !== undefined && { new_string: newString }) };
}

function matches(h: Hook, name: string): boolean {
  if (!h.matcher || h.matcher === "*") return true;
  try {
    return new RegExp(`^(?:${h.matcher})$`).test(name);
  } catch {
    return false; // an invalid matcher never fires; it must not break every tool call
  }
}

export const AgentMigrateBridge = async ({ directory }: { directory: string }) => {
  const hooks = loadHooks();
  const on = (e: string) => hooks.filter((h) => h.event === e);
  let startContext = "";
  // Subagent (child) sessions go idle after every task; Claude runs Stop for the main agent only.
  const children = new Set<string>();

  return {
    event: async ({ event }: { event: { type: string; properties?: any } }) => {
      if (event.type === "session.created" && event.properties?.info?.parentID) children.add(event.properties.info.id);
      if (event.type === "session.created" && !event.properties?.info?.parentID) {
        const outs = await Promise.all(on("session_start").map((h) => runHook(h, { hook_event_name: "SessionStart", source: "startup", cwd: directory }, directory, 10_000)));
        // Claude feeds SessionStart stdout (or JSON additionalContext) to the model as context.
        startContext = outs
          .filter((o) => o.code === 0 && o.stdout.trim())
          .map((o) => {
            try {
              return JSON.parse(o.stdout).hookSpecificOutput?.additionalContext ?? o.stdout;
            } catch {
              return o.stdout;
            }
          })
          .join("\n\n")
          .trim();
      }
      if (event.type === "session.idle" && !children.has(event.properties?.sessionID)) {
        for (const h of on("stop")) void runHook(h, { hook_event_name: "Stop", cwd: directory }, directory, 30_000);
      }
    },

    "experimental.chat.system.transform": async (_input: unknown, output: { system: string[] }) => {
      if (existsSync(MEMORY_ROOT)) output.system.push(memorySection(directory));
      if (startContext) output.system.push(startContext);
    },

    // Throwing here blocks the tool call; the message goes back to the model.
    "tool.execute.before": async (input: { tool: string }, output: { args: any }) => {
      const name = CLAUDE_TOOL[input.tool] ?? input.tool;
      for (const h of on("pre_tool").filter((h) => matches(h, name))) {
        const r = await runHook(h, { hook_event_name: "PreToolUse", tool_name: name, tool_input: claudeInput(output.args), cwd: directory }, directory, 60_000);
        let denied = r.code === 2;
        try {
          const j = JSON.parse(r.stdout);
          denied ||= j.decision === "block" || j.hookSpecificOutput?.permissionDecision === "deny";
        } catch {}
        if (denied) throw new Error(r.stderr.trim() || `Blocked by a migrated pre-tool hook.`);
      }
    },

    "tool.execute.after": async (input: { tool: string; args: any }) => {
      const name = CLAUDE_TOOL[input.tool] ?? input.tool;
      for (const h of on("post_tool").filter((h) => matches(h, name))) {
        void runHook(h, { hook_event_name: "PostToolUse", tool_name: name, tool_input: claudeInput(input.args), cwd: directory }, directory, 60_000);
      }
    },

    dispose: async () => {
      await Promise.all(on("session_end").map((h) => runHook(h, { hook_event_name: "SessionEnd", cwd: directory }, directory, 5_000)));
    },
  };
};
'''.replace("__TOOLS__", json.dumps(OC_TOOLS_AS_CLAUDE))


def _write_private(path: Path, data: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(path, 0o600)


def _visible_skill_names(home: Path, target: Path) -> dict[str, Path]:
    """Skills opencode already discovers on its own (skill/index.ts)."""
    out = {}
    for root in (home / ".claude" / "skills", home / ".agents" / "skills", target / "skill", target / "skills"):
        for child in sorted(root.iterdir()) if root.is_dir() else []:
            if (child / "SKILL.md").is_file():
                out.setdefault(child.name, child.resolve())
    return out


def _env_refs(value, dropped: list):
    """Claude/Codex `${VAR}` -> opencode `{env:VAR}` (opencode has no `:-default`)."""
    if isinstance(value, dict):
        return {k: _env_refs(v, dropped) for k, v in value.items()}
    if isinstance(value, list):
        return [_env_refs(v, dropped) for v in value]
    if not isinstance(value, str):
        return value

    def sub(m):
        if m.group(2) is not None:
            dropped.append(m.group(1))
        return "{env:%s}" % m.group(1)

    return re.sub(r"\$\{(\w+)(:-[^}]*)?\}", sub, value)


def _mcp_entry(cfg: dict, dropped: list) -> dict:
    cfg = _env_refs(cfg, dropped)
    if "url" in cfg:
        out = {"type": "remote", "url": cfg["url"], "enabled": True}
        if cfg.get("headers"):
            out["headers"] = cfg["headers"]
        return out
    out = {"type": "local", "command": [cfg.get("command", ""), *cfg.get("args", [])], "enabled": True}
    if cfg.get("env"):
        out["environment"] = cfg["env"]
    if cfg.get("cwd"):
        out["cwd"] = cfg["cwd"]
    return out


def _wildcards(rx: str) -> list[str] | None:
    """Invert the readers' guard regexes into opencode bash wildcards; None if it isn't one of those shapes.

    Readers emit `^lit(\\s.*)?$` (Claude `X:*`), `^a.*b$` (Claude `*`), and
    `^tok\\s+(?:a|b)(\\s|$)` (Codex prefix rules). opencode's `X *` also matches bare `X`.
    """
    if rx == r"^[\s\S]*$":  # Claude's bare `Bash` rule: every command
        return ["*"]
    m = re.fullmatch(r"\^(.*?)(\(\\s\.\*\)\?\$|\(\\s\|\$\)|\$)", rx, re.S)
    if not m:
        return None
    body, end = m.groups()
    tail = "" if end == "$" else " *"
    pieces = re.split(r"\(\?:((?:\\.|[^()\\])*)\)", body)  # odd indexes are (?:a|b) groups
    options = [re.split(r"(?<!\\)\|", x) if i % 2 else [x] for i, x in enumerate(pieces)]
    out = []
    for combo in itertools.product(*options):
        s = "".join(combo).replace(r"\s+", " ").replace(".*", "\0")
        if re.search(r"(?<!\\)[.()\[\]{}|+?^$*]", s):
            return None
        # ponytail: a literal * or ? in a rule becomes a wildcard here (slightly broader); fine for guards.
        out.append(re.sub(r"\\(.)", r"\1", s).replace("\0", "*") + tail)
    return out


def _b62(seed: str, n: int) -> str:
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    h = hashlib.sha256(seed.encode()).digest()
    return "".join(chars[b % 62] for b in h[:n])


def _oc_id(prefix: str, ts_ms: int, counter: int, seed: str, descending=False) -> str:
    """opencode id layout (src/id/id.ts), but with a seeded tail so re-imports hit the same rows."""
    now = ts_ms * 0x1000 + counter
    if descending:
        now = ~now & (2**48 - 1)
    return f"{prefix}_{(now & (2**48 - 1)).to_bytes(6, 'big').hex()}{_b62(seed, 14)}"


def _export(s, source: str) -> dict:
    """A reader Session as `opencode export` JSON (shape copied from a real export)."""
    provider = PROVIDER_FOR_SOURCE.get(source, source)
    if s.started:
        started = int(datetime.fromisoformat(s.started.replace("Z", "+00:00")).timestamp() * 1000)
    else:  # some sources leave it blank: the first message is the next best start time
        started = s.messages[0].ts_ms if s.messages else 0
    sid = _oc_id("ses", started, 0, s.id, descending=True)
    messages, parent, ts = [], None, started
    for i, m in enumerate(s.messages):
        if m.role == "assistant" and parent is None:
            continue  # opencode assistant turns need a user parent
        ts = max(ts + 1, m.ts_ms)
        mid = _oc_id("msg", ts, i, f"{s.id}:{i}")
        base = {"id": mid, "sessionID": sid, "role": m.role}
        if m.role == "user":
            info = {**base, "time": {"created": ts}, "agent": "build",
                    "model": {"providerID": provider, "modelID": m.model or "unknown"}}
            parent = mid
        else:
            info = {**base, "time": {"created": ts, "completed": ts}, "parentID": parent, "modelID": m.model or "unknown",
                    "providerID": provider, "mode": "build", "agent": "build", "path": {"cwd": s.cwd, "root": "/"},
                    "cost": 0, "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                    "finish": "stop"}
        part = {"id": _oc_id("prt", ts, i, f"{s.id}:{i}:p"), "sessionID": sid, "messageID": mid, "type": "text", "text": m.text}
        messages.append({"info": info, "parts": [part]})
    first_user = next((x["parts"][0]["text"] for x in messages if x["info"]["role"] == "user"), "")
    title = s.title or (first_user.strip().splitlines() or ["Migrated chat"])[0][:80]
    info = {"id": sid, "slug": f"migrated-{_b62(s.id, 8).lower()}", "projectID": "global", "directory": s.cwd,
            "title": title, "version": "agent-migrate", "time": {"created": started, "updated": ts}}
    return {"info": info, "messages": messages}


def plan(b: Bundle, target: Path, home: Path, parts: set[str]) -> Plan:
    p = Plan(gaps=list(b.gaps))
    cfg_path = target / "opencode.json"
    cfg = _read_json(cfg_path)
    cfg_ok = cfg is not None
    if not cfg_ok:
        cfg = {}
        p.gaps.append("opencode.json has comments or is invalid JSON: MCP servers, guards and skill switches were not merged; add them by hand")
    cfg_before = json.dumps(cfg, sort_keys=True)
    perm = cfg.get("permission", {})
    perm = {"*": perm} if isinstance(perm, str) else perm
    added_rules = []

    def perm_map(key):  # a bare "allow" becomes {"*": "allow"} so specific rules can follow it
        cur = perm.get(key, {})
        perm[key] = {"*": cur} if isinstance(cur, str) else cur
        return perm[key]

    # --- instructions: one owned block per source, so claude + codex runs can coexist.
    if "instructions" in parts and b.instructions:
        agents = target / "AGENTS.md"

        def write_instructions():
            agents.parent.mkdir(parents=True, exist_ok=True)
            if agents.is_symlink():  # a link to another harness's file: keep it, start our own
                agents.rename(agents.with_name(f"AGENTS.md.bak-{STAMP}"))
            text = agents.read_text() if agents.is_file() else ""
            if text and "agent-migrate:" not in text:
                shutil.copy2(agents, agents.with_name(f"AGENTS.md.bak-{STAMP}"))
            text = _upsert_block(text, "opencode-notes", OC_NOTES)
            agents.write_text(_upsert_block(text, b.source, f"# Instructions migrated from {b.source}\n\n{b.instructions}"))

        note = " (current AGENTS.md is a symlink; it will be backed up)" if agents.is_symlink() else ""
        p.add("instructions", "write", f"AGENTS.md ← {b.source} instructions ({len(b.instructions)} chars){note}", write_instructions)

    # --- skills: link what opencode can't already see; "off" becomes a skill permission deny.
    if "skills" in parts:
        visible = _visible_skill_names(home, target)
        for s in b.skills:
            if s.name in visible:
                same = visible[s.name] == s.path.resolve()
                p.add("skills", "skip", f"{s.name}: already visible to opencode" + ("" if same else " (different copy wins)"))
            else:
                link = target / "skills" / s.name
                p.add("skills", "link", f"{s.name} → {s.path}",
                      lambda link=link, src=s.path: (link.parent.mkdir(parents=True, exist_ok=True), link.symlink_to(src)))
                visible[s.name] = s.path.resolve()
            if not s.enabled and cfg_ok and perm_map("skill").get(s.name) != "deny":
                perm_map("skill")[s.name] = "deny"
                added_rules.append(s.name)
                p.add("skills", "write", f"{s.name}: disabled (was off in {b.source})")

    # --- prompts: rewritten copies, not links (see module doc on frontmatter).
    if "prompts" in parts:
        for pr in b.prompts:
            dst = target / "commands" / f"{pr.name}.md"
            if dst.exists() or dst.is_symlink():
                p.add("prompts", "skip", f"/{pr.name}: already exists")
                continue

            def write_prompt(dst=dst, src=pr.path):
                text = src.read_text()
                fm = re.match(r"---\n(.*?)\n---\n?", text, re.S)
                desc = re.search(r"^description:\s*(.+)$", fm.group(1), re.M) if fm else None
                body = text[fm.end():] if fm else text
                head = "---\ndescription: %s\n---\n" % json.dumps(desc.group(1).strip().strip("\"'")) if desc else ""
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(head + body)

            p.add("prompts", "write", f"/{pr.name} ← {pr.path}", write_prompt)

    # --- mcp: into opencode.json. Values may be secrets: 0600, never printed.
    if "mcp" in parts and b.mcp and cfg_ok:
        servers = cfg.setdefault("mcp", {})
        for s in b.mcp:
            if s.name in servers:
                p.add("mcp", "skip", f"{s.name}: already configured")
                continue
            dropped = []
            servers[s.name] = _mcp_entry(s.config, dropped)
            if dropped:
                p.gaps.append(f"mcp {s.name}: opencode has no ${{VAR:-default}}; set {', '.join(dropped)} in your env")
            secret = " (holds secrets → 0600 file)" if s.config.get("env") or s.config.get("headers") else ""
            p.add("mcp", "write", f"{s.name}: {servers[s.name]['type']}{secret}")

    # --- guards: native bash permission rules, appended after the user's so they win.
    if "guards" in parts and cfg_ok:
        for g in b.guards:
            pats = _wildcards(g.pattern)
            if pats is None:
                p.gaps.append(f"guard ({g.origin}): regex has no opencode wildcard form; add a permission.bash rule by hand")
                continue
            bash = perm_map("bash")
            for pat in pats:
                if pat in bash:
                    p.add("guards", "skip", f"{pat}: already {bash[pat]}")
                else:
                    bash[pat] = g.action
                    added_rules.append(pat)
                    p.add("guards", "write", f"{g.action:4} bash '{pat}' ({g.origin})")

    # --- memory: copy, never overwrite a file opencode already has.
    if "memory" in parts:
        for m in b.memory:
            dst = target / "memory" / m.project
            files = [f for f in m.path.rglob("*") if f.is_file()]
            new = [f for f in files if not (dst / f.relative_to(m.path)).exists()]
            if not new:
                p.add("memory", "skip", f"{m.project}: up to date")
                continue

            def copy(m=m, dst=dst, new=new):
                for f in new:
                    out = dst / f.relative_to(m.path)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, out)

            p.add("memory", "copy", f"{m.project}: {len(new)} files", copy)

    # --- hooks (+ memory runtime): data in bridge.json per source, runtime in the plugin.
    if ("memory" in parts and b.memory) or ("hooks" in parts and b.hooks):
        hooks = []
        for h in (b.hooks if "hooks" in parts else []):
            try:
                known = not h.matcher or h.matcher == "*" or any(re.fullmatch(h.matcher, t) for t in OC_TOOLS_AS_CLAUDE.values())
            except re.error:
                p.gaps.append(f"hook {h.event} [{h.matcher}] ({h.origin}): matcher is not a valid regex; skipped")
                continue
            if not known:
                p.gaps.append(f"hook {h.event} [{h.matcher}] ({h.origin}): opencode has no such tool; skipped")
                continue
            hooks.append({"event": h.event, "command": h.command, "matcher": h.matcher, "env": h.env})
            # Never the command text: it can carry tokens, and plans are printed.
            p.add("hooks", "write", f"{h.event}{f' [{h.matcher}]' if h.matcher else ''} ({h.origin})")
        bridge_path = target / "agent-migrate" / "bridge.json"

        def write_bridge(hooks=hooks):
            try:
                data = json.loads(bridge_path.read_text())
            except (OSError, ValueError):
                data = {}
            data.setdefault("sources", {})[b.source] = {"hooks": hooks}
            bridge_path.parent.mkdir(parents=True, exist_ok=True)
            bridge_path.write_text(json.dumps(data, indent=2))
            ext = target / "plugins" / "agent-migrate-bridge.ts"
            ext.parent.mkdir(parents=True, exist_ok=True)
            ext.write_text(BRIDGE_TS)

        p.add("hooks", "write", "plugins/agent-migrate-bridge.ts + agent-migrate/bridge.json", write_bridge)

    # --- sessions: export JSON kept under agent-migrate/sessions; `opencode import` loads it.
    if "sessions" in parts:
        sess_dir = target / "agent-migrate" / "sessions"
        have = {f.stem for f in sess_dir.glob("*.json")} if sess_dir.is_dir() else set()
        todo, bad = [], 0
        for s in b.sessions():
            if s.id in have:
                continue
            try:  # one malformed chat must not sink the rest: convert now, count what fails
                _export(s, b.source)
                todo.append(s.id)
            except Exception:
                bad += 1
        if bad:
            p.gaps.append(f"{bad} chats have data opencode's export format can't hold (bad timestamps?); skipped")
        # Import writes opencode's own database, which follows XDG_DATA_HOME, not the target dir:
        # only do it for the real config dir, so a --target test run never touches real chats.
        real = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "opencode"
        auto = target.expanduser().resolve() == real.resolve() and shutil.which("opencode")

        def write_sessions(todo=set(todo)):
            failed = []
            for s in b.sessions():
                if s.id not in todo:
                    continue
                out = sess_dir / f"{s.id}.json"
                try:
                    _write_private(out, json.dumps(_export(s, b.source)))  # chats can hold pasted secrets
                except Exception:
                    failed.append(s.id)
                    continue
                if auto:
                    cwd = s.cwd if os.path.isdir(s.cwd) else str(home)
                    r = subprocess.run(["opencode", "import", str(out)], cwd=cwd, capture_output=True, text=True)
                    if r.returncode:
                        out.unlink()  # not imported: let the next run retry it
                        failed.append(s.id)
            if failed:  # the rest are in; report the stragglers (a re-run retries them)
                raise RuntimeError(f"{len(failed)} chats not migrated, e.g. {failed[0]}")

        if todo:
            p.add("sessions", "write", f"{len(todo)} chats (text + short tool-call lines)" + (" → opencode import" if auto else ""),
                  write_sessions)
            if not auto:
                p.gaps.append(f"chats saved as JSON in {sess_dir}; load each with `opencode import <file>` "
                              "(run it from the chat's project dir)")
        if have:
            p.add("sessions", "skip", f"{len(have)} chats already migrated")

    if added_rules:
        cfg["permission"] = perm
    if cfg_ok and json.dumps(cfg, sort_keys=True) != cfg_before:
        def write_cfg():
            if cfg_path.is_file() and not any(re.fullmatch(r"opencode\.json\.bak-\d{8}-\d{6}", f.name) for f in target.iterdir()):
                shutil.copy2(cfg_path, target / f"opencode.json.bak-{STAMP}")
            cfg.setdefault("$schema", "https://opencode.ai/config.json")
            _write_private(cfg_path, json.dumps(cfg, indent=2) + "\n")

        p.add("settings", "write", "opencode.json (mcp, permission; mode 0600)", write_cfg)
    return p
