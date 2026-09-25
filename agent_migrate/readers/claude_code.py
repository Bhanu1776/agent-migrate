"""Claude Code reader: ~/.claude + ~/.claude.json -> Bundle. Read-only."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ..model import Bundle, Guard, Hook, McpServer, MemoryDir, Message, Prompt, Session, Skill

HOOK_EVENTS = {
    "SessionStart": "session_start",
    "PreToolUse": "pre_tool",
    "PostToolUse": "post_tool",
    "Stop": "stop",
    "SessionEnd": "session_end",
}

# macOS per-process temp dirs (tool sandboxes) are noise. /tmp worktrees are real work: kept.
_JUNK_PROJECT = re.compile(r"^-(private-)?var-folders-")


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _frontmatter_name(skill_md: Path) -> str | None:
    try:
        text = skill_md.read_text(errors="replace")
    except OSError:
        return None
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if m:
        n = re.search(r"^name:\s*['\"]?([^'\"\n]+)", m.group(1), re.M)
        if n:
            return n.group(1).strip()
    return None


def _skills_under(root: Path) -> list[Skill]:
    # Resolve each top-level entry ourselves: rglob doesn't descend into symlinked dirs, and
    # most user skills are symlinks into a shared ~/.agents/skills.
    out = []
    for child in sorted(root.iterdir()) if root.is_dir() else []:
        if child.name.startswith("."):  # .cursor/ copies etc. inside plugins
            continue
        top = child.resolve()
        mds = [top / "SKILL.md"] if (top / "SKILL.md").is_file() else sorted(top.rglob("SKILL.md")) if top.is_dir() else []
        for md in mds:
            if any(p.startswith(".") for p in md.relative_to(top).parts[:-1]):
                continue
            out.append(Skill(_frontmatter_name(md) or md.parent.name, md.parent))
    return out


def _enabled_plugins(claude: Path, settings: dict) -> dict[str, Path]:
    """plugin id -> install path, for plugins switched on in settings.json."""
    registry = _load_json(claude / "plugins" / "installed_plugins.json", {}).get("plugins", {})
    out = {}
    for pid, on in settings.get("enabledPlugins", {}).items():
        installs = registry.get(pid) or []
        if on and installs and Path(installs[0]["installPath"]).is_dir():
            out[pid] = Path(installs[0]["installPath"])
    return out


def _bash_rule_to_regex(rule: str) -> str | None:
    """Claude permission rule `Bash(git push:*)` / `Bash(*prod*)` -> full-command regex."""
    m = re.fullmatch(r"Bash\((.*)\)", rule.strip())
    if not m:
        return None
    body = m.group(1)
    if body.endswith(":*"):  # legacy prefix syntax
        return "^" + re.escape(body[:-2]) + r"(\s.*)?$"
    return "^" + ".*".join(re.escape(p) for p in body.split("*")) + "$"


def _hooks_from(block: dict, origin: str, env: dict, gaps: list[str]) -> list[Hook]:
    out = []
    for event, groups in (block or {}).items():
        canon = HOOK_EVENTS.get(event)
        for g in groups:
            for h in g.get("hooks", []):
                if h.get("type") != "command":
                    gaps.append(f"hook {event} ({origin}): type '{h.get('type')}' not supported")
                    continue
                if not canon:
                    gaps.append(f"hook {event} ({origin}): no equivalent event — `{h['command'][:60]}`")
                    continue
                cmd = h["command"]
                for k, v in env.items():
                    cmd = cmd.replace("${%s}" % k, v)
                # SessionStart matchers filter the start *source*, not tools; drop them.
                matcher = g.get("matcher") if canon in ("pre_tool", "post_tool") else None
                out.append(Hook(canon, cmd, matcher or None, dict(env), origin))
    return out


def _iso_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _session(path: Path) -> Session | None:
    records = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    cwd = next((r["cwd"] for r in records if r.get("cwd")), None)
    started = next((r["timestamp"] for r in records if r.get("timestamp")), None)
    if not cwd or not started:
        return None
    title = None
    msgs: list[Message] = []
    for r in records:
        if r.get("type") == "custom-title" and r.get("customTitle"):
            title = r["customTitle"]
        if r.get("type") not in ("user", "assistant") or r.get("isSidechain") or r.get("isMeta"):
            continue
        content = (r.get("message") or {}).get("content")
        ts = _iso_ms(r.get("timestamp") or started)
        if r["type"] == "user":
            if isinstance(content, str):
                text = content
            else:  # tool_result-only records are tool plumbing, not something the user said
                text = "\n\n".join(b.get("text", "") for b in content or [] if b.get("type") == "text")
            role, model = "user", None
        else:
            lines = []
            for b in content or []:
                if b.get("type") == "text" and b.get("text"):
                    lines.append(b["text"])
                elif b.get("type") == "tool_use":
                    lines.append(f"→ {b.get('name')}: {json.dumps(b.get('input', {}))[:200]}")
            text, role, model = "\n".join(lines), "assistant", r["message"].get("model")
        if not text.strip():
            continue
        if msgs and msgs[-1].role == role:  # Claude splits one reply over many records
            msgs[-1].text += "\n\n" + text
            msgs[-1].model = msgs[-1].model or model
        else:
            msgs.append(Message(role, text, ts, model))
    if not any(m.role == "user" for m in msgs):
        return None
    return Session(path.stem, cwd, started, msgs, title)


def read(home: Path) -> Bundle:
    claude = home / ".claude"
    settings = _load_json(claude / "settings.json", {})
    b = Bundle(source="claude-code")

    md = claude / "CLAUDE.md"
    b.instructions = md.read_text() if md.is_file() else None

    # Skills: user dir first, then enabled plugins. Names Claude switched off stay off.
    off = {k for k, v in settings.get("skillOverrides", {}).items() if v == "off"}
    plugins = _enabled_plugins(claude, settings)
    seen = set()
    for s in _skills_under(claude / "skills") + [s for p in plugins.values() for s in _skills_under(p / "skills")]:
        if s.name in seen:
            continue
        seen.add(s.name)
        s.enabled = s.name not in off
        b.skills.append(s)

    # Plugin commands are namespaced in Claude (/codex:rescue); keep that so /setup etc. don't clash.
    cmd_dirs = [("", claude / "commands")] + [(pid.split("@")[0] + "-", p / "commands") for pid, p in plugins.items()]
    for prefix, cmd_dir in cmd_dirs:
        for f in sorted(cmd_dir.glob("*.md")) if cmd_dir.is_dir() else []:
            b.prompts.append(Prompt(prefix + f.stem, f))

    # MCP: user scope wins; project-scoped servers get promoted to global (noted as a gap).
    cj = _load_json(home / ".claude.json", {})
    servers = dict(cj.get("mcpServers", {}))
    for proj, pdata in cj.get("projects", {}).items():
        for name, cfg in (pdata.get("mcpServers") or {}).items():
            if name not in servers:
                servers[name] = cfg
                b.gaps.append(f"mcp '{name}' was project-scoped ({proj}); it becomes global")
    for name, cfg in servers.items():
        cfg = {k: v for k, v in cfg.items() if k != "type"}
        b.mcp.append(McpServer(name, cfg))

    projects = claude / "projects"
    for d in sorted(projects.glob("*/memory")) if projects.is_dir() else []:
        if not _JUNK_PROJECT.match(d.parent.name) and any(d.glob("*.md")):
            b.memory.append(MemoryDir(d.parent.name, d))

    b.hooks += _hooks_from(settings.get("hooks"), "settings.json", {}, b.gaps)
    for pid, root in plugins.items():
        hj = _load_json(root / "hooks" / "hooks.json", {})
        b.hooks += _hooks_from(hj.get("hooks"), f"plugin:{pid}", {"CLAUDE_PLUGIN_ROOT": str(root)}, b.gaps)

    perms = settings.get("permissions", {})
    rules = [(r, "deny", "permissions.deny") for r in perms.get("deny", [])]
    rules += [(r, "ask", "permissions.ask") for r in perms.get("ask", [])]
    rules += [(r, "ask", "autoMode.soft_deny") for r in settings.get("autoMode", {}).get("soft_deny", [])]
    for rule, action, origin in rules:
        rx = _bash_rule_to_regex(rule)
        if rx:
            b.guards.append(Guard(rx, action, f"{origin}: {rule}"))
        elif not rule.startswith("$"):
            b.gaps.append(f"permission rule '{rule}' ({origin}) is not a Bash rule; not migrated")

    def sessions():
        for f in sorted(projects.glob("*/*.jsonl")) if projects.is_dir() else []:
            if _JUNK_PROJECT.match(f.parent.name):
                continue
            try:
                s = _session(f)
            except Exception:  # one corrupt transcript must not sink the whole run
                continue
            if s:
                yield s

    b.sessions = sessions

    if settings.get("statusLine"):
        b.gaps.append("statusLine script: not migrated (target UIs differ)")
    if (claude / "keybindings.json").is_file():
        b.gaps.append("keybindings.json: not migrated (action names differ per harness)")
    if (claude / "agents").is_dir():
        b.gaps.append("~/.claude/agents (subagents): not migrated")
    if (claude / "output-styles").is_dir():
        b.gaps.append("~/.claude/output-styles: not migrated; paste into instructions if needed")
    b.gaps.append("claude.ai connectors (Slack, Gmail, Notion, ...) live in your Claude account; re-add as MCP servers by hand")
    return b
