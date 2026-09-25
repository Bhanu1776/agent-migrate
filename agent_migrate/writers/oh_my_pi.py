"""oh-my-pi (omp, npm @oh-my-pi/pi-coding-agent) writer: Bundle -> Plan under ~/.omp/agent.

omp is a fork of pi, but it already reads most of another harness's setup by itself
(src/discovery/claude.ts, claude-plugins.ts, codex.ts, agents.ts): ~/.claude/{skills,commands,
CLAUDE.md}, ~/.claude.json MCP, every installed Claude plugin, ~/.codex/{skills,prompts,config.toml},
~/.agents/skills. So this writer mostly *skips*, and adds only what omp can't see:
  - instructions: <agent>/AGENTS.md. omp keeps ONE user-level context file and its own AGENTS.md
    shadows ~/.claude/CLAUDE.md, so both sources get owned blocks there (same as pi).
  - skill "off" switches: omp ignores Claude's skillOverrides -> `skill:<name>` in config.yml
    `disabledExtensions` (the key omp's own /extensions UI writes).
  - MCP servers omp doesn't read natively (project-scoped Claude ones) -> <agent>/mcp.json, 0600.
  - memory/hooks/guards: pi's file layout + bridge extension, patched for omp's extension API.
  - chats: omp imports Claude/Codex sessions itself (`omp --from-claude` / `--from-codex`).
Idempotent like pi: skips what exists, replaces owned blocks, backs files up once.
"""
from __future__ import annotations

import json
import re
import shutil
import tomllib
from pathlib import Path

from ..model import Bundle, Plan
from . import pi

try:  # stdlib-only tool: use a YAML parser to vet config.yml only when one happens to be installed
    import yaml
except ImportError:
    yaml = None

DEFAULT_TARGET = "~/.omp/agent"

OMP_NOTES = """## omp mechanics (added by agent-migrate — the instructions below were written for another harness)

- Tools: `bash`, `read`, `edit`, `write`, `grep`, `glob`; subagents via the `task` tool; questions via `ask`.
- Skills: `/skill:<name>`. MCP servers are native tools (`/mcp` to manage). Chat title: `/rename <title>`.
- Memory: follow the memory section of this prompt when present."""

# pi's bridge, adapted to omp's ExtensionAPI (src/extensibility/extensions/types.ts):
# before_agent_start gets `systemPrompt: string[]` and returns a replacement (no
# systemPromptOptions.sections), there is no `agent_settled` (use agent_end unless it will
# continue), session_start carries no reason, and the tool is `glob` (no find/ls).
_BRIDGE_PATCHES = [
    ('from "@earendil-works/pi-coding-agent"', 'from "@oh-my-pi/pi-coding-agent"'),
    ("// agent-migrate bridge for pi —", "// agent-migrate bridge for omp (oh-my-pi) —"),
    ("source: event.reason,", 'source: "startup",'),
    ("""    const sections = event.systemPromptOptions.sections;
    if (existsSync(MEMORY_ROOT)) sections.memory = memorySection(ctx.cwd);
    if (startContext) sections.session_start_context = startContext;""",
     """    const extra = [existsSync(MEMORY_ROOT) ? memorySection(ctx.cwd) : "", startContext].filter(Boolean);
    if (extra.length) return { systemPrompt: [...event.systemPrompt, ...extra] };"""),
    ('pi.on("agent_settled", async (_e, ctx) => {', 'pi.on("agent_end", async (_e, ctx) => {\n    if (_e.willContinue) return;'),
    ('find: "Glob", ls: "LS"', 'glob: "Glob"'),
]


def bridge_source() -> str:
    text = pi.ASSET.read_text()
    for old, new in _BRIDGE_PATCHES:
        if old not in text:  # pi's asset changed: fail loud rather than ship a half-patched bridge
            raise RuntimeError(f"pi-bridge.ts no longer contains {old[:50]!r}; update oh_my_pi._BRIDGE_PATCHES")
        text = text.replace(old, new)
    return text


def _under(path: Path, roots) -> bool:
    p = path.resolve()
    return any(p.is_relative_to(r.resolve()) for r in roots if r.exists())


def _native_skill_roots(home: Path, target: Path):
    return [target / "skills", home / ".claude" / "skills", home / ".claude" / "plugins",
            home / ".codex" / "skills", home / ".agents" / "skills", home / ".agent" / "skills"]


def _native_mcp_names(home: Path) -> set[str]:
    # claude.ts: first non-empty of ~/.claude.json, ~/.claude/mcp.json (top-level mcpServers only).
    names = set(pi._load(home / ".claude.json", {}).get("mcpServers") or {}) or \
        set(pi._load(home / ".claude" / "mcp.json", {}).get("mcpServers") or {})
    try:
        names |= set(tomllib.loads((home / ".codex" / "config.toml").read_text()).get("mcp_servers", {}))
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return names


def add_disabled(text: str, ids: list[str]) -> str | None:
    """Add ids to config.yml's top-level `disabledExtensions` list without a YAML parser.
    Returns None when the existing value isn't a block list (or empty) we can safely extend."""
    ids = [i for i in ids if not re.search(rf"^\s*-\s*['\"]?{re.escape(i)}['\"]?\s*$", text, re.M)]
    if not ids:
        return text
    m = re.search(r"^disabledExtensions:[ \t]*(\S.*)?\n?", text, re.M)
    if not m:
        return text.rstrip("\n") + ("\n" if text.strip() else "") + "disabledExtensions:\n" + "".join(f"  - {json.dumps(i)}\n" for i in ids)
    value = (m.group(1) or "").split("#")[0].strip()
    if value not in ("", "[]"):
        return None  # flow list or scalar: leave it to the user
    # The items' indent comes from the first real line after the key: comments and blank lines
    # in between can sit at any column and must not decide it.
    nxt = next((l for l in text[m.end():].splitlines() if l.strip() and not l.lstrip().startswith("#")), None)
    if value == "[]" or nxt is None or not nxt[0].isspace() and not nxt.startswith("-"):
        indent = "  "  # empty list / null value: we start the list
    elif nxt.lstrip().startswith("-"):
        indent = nxt[:len(nxt) - len(nxt.lstrip())]
    else:
        return None  # an indented non-item (a mapping?) — not a list we understand
    items = "".join(f"{indent}- {json.dumps(i)}\n" for i in ids)
    return text[:m.start()] + "disabledExtensions:\n" + items + text[m.end():]


def _yaml_ok(text: str) -> bool:
    """False only when a YAML parser is installed and rejects the text."""
    if yaml is None or not text.strip():
        return True
    try:
        yaml.safe_load(text)
        return True
    except yaml.YAMLError:
        return False


def plan(b: Bundle, target: Path, home: Path, parts: set[str]) -> Plan:
    # Memory copy + hooks/guards bridge.json are byte-for-byte pi's layout: reuse pi's plan for
    # those parts and swap only the extension source it installs.
    p = pi.plan(b, target, home, parts & {"memory", "hooks", "guards"})
    p.gaps = [g.replace("pi has no such tool", "omp has no such tool") for g in p.gaps]
    # ponytail: pi keeps hooks matching "LS" (omp has no ls tool); they just never fire.
    for a in p.actions:
        if a.part == "hooks" and "agent-migrate-bridge.ts" in a.desc and a.apply:
            def install(pi_apply=a.apply):
                pi_apply()
                (target / "extensions" / "agent-migrate-bridge.ts").write_text(bridge_source())
            a.apply = install

    if "instructions" in parts and b.instructions:
        agents = target / "AGENTS.md"

        def write_instructions():
            agents.parent.mkdir(parents=True, exist_ok=True)
            if agents.is_symlink():
                agents.rename(agents.with_name(f"AGENTS.md.bak-{pi.STAMP}"))
            text = agents.read_text() if agents.is_file() else ""
            if text and "agent-migrate:" not in text:
                shutil.copy2(agents, agents.with_name(f"AGENTS.md.bak-{pi.STAMP}"))
            text = pi._upsert_block(text, "omp-notes", OMP_NOTES)
            agents.write_text(pi._upsert_block(text, b.source, f"# Instructions migrated from {b.source}\n\n{b.instructions}"))

        p.add("instructions", "write", f"AGENTS.md ← {b.source} instructions ({len(b.instructions)} chars; "
              "omp reads this instead of ~/.claude/CLAUDE.md)", write_instructions)

    if "skills" in parts:
        roots = _native_skill_roots(home, target)
        seen = {c.name for r in roots if r.name == "skills" and r.is_dir() for c in r.iterdir() if (c / "SKILL.md").is_file()}
        off = []
        for s in b.skills:
            if _under(s.path, roots) or s.name in seen:
                p.add("skills", "skip", f"{s.name}: omp already loads it")
            else:
                link = target / "skills" / s.name
                p.add("skills", "link", f"{s.name} → {s.path}",
                      lambda link=link, src=s.path: (link.parent.mkdir(parents=True, exist_ok=True), link.symlink_to(src)))
                seen.add(s.name)
            if not s.enabled:
                off.append(f"skill:{s.name}")
        cfg = target / "config.yml"
        text = cfg.read_text() if cfg.is_file() else ""
        new = add_disabled(text, off)
        if new is not None and new != text and not (_yaml_ok(text) and _yaml_ok(new)):
            new = None  # never write into a config.yml omp can't parse, or one our edit would break
        if new is None:
            p.gaps.append(f"config.yml: couldn't safely extend disabledExtensions (flow list or unparseable YAML): "
                          f"add {', '.join(off)} to it by hand")
        elif new != text:
            if not cfg.exists() and (target / "settings.json").exists():
                p.gaps.append("omp hasn't converted its legacy settings.json yet: start omp once, then re-run skills")
            else:
                def write_cfg(new=new):
                    target.mkdir(parents=True, exist_ok=True)
                    if cfg.is_file() and not list(target.glob("config.yml.bak-agent-migrate-*")):
                        shutil.copy2(cfg, target / f"config.yml.bak-agent-migrate-{pi.STAMP}")
                    cfg.write_text(add_disabled(cfg.read_text() if cfg.is_file() else "", off))
                n = len(new.splitlines()) - len(text.splitlines())
                p.add("settings", "write", f"config.yml disabledExtensions: +{n} skills off (were off in {b.source})", write_cfg)

    if "prompts" in parts:
        native = [home / ".claude" / "commands", home / ".claude" / "plugins", home / ".codex" / "prompts",
                  home / ".codex" / "commands", target / "commands", target / "prompts"]
        for pr in b.prompts:
            dst = target / "commands" / f"{pr.name}.md"
            if _under(pr.path, native) or dst.exists() or dst.is_symlink() or (target / "prompts" / f"{pr.name}.md").exists():
                p.add("prompts", "skip", f"/{pr.name}: omp already loads it")
            else:
                p.add("prompts", "link", f"/{pr.name} → {pr.path}",
                      lambda dst=dst, src=pr.path: (dst.parent.mkdir(parents=True, exist_ok=True), dst.symlink_to(src)))

    mcp_path = target / "mcp.json"
    mcp = pi._read_json(mcp_path) if "mcp" in parts and b.mcp else None
    if "mcp" in parts and b.mcp and mcp is None:  # rewriting it would drop the user's other servers
        p.gaps.append("mcp.json is not valid JSON: MCP servers were not merged; fix it and re-run")
    if mcp is not None:
        servers = mcp.setdefault("mcpServers", {})
        native, added = _native_mcp_names(home), 0
        for s in b.mcp:
            if s.name in native or s.name in servers:
                p.add("mcp", "skip", f"{s.name}: omp already loads it")
                continue
            servers[s.name] = s.config
            added += 1
            secret = " (holds secrets → 0600 file)" if s.config.get("env") or s.config.get("headers") else ""
            p.add("mcp", "write", f"{s.name}: {s.config.get('type') or ('http' if 'url' in s.config else 'stdio')}{secret}")
        if added:
            def write_mcp():
                if mcp_path.is_file() and not list(target.glob("mcp.json.bak-*")):
                    pi._write_private(mcp_path.with_name(f"mcp.json.bak-{pi.STAMP}"), mcp_path.read_text())
                if pi._read_json(mcp_path) is None:
                    raise RuntimeError("mcp.json stopped parsing since the plan; left untouched")
                pi._write_private(mcp_path, json.dumps(mcp, indent=2))
            p.add("mcp", "write", f"mcp.json: +{added} servers", write_mcp)

    if "sessions" in parts:
        flag = {"claude-code": "--from-claude", "codex": "--from-codex"}.get(b.source)
        if flag:
            p.add("sessions", "skip", f"omp imports {b.source} chats itself, full fidelity: run `omp {flag}`")
        else:
            p.gaps.append(f"chats from {b.source}: omp has no importer for them; not converted")
    return p
