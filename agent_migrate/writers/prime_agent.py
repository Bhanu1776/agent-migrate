"""Prime Agent writer (github.com/PrimeIntellect-ai/prime-agent): Bundle -> Plan under ~/.prime/agent.

Prime Agent is a pi fork, so most of the layout matches pi (AGENTS.md, skills/, prompts/,
extensions/*.ts, v3 JSONL sessions). What differs, from its docs + source (v0.9.6):
- the model has ONE tool, `ipython`; shell runs inside Python (`!cmd`, `await bash(...)`), so
  hooks/guards keyed on Claude's Bash tool are matched against shell commands found in the code;
- MCP is native: `mcpServers` in settings.json, run by the kernel; stdio `env` accepts only
  `{"env": "NAME"}` references, never literal values (rlm/mcp.py `_stdio_env`);
- sessions are flat: sessions/<id>.jsonl (session-manager.ts `getSessionFilePath`).
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from ..model import Bundle, Plan
from .pi import API_FOR_SOURCE, STAMP, _load, _session_lines, _upsert_block, _write_private

DEFAULT_TARGET = "~/.prime/agent"

# Built-in integrations own these ids; a user mcpServers entry with the name is ignored (docs/mcp-integrations.md).
RESERVED_MCP = {"linear", "notion"}

NOTES = """## Prime Agent mechanics (added by agent-migrate — the instructions below were written for another harness)

- Your one tool is `ipython`, a persistent Python REPL. Shell: `!cmd` or `await bash("cmd")`. Files: plain Python I/O. There is no Task/Workflow tool: spawn subagents with `rlm.spawn(...)`.
- Skills: `/skill:<name>`; Python-backed skills are importable in the REPL. MCP: `await mcp.list_tools("<server>")`, then `await mcp.call_tool(...)`.
- Memory: follow the `<memory>` section of this prompt when present."""

# Runtime for memory, hooks and guards. Uses only a type import from the host package, so it has
# no runtime dependency on how prime-agent aliases `@earendil-works/pi-coding-agent`.
BRIDGE_TS = r"""// agent-migrate bridge for Prime Agent — generated file, rewritten on every `agent-migrate ... prime-agent` run.
// Data: <agentDir>/agent-migrate/bridge.json. Memory: <agentDir>/memory/<project-slug>/MEMORY.md (+ _global).
// Prime Agent's model has one tool, `ipython`; shell runs inside Python. Hooks and guards written for a
// Bash tool are applied to the shell commands visible in the code: `!cmd` lines and string literals.
// Commands assembled at runtime are not seen — this is best effort, not a sandbox.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn, execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

type Hook = { event: string; command: string; matcher?: string | null; env?: Record<string, string> };
type Guard = { pattern: string; action: "ask" | "deny"; origin?: string };

const AGENT_DIR = dirname(dirname(fileURLToPath(import.meta.url)));
const MEMORY_ROOT = join(AGENT_DIR, "memory");

function loadBridge(): { hooks: Hook[]; guards: Guard[] } {
  try {
    const data = JSON.parse(readFileSync(join(AGENT_DIR, "agent-migrate", "bridge.json"), "utf8"));
    const sources = Object.values(data.sources ?? {}) as Array<{ hooks?: Hook[]; guards?: Guard[] }>;
    return { hooks: sources.flatMap((s) => s.hooks ?? []), guards: sources.flatMap((s) => s.guards ?? []) };
  } catch {
    return { hooks: [], guards: [] };
  }
}

const slug = (p: string) => p.replace(/[^a-zA-Z0-9]/g, "-");

function memoryDir(cwd: string): string {
  const direct = join(MEMORY_ROOT, slug(cwd));
  if (existsSync(direct)) return direct;
  try {
    const root = execFileSync("git", ["-C", cwd, "rev-parse", "--show-toplevel"], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
    return join(MEMORY_ROOT, slug(root));
  } catch {
    return direct;
  }
}

const readFirst = (dir: string, names: string[], cap: number) => {
  const f = names.map((n) => join(dir, n)).find(existsSync);
  if (!f) return undefined;
  const t = readFileSync(f, "utf8").trim();
  return t.length > cap ? `${t.slice(0, cap)}\n… (truncated — read ${f} for the rest)` : t;
};

function memorySection(cwd: string): string {
  const dir = memoryDir(cwd);
  const global = readFirst(join(MEMORY_ROOT, "_global"), ["memory_summary.md", "MEMORY.md"], 12_000);
  return `<memory>
You have a persistent file-based memory at \`${dir}/\`. Write to it from ipython (create the directory if missing). Each memory is one file holding one fact, with frontmatter (name: kebab-case slug, description: one line used for recall, metadata.type: user | feedback | project | reference), then the fact. For feedback/project add **Why:** and **How to apply:** lines.

After writing a memory file, add a one-line pointer to \`MEMORY.md\` in that directory (\`- [Title](file.md) — hook\`). MEMORY.md is the index shown to you each session: one line per memory, never memory content. Update an existing file rather than duplicate; delete memories that turn out wrong. Read a memory file only when its index line looks relevant.

Current MEMORY.md:
${readFirst(dir, ["MEMORY.md"], 25_000) ?? "(empty — no memories yet for this project)"}${global ? `\n\nGlobal memory (read-mostly; full files in ${join(MEMORY_ROOT, "_global")}/):\n${global}` : ""}
</memory>`;
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

// Shell commands visible in a tool call: a `bash` tool's command, or in ipython code the `!cmd`
// lines plus every string literal (covers bash("..."), subprocess.run("...")).
export function shellCommands(toolName: string, input: Record<string, unknown>): string[] {
  if (toolName === "bash") return [String(input.command ?? "")];
  if (toolName !== "ipython") return [];
  const code = String(input.code ?? "");
  const out = code.split("\n").map((l) => l.trim()).filter((l) => l.startsWith("!")).map((l) => l.replace(/^!+\s*/, ""));
  for (const m of code.matchAll(/("{3}|'{3}|"|')((?:\\.|(?!\1)[\s\S])*?)\1/g)) out.push(m[2]);
  return out.filter(Boolean);
}

const matches = (h: Hook, name: string) => !h.matcher || h.matcher === "*" || new RegExp(`^(?:${h.matcher})$`).test(name);

async function blockedByHook(h: Hook, name: string, toolInput: object, cwd: string): Promise<string | undefined> {
  const r = await runHook(h, { hook_event_name: "PreToolUse", tool_name: name, tool_input: toolInput, cwd }, cwd, 60_000);
  let denied = r.code === 2;
  try {
    const j = JSON.parse(r.stdout);
    denied ||= j.decision === "block" || j.hookSpecificOutput?.permissionDecision === "deny";
  } catch {}
  return denied ? r.stderr.trim() || `Blocked by hook: ${h.command}` : undefined;
}

export default function (pi: ExtensionAPI) {
  const { hooks, guards } = loadBridge();
  const on = (e: string) => hooks.filter((h) => h.event === e);
  let startContext = "";

  pi.on("session_start", async (event, ctx) => {
    const outs = await Promise.all(
      on("session_start").map((h) => runHook(h, { hook_event_name: "SessionStart", source: event.reason, cwd: ctx.cwd }, ctx.cwd, 10_000)),
    );
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
  });

  pi.on("before_agent_start", async (event, ctx) => {
    let prompt = event.systemPrompt;
    if (existsSync(MEMORY_ROOT)) prompt += `\n\n${memorySection(ctx.cwd)}`;
    if (startContext) prompt += `\n\n<session_start_context>\n${startContext}\n</session_start_context>`;
    return prompt === event.systemPrompt ? undefined : { systemPrompt: prompt };
  });

  pi.on("tool_call", async (event, ctx) => {
    const input = event.input as Record<string, unknown>;
    const cmds = shellCommands(event.toolName, input);
    for (const g of guards) {
      const rx = new RegExp(g.pattern);
      const hit = cmds.find((c) => rx.test(c));
      if (hit === undefined) continue;
      const what = g.origin ? ` (${g.origin})` : "";
      if (g.action === "deny") return { block: true, reason: `Blocked by migrated rule${what}.` };
      if (!ctx.hasUI) return { block: true, reason: `Needs human approval${what}.` };
      if (!(await ctx.ui.confirm("⚠ Guarded command", `${hit}\n\nRule${what}. Run it?`))) return { block: true, reason: `User denied${what}.` };
    }
    for (const h of on("pre_tool")) {
      // Bash-matching hooks see each shell command as a Claude Bash call; catch-all hooks see the raw call.
      const calls = matches(h, "Bash") && h.matcher && h.matcher !== "*"
        ? cmds.map((c) => ["Bash", { command: c }] as const)
        : matches(h, event.toolName) ? [[event.toolName, input] as const] : [];
      for (const [name, ti] of calls) {
        const reason = await blockedByHook(h, name, ti, ctx.cwd);
        if (reason) return { block: true, reason };
      }
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    const input = event.input as Record<string, unknown>;
    const cmds = shellCommands(event.toolName, input);
    for (const h of on("post_tool")) {
      const calls = matches(h, "Bash") && h.matcher && h.matcher !== "*"
        ? cmds.map((c) => ["Bash", { command: c }] as const)
        : matches(h, event.toolName) ? [[event.toolName, input] as const] : [];
      for (const [name, ti] of calls) void runHook(h, { hook_event_name: "PostToolUse", tool_name: name, tool_input: ti, cwd: ctx.cwd }, ctx.cwd, 60_000);
    }
  });

  pi.on("agent_end", async (_e, ctx) => {
    for (const h of on("stop")) void runHook(h, { hook_event_name: "Stop", cwd: ctx.cwd }, ctx.cwd, 30_000);
  });

  pi.on("session_shutdown", async (_e, ctx) => {
    await Promise.all(on("session_end").map((h) => runHook(h, { hook_event_name: "SessionEnd", cwd: ctx.cwd }, ctx.cwd, 5_000)));
  });
}
"""


def _visible_skills(home: Path, target: Path) -> dict[str, Path]:
    """Skills prime-agent discovers by itself (docs/skills.md), as the path it sees them at."""
    out = {}
    for root in (target / "skills", home / ".agents" / "skills"):
        for child in sorted(root.iterdir()) if root.is_dir() else []:
            if (child / "SKILL.md").is_file():
                out.setdefault(child.name, child)
    return out


def _mcp_entry(name: str, cfg: dict, gaps: list[str]) -> dict | None:
    """Claude-shaped server -> prime-agent settings entry. Never puts a secret value in a gap."""
    if "url" in cfg:
        entry = {"type": "http", "url": cfg["url"]}
        headers = dict(cfg.get("headers") or {})
        # Claude expands `Bearer ${VAR}`; prime-agent's native form for that is bearerTokenEnvVar.
        if m := re.fullmatch(r"Bearer \$\{?(\w+)\}?", str(headers.get("Authorization", ""))):
            del headers["Authorization"]
            entry["bearerTokenEnvVar"] = m.group(1)
        if headers:
            entry["headers"] = headers
            if any("${" in str(v) for v in headers.values()):
                gaps.append(f"mcp '{name}': headers with ${{VAR}} are sent literally by prime-agent; put the value in or use bearerTokenEnvVar")
        return entry
    if "command" not in cfg:
        gaps.append(f"mcp '{name}': neither url nor command; not migrated")
        return None
    entry = {"type": "stdio", "command": cfg["command"], "args": [str(a) for a in cfg.get("args", [])]}
    if cfg.get("cwd"):
        entry["cwd"] = cfg["cwd"]
    if cfg.get("env"):
        env, need = {}, []
        for k, v in cfg["env"].items():
            m = re.fullmatch(r"\$\{?(\w+)\}?", str(v))
            env[k] = {"env": m.group(1) if m else k}
            if not m:
                need.append(k)
        entry["env"] = env
        if need:
            gaps.append(f"mcp '{name}': prime-agent takes no literal env values; export {', '.join(need)} in the shell that starts prime-agent")
    return entry


def plan(b: Bundle, target: Path, home: Path, parts: set[str]) -> Plan:
    p = Plan(gaps=list(b.gaps))
    settings_path = target / "settings.json"
    settings = _load(settings_path, {})
    before = json.dumps(settings, sort_keys=True)

    if "instructions" in parts and b.instructions:
        agents = target / "AGENTS.md"

        def write_instructions():
            agents.parent.mkdir(parents=True, exist_ok=True)
            if agents.is_symlink():  # a link to another harness's file: keep it, start our own
                agents.rename(agents.with_name(f"AGENTS.md.bak-{STAMP}"))
            text = agents.read_text() if agents.is_file() else ""
            if text and "agent-migrate:" not in text:
                shutil.copy2(agents, agents.with_name(f"AGENTS.md.bak-{STAMP}"))
            text = _upsert_block(text, "prime-agent-notes", NOTES)
            agents.write_text(_upsert_block(text, b.source, f"# Instructions migrated from {b.source}\n\n{b.instructions}"))

        p.add("instructions", "write", f"AGENTS.md ← {b.source} instructions ({len(b.instructions)} chars)", write_instructions)

    if "skills" in parts:
        visible = _visible_skills(home, target)
        excludes = settings.setdefault("skills", [])
        for s in b.skills:
            if s.name in visible:
                same = visible[s.name].resolve() == s.path.resolve()
                p.add("skills", "skip", f"{s.name}: already visible to prime-agent" + ("" if same else " (different copy wins)"))
            else:
                link = target / "skills" / s.name
                p.add("skills", "link", f"{s.name} → {s.path}",
                      lambda link=link, src=s.path: (link.parent.mkdir(parents=True, exist_ok=True), link.symlink_to(src)))
                visible[s.name] = link
            # Exact force-exclude, matched against the skill dir (relative to the agent dir, or absolute).
            where = visible[s.name]
            rule = "-" + (str(where.relative_to(target)) if where.is_relative_to(target) else str(where))
            if not s.enabled and rule not in excludes:
                excludes.append(rule)
                p.add("skills", "write", f"{s.name}: disabled (was off in {b.source})")

    if "prompts" in parts:
        for pr in b.prompts:
            dst = target / "prompts" / f"{pr.name}.md"
            if dst.exists() or dst.is_symlink():
                p.add("prompts", "skip", f"/{pr.name}: already exists")
            else:
                p.add("prompts", "link", f"/{pr.name} → {pr.path}",
                      lambda dst=dst, src=pr.path: (dst.parent.mkdir(parents=True, exist_ok=True), dst.symlink_to(src)))

    if "mcp" in parts:
        servers = settings.setdefault("mcpServers", {})
        for s in b.mcp:
            if s.name in servers:
                p.add("mcp", "skip", f"{s.name}: already configured")
            elif s.name in RESERVED_MCP:
                p.gaps.append(f"mcp '{s.name}': name is reserved by prime-agent's built-in service; connect it in /plugins")
            elif entry := _mcp_entry(s.name, s.config, p.gaps):
                servers[s.name] = entry
                secret = " (holds secrets → 0600 file)" if s.config.get("headers") else ""
                p.add("mcp", "write", f"{s.name}: {entry['type']}{secret}")
        if not servers:
            del settings["mcpServers"]

    if "memory" in parts:
        for m in b.memory:
            dst = target / "memory" / m.project
            new = [f for f in m.path.rglob("*") if f.is_file() and not (dst / f.relative_to(m.path)).exists()]
            if not new:
                p.add("memory", "skip", f"{m.project}: up to date")
                continue

            def copy(m=m, dst=dst, new=new):
                for f in new:
                    out = dst / f.relative_to(m.path)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, out)

            p.add("memory", "copy", f"{m.project}: {len(new)} files", copy)

    if ("memory" in parts and b.memory) or ("hooks" in parts and b.hooks) or ("guards" in parts and b.guards):
        hooks = []
        for h in (b.hooks if "hooks" in parts else []):
            if h.matcher and h.matcher != "*" and not re.fullmatch(h.matcher, "Bash"):
                p.gaps.append(f"hook {h.event} [{h.matcher}] ({h.origin}): prime-agent has no such tool (only ipython); skipped")
                continue
            hooks.append({"event": h.event, "command": h.command, "matcher": h.matcher, "env": h.env})
            p.add("hooks", "write", f"{h.event}{f' [{h.matcher}]' if h.matcher else ''} ({h.origin}): {h.command[:70]}")
        guards = [{"pattern": g.pattern, "action": g.action, "origin": g.origin} for g in (b.guards if "guards" in parts else [])]
        for g in guards:
            p.add("guards", "write", f"{g['action']:4} {g['origin']}")
        if guards or any(h["matcher"] for h in hooks):
            p.gaps.append("guards/Bash hooks see shell commands written in ipython code (`!cmd`, string literals) only; commands built at runtime are not checked")
        bridge_path = target / "agent-migrate" / "bridge.json"

        def write_bridge(hooks=hooks, guards=guards):
            data = _load(bridge_path, {"sources": {}})
            data.setdefault("sources", {})[b.source] = {"hooks": hooks, "guards": guards}
            bridge_path.parent.mkdir(parents=True, exist_ok=True)
            bridge_path.write_text(json.dumps(data, indent=2))
            ext = target / "extensions" / "agent-migrate-bridge.ts"
            ext.parent.mkdir(parents=True, exist_ok=True)
            ext.write_text(BRIDGE_TS)

        p.add("hooks", "write", "extensions/agent-migrate-bridge.ts + agent-migrate/bridge.json", write_bridge)

    if "sessions" in parts:
        root = target / "sessions"
        have = {f.stem for f in root.glob("*.jsonl")} if root.is_dir() else set()
        todo = {s.id for s in b.sessions() if s.id not in have}
        api, provider = API_FOR_SOURCE.get(b.source, ("pi-messages", b.source))

        def write_sessions(todo=todo):
            root.mkdir(parents=True, exist_ok=True)
            for s in b.sessions():
                if s.id in todo:
                    (root / f"{s.id}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in _session_lines(s, api, provider)))

        if todo:
            p.add("sessions", "write", f"{len(todo)} chats (text + short tool-call lines)", write_sessions)
        if have:
            p.add("sessions", "skip", f"{len(have)} chats already in prime-agent")

    if json.dumps(settings, sort_keys=True) != before:
        def write_settings():
            if settings_path.exists() and not list(target.glob("settings.json.bak-*")):
                shutil.copy2(settings_path, target / f"settings.json.bak-{STAMP}")
            cur = _load(settings_path, {})
            for key in ("skills", "mcpServers"):
                if key in settings:
                    cur[key] = settings[key]
            # prime-agent itself writes settings.json 0600; ours may hold MCP headers, so match it.
            _write_private(settings_path, json.dumps(cur, indent=2))

        p.add("settings", "write", "settings.json (skill switches, MCP servers; 0600)", write_settings)
    return p
