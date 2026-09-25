// agent-migrate bridge for pi — generated file, rewritten on every `agent-migrate ... pi` run.
// Gives pi the runtime behaviour other harnesses had and pi lacks natively:
//  - file memory: <agentDir>/memory/<project-slug>/MEMORY.md (+ _global) injected each turn
//  - hooks: Claude-style shell hooks (JSON on stdin, exit 2 = block) on pi's events
//  - guards: regexes on bash commands that need a human "yes" (ask) or are refused (deny)
// Data lives in <agentDir>/agent-migrate/bridge.json so this file stays generic.
import { isToolCallEventType, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn, execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

type Hook = { event: string; command: string; matcher?: string | null; env?: Record<string, string> };
type Guard = { pattern: string; action: "ask" | "deny"; origin?: string };

const AGENT_DIR = dirname(dirname(fileURLToPath(import.meta.url)));
const MEMORY_ROOT = join(AGENT_DIR, "memory");

type CompiledGuard = Guard & { rx: RegExp };

// A rule that doesn't compile must not take the whole extension down; it's dropped and
// reported once at session start instead.
function loadBridge(): { hooks: Hook[]; guards: CompiledGuard[]; broken: string[] } {
  try {
    const data = JSON.parse(readFileSync(join(AGENT_DIR, "agent-migrate", "bridge.json"), "utf8"));
    const sources = Object.values(data.sources ?? {}) as Array<{ hooks?: Hook[]; guards?: Guard[] }>;
    const guards: CompiledGuard[] = [];
    const broken: string[] = [];
    for (const g of sources.flatMap((s) => s.guards ?? [])) {
      try {
        guards.push({ ...g, rx: new RegExp(g.pattern) });
      } catch {
        broken.push(g.origin ?? g.pattern);
      }
    }
    return { hooks: sources.flatMap((s) => s.hooks ?? []), guards, broken };
  } catch {
    return { hooks: [], guards: [], broken: [] };
  }
}

// Mirror of agent_migrate/guards.py command_segments(): keep them identical.
function commandSegments(cmd: string, depth = 0): string[] {
  const out = [cmd];
  if (depth > 3) return out;
  for (const m of cmd.matchAll(/\$\(([^)]*)\)|`([^`]*)`/g)) out.push(...commandSegments(m[1] ?? m[2] ?? "", depth + 1));
  for (const part of cmd.split(/\n|;|&&|\|\||\||&/)) {
    let s = part.trim();
    for (;;) {
      let t = s.replace(/^[({'"]+/, "").replace(/[)}'"]+$/, "").trim();
      t = t.replace(/^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+/, "").replace(/^(?:sudo(?:\s+-[ugCDhpRrTt]\s+\S+|\s+-\S+)*|nice(?:\s+-n\s+\S+|\s+-\S+)*|env(?:\s+-[uSC]\s+\S+|\s+-\S+)*|exec(?:\s+-a\s+\S+|\s+-\S+)*|xargs(?:\s+-[IdEeLnPs]\s+\S+|\s+-\S+)*|(?:command|builtin|nohup|time|eval)(?:\s+-\S+)*)(?:\s+|$)/, "").replace(/^(?:ba|z|da|k)?sh(?:\s+--?[A-Za-z][\w-]*)*?\s+-[A-Za-z]*c\s+['"]?/, "").trim();
      if (t === s) break;
      s = t;
    }
    if (s) out.push(s);
  }
  return out;
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

// Global memory (from Codex) can be tens of KB; inject its short summary when it has one and
// cap it, so a big memory store doesn't tax every turn. The model can read the rest on demand.
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
  return `You have a persistent file-based memory at \`${dir}/\`. Write to it with the write tool (create the directory if missing). Each memory is one file holding one fact, with frontmatter (name: kebab-case slug, description: one line used for recall, metadata.type: user | feedback | project | reference), then the fact. For feedback/project add **Why:** and **How to apply:** lines.

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

// pi tool name -> Claude tool name, so migrated matchers ("Bash", "Edit|Write") keep working.
const CLAUDE_TOOL: Record<string, string> = { bash: "Bash", read: "Read", edit: "Edit", write: "Write", grep: "Grep", find: "Glob", ls: "LS" };

function claudeInput(input: Record<string, unknown>): Record<string, unknown> {
  const { path, oldText, newText, ...rest } = input as Record<string, unknown>;
  return { ...rest, ...(path !== undefined && { file_path: path }), ...(oldText !== undefined && { old_string: oldText }), ...(newText !== undefined && { new_string: newText }) };
}

function matches(h: Hook, claudeName: string): boolean {
  if (!h.matcher || h.matcher === "*") return true;
  try {
    return new RegExp(`^(?:${h.matcher})$`).test(claudeName);
  } catch {
    return false; // an invalid matcher never fires; it must not break every tool call
  }
}

export default function (pi: ExtensionAPI) {
  const { hooks, guards, broken } = loadBridge();
  const on = (e: string) => hooks.filter((h) => h.event === e);
  let startContext = "";

  pi.on("session_start", async (event, ctx) => {
    if (broken.length && ctx.hasUI) ctx.ui.notify(`agent-migrate: ${broken.length} guard rule(s) have an invalid regex and are OFF: ${broken.join(", ")}`, "warning");
    // Claude runs SessionStart/Stop for the main agent only; headless runs (pi -p delegations)
    // would otherwise re-fire notification-style hooks for every subagent.
    if (!ctx.hasUI) return;
    const outs = await Promise.all(
      on("session_start").map((h) => runHook(h, { hook_event_name: "SessionStart", source: event.reason, cwd: ctx.cwd }, ctx.cwd, 10_000)),
    );
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
  });

  pi.on("before_agent_start", async (event, ctx) => {
    const sections = event.systemPromptOptions.sections;
    if (existsSync(MEMORY_ROOT)) sections.memory = memorySection(ctx.cwd);
    if (startContext) sections.session_start_context = startContext;
  });

  pi.on("tool_call", async (event, ctx) => {
    if (isToolCallEventType("bash", event)) {
      const cmd = event.input.command;
      for (const g of guards) {
        if (!commandSegments(cmd).some((seg) => g.rx.test(seg))) continue;
        const what = g.origin ? ` (${g.origin})` : "";
        if (g.action === "deny") return { block: true, reason: `Blocked by migrated rule${what}.` };
        // Nobody to ask (print mode, subagents) means fail closed.
        if (!ctx.hasUI) return { block: true, reason: `Needs human approval${what}.` };
        if (!(await ctx.ui.confirm("⚠ Guarded command", `${cmd}\n\nRule${what}. Run it?`))) return { block: true, reason: `User denied${what}.` };
      }
    }
    const name = CLAUDE_TOOL[event.toolName] ?? event.toolName;
    for (const h of on("pre_tool").filter((h) => matches(h, name))) {
      const r = await runHook(h, { hook_event_name: "PreToolUse", tool_name: name, tool_input: claudeInput(event.input as Record<string, unknown>), cwd: ctx.cwd }, ctx.cwd, 60_000);
      let denied = r.code === 2;
      try {
        const j = JSON.parse(r.stdout);
        denied ||= j.decision === "block" || j.hookSpecificOutput?.permissionDecision === "deny";
      } catch {}
      if (denied) return { block: true, reason: r.stderr.trim() || `Blocked by a migrated pre-tool hook.` };
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    const name = CLAUDE_TOOL[event.toolName] ?? event.toolName;
    for (const h of on("post_tool").filter((h) => matches(h, name))) {
      void runHook(h, { hook_event_name: "PostToolUse", tool_name: name, tool_input: claudeInput(event.input), cwd: ctx.cwd }, ctx.cwd, 60_000);
    }
  });

  pi.on("agent_settled", async (_e, ctx) => {
    if (!ctx.hasUI) return;
    for (const h of on("stop")) void runHook(h, { hook_event_name: "Stop", cwd: ctx.cwd }, ctx.cwd, 30_000);
  });

  pi.on("session_shutdown", async (_e, ctx) => {
    await Promise.all(on("session_end").map((h) => runHook(h, { hook_event_name: "SessionEnd", cwd: ctx.cwd }, ctx.cwd, 5_000)));
  });
}
