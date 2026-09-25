// End-to-end check of a migrated pi agent dir, using pi's own SDK (no model calls).
// Usage: node tests/verify_pi_target.mjs <pi-agent-dir> [cwd]
// Proves the things a user would notice on Monday: skills visible/hidden, bridge loaded,
// memory + SessionStart context reach the prompt, guards block, converted chats open.
import { existsSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const agentDir = process.argv[2];
const cwd = process.argv[3] ?? process.cwd();
process.env.PI_CODING_AGENT_DIR = agentDir;
const release = join(homedir(), ".pi/agent/install/releases");
const pkg = join(release, readdirSync(release).sort().at(-1), "node_modules/@earendil-works/pi-coding-agent/dist/index.js");
const { createAgentSession, SessionManager } = await import(pkg);

let failed = 0;
const check = (ok, what) => {
  console.log(`${ok ? "✓" : "✗"} ${what}`);
  if (!ok) failed++;
};

const { session } = await createAgentSession({ cwd, agentDir, sessionManager: SessionManager.inMemory(cwd) });
await session.bindExtensions({});
const r = session._extensionRunner;

const skills = session.resourceLoader.getSkills().skills.map((s) => s.name);
const settings = JSON.parse((await import("node:fs")).readFileSync(join(agentDir, "settings.json"), "utf8"));
const off = (settings.skills ?? []).filter((s) => s.startsWith("-skills/")).map((s) => s.slice(8));
check(skills.length > 0, `${skills.length} skills load`);
check(off.every((n) => !skills.includes(n)), `${off.length} disabled skills stay hidden`);
const linked = existsSync(join(agentDir, "skills")) ? readdirSync(join(agentDir, "skills")) : [];
check(linked.every((n) => skills.includes(n) || off.includes(n)), `${linked.length} linked skills are visible`);

const exts = session.resourceLoader.getExtensions().extensions.map((e) => e.path);
check(exts.some((p) => p.endsWith("agent-migrate-bridge.ts")), "bridge extension loads");
check(session.resourceLoader.getExtensions().errors?.length === 0 || !session.resourceLoader.getExtensions().errors, "no extension load errors");

const bridge = JSON.parse((await import("node:fs")).readFileSync(join(agentDir, "agent-migrate/bridge.json"), "utf8"));
const allGuards = Object.values(bridge.sources).flatMap((s) => s.guards);
const startHooks = Object.values(bridge.sources).flatMap((s) => s.hooks).filter((h) => h.event === "session_start").length;
const prompt = async () => (await r.emitBeforeAgentStart("hi", undefined, { cwd, sections: {} }))?.systemPromptOptions?.sections ?? {};

// Headless first (like `pi -p` subagents): guards fail closed, SessionStart hooks stay quiet.
await r.emit({ type: "session_start", reason: "startup" });
let sections = await prompt();
check(/persistent file-based memory/.test(sections.memory ?? ""), `memory section injected (${(sections.memory ?? "").length} chars)`);
check(!sections.session_start_context, "headless run skips SessionStart hooks");
// Build a command each rule must catch, then hide it behind a compound/wrapper form too.
const sampleFor = (g) => g.pattern.replace(/^\^|\$$/g, "").replace(/\(\\s\.\*\)\?/g, " x").replace(/\[\\s\\S\]\*/g, "x").replace(/\.\*/g, "x").replace(/\\(.)/g, "$1") || "x";
for (const g of allGuards) {
  for (const cmd of [sampleFor(g), `cd /tmp && env A=1 ${sampleFor(g)}`]) {
    const out = await r.emitToolCall({ type: "tool_call", toolName: "bash", toolCallId: "t", input: { command: cmd } });
    check(out?.block === true, `guard blocks \`${cmd}\` (${g.action}, no UI)`);
  }
}
const safe = await r.emitToolCall({ type: "tool_call", toolName: "bash", toolCallId: "t", input: { command: "ls" } });
check(!safe?.block || allGuards.some((g) => g.pattern === "^[\\s\\S]*$"), "plain `ls` is not blocked");

// Interactive: SessionStart output reaches the prompt; "ask" rules follow the user's answer.
let answer = true;
const ui = new Proxy({ confirm: async () => answer, notify: () => {} }, { get: (t, k) => t[k] ?? (() => undefined) });
r.setUIContext(ui, "tui");
await r.emit({ type: "session_start", reason: "startup" });
sections = await prompt();
check(!startHooks || (sections.session_start_context ?? "").length > 0, `SessionStart output injected with UI (${startHooks} hooks, ${(sections.session_start_context ?? "").length} chars)`);
for (const g of allGuards) {
  answer = true;
  const yes = await r.emitToolCall({ type: "tool_call", toolName: "bash", toolCallId: "t", input: { command: sampleFor(g) } });
  answer = false;
  const no = await r.emitToolCall({ type: "tool_call", toolName: "bash", toolCallId: "t", input: { command: sampleFor(g) } });
  check(g.action === "deny" ? yes?.block && no?.block : !yes?.block && no?.block, `${g.action} rule with UI: ${g.action === "deny" ? "always blocked" : "runs on yes, blocked on no"}`);
}

const root = join(agentDir, "sessions");
const files = existsSync(root) ? readdirSync(root).flatMap((d) => readdirSync(join(root, d)).map((f) => join(root, d, f))) : [];
let bad = 0;
for (const f of files) {
  try {
    const ctx = SessionManager.open(f).buildSessionContext();
    if (!ctx.messages.length) bad++;
  } catch {
    bad++;
  }
}
check(bad === 0, `${files.length} chats open in pi (${bad} failed)`);

console.log(failed ? `\n${failed} check(s) FAILED` : "\nall checks passed");
process.exit(failed ? 1 : 0);
