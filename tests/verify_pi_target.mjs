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

await r.emit({ type: "session_start", reason: "startup" });
const res = await r.emitBeforeAgentStart("hi", undefined, { cwd, sections: {} });
const sections = res?.systemPromptOptions?.sections ?? {};
const bridge = JSON.parse((await import("node:fs")).readFileSync(join(agentDir, "agent-migrate/bridge.json"), "utf8"));
check(/persistent file-based memory/.test(sections.memory ?? ""), `memory section injected (${(sections.memory ?? "").length} chars)`);
const startHooks = Object.values(bridge.sources).flatMap((s) => s.hooks).filter((h) => h.event === "session_start").length;
check(!startHooks || (sections.session_start_context ?? "").length > 0, `SessionStart output injected (${startHooks} hooks, ${(sections.session_start_context ?? "").length} chars)`);
for (const g of Object.values(bridge.sources).flatMap((s) => s.guards)) {
  // Build a command the rule must catch: literal pieces of the regex, joined.
  const sample = g.pattern.replace(/^\^|\$$/g, "").replace(/\(\\s\.\*\)\?/g, " x").replace(/\.\*/g, "x").replace(/\\(.)/g, "$1");
  const out = await r.emitToolCall({ type: "tool_call", toolName: "bash", toolCallId: "t", input: { command: sample } });
  check(out?.block === true, `guard blocks \`${sample}\` (${g.action}, no UI)`);
}
const safe = await r.emitToolCall({ type: "tool_call", toolName: "bash", toolCallId: "t", input: { command: "ls" } });
check(!safe?.block, "plain `ls` is not blocked");

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
