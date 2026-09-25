"""pi writer: Bundle -> Plan of file changes under pi's agent dir (default ~/.pi/agent).

Idempotent by design: every step skips what already exists, owned blocks/files are replaced
in place, and nothing the user wrote is deleted (backups get a timestamp suffix).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from ..model import Bundle, Plan

DEFAULT_TARGET = "~/.pi/agent"
ASSET = Path(__file__).resolve().parent.parent / "assets" / "pi-bridge.ts"
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")

# Tools pi has, by the Claude names hook matchers use. A matcher hitting none of these
# (e.g. "TodoWrite") can never fire in pi, so it's reported instead of silently kept.
PI_TOOLS_AS_CLAUDE = ["Bash", "Read", "Edit", "Write", "Grep", "Glob", "LS"]

# Where the source model came from; pi needs an api/provider pair on assistant messages.
API_FOR_SOURCE = {"claude-code": ("anthropic-messages", "anthropic"), "codex": ("openai-responses", "openai")}

PI_NOTES = """## Pi mechanics (added by agent-migrate — the instructions below were written for another harness)

- Tools: `bash`, `read`, `edit`, `write` (Claude's Bash/Read/Edit/Write, Codex's shell/apply_patch). There is no subagent, Task, or Workflow tool: to delegate, run `pi -p --no-session --model <provider/model> "<self-contained task>"` via bash.
- Skills: `/skill:<name>`. MCP: the `mcp` tool (pi-mcp-adapter); small servers are also direct tools.
- Chat title: `/name <title>`. Memory: follow the `<memory>` section of this prompt when present."""


def _block(key: str, body: str) -> str:
    return f"<!-- agent-migrate:{key}:start -->\n{body.strip()}\n<!-- agent-migrate:{key}:end -->"


def _upsert_block(text: str, key: str, body: str) -> str:
    # Source text can already hold our markers (e.g. CLAUDE.md symlinked to a migrated
    # AGENTS.md). Left as-is, the non-greedy match stops at the inner end marker and every
    # run nests one level deeper, so neutralise them before embedding.
    body = body.replace("<!-- agent-migrate:", "<!-- agent-migrate(source):")
    new = _block(key, body)
    rx = re.compile(rf"<!-- agent-migrate:{re.escape(key)}:start -->.*?<!-- agent-migrate:{re.escape(key)}:end -->", re.S)
    return rx.sub(lambda _: new, text) if rx.search(text) else (text.rstrip() + "\n\n" + new + "\n").lstrip()


def _write_private(path: Path, data: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(path, 0o600)


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _read_json(path: Path) -> dict | None:
    """{} when the file is missing, None when it exists but isn't valid JSON (or isn't an
    object). Callers must treat None as "don't touch": rewriting it would drop the user's
    other keys."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _backup_once(path: Path):
    """Keep the user's original next to it before our first change; later runs don't pile up copies."""
    if path.is_file() and not list(path.parent.glob(f"{path.name}.bak-*")):
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{STAMP}"))


def _visible_skill_names(home: Path, target: Path) -> dict[str, Path]:
    """Skills pi already discovers on its own: ~/.agents/skills and <target>/skills."""
    out = {}
    for root in (home / ".agents" / "skills", target / "skills"):
        for child in sorted(root.iterdir()) if root.is_dir() else []:
            if (child / "SKILL.md").is_file():
                out.setdefault(child.name, child.resolve())
    return out


def _sessions_dir(cwd: str) -> str:
    return "--" + re.sub(r"[/\\:]", "-", cwd.lstrip("/\\")) + "--"


def _session_lines(s, api, provider) -> list[dict]:
    zero = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}
    lines = [{"type": "session", "version": 3, "id": s.id, "timestamp": s.started, "cwd": s.cwd}]
    parent = None
    for m in s.messages:
        iso = datetime.fromtimestamp(m.ts_ms / 1000).astimezone().isoformat()
        if m.role == "user":
            msg = {"role": "user", "content": m.text, "timestamp": m.ts_ms}
        else:
            msg = {"role": "assistant", "content": [{"type": "text", "text": m.text}], "api": api, "provider": provider,
                   "model": m.model or "unknown", "usage": zero, "stopReason": "stop", "timestamp": m.ts_ms}
        eid = uuid.uuid4().hex[:8]
        lines.append({"type": "message", "id": eid, "parentId": parent, "timestamp": iso, "message": msg})
        parent = eid
    if s.title:
        lines.append({"type": "session_info", "id": uuid.uuid4().hex[:8], "parentId": parent,
                      "timestamp": lines[-1]["timestamp"], "name": s.title})
    return lines


def plan(b: Bundle, target: Path, home: Path, parts: set[str]) -> Plan:
    p = Plan(gaps=list(b.gaps))
    settings_path = target / "settings.json"
    settings = _read_json(settings_path)
    settings_ok = settings is not None
    if not settings_ok:  # broken JSON: never rewrite it (it would drop the user's other keys)
        p.gaps.append(f"{settings_path} is not valid JSON: skill switches and pi-mcp-adapter install skipped; fix it and re-run")
        settings = {}
    settings_before = json.dumps(settings, sort_keys=True)

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
            text = _upsert_block(text, "pi-notes", PI_NOTES)
            agents.write_text(_upsert_block(text, b.source, f"# Instructions migrated from {b.source}\n\n{b.instructions}"))

        note = " (current AGENTS.md is a symlink; it will be backed up)" if agents.is_symlink() else ""
        p.add("instructions", "write", f"AGENTS.md ← {b.source} instructions ({len(b.instructions)} chars){note}", write_instructions)

    # --- skills: link what pi can't already see; carry "off" switches as exact excludes.
    if "skills" in parts:
        visible = _visible_skill_names(home, target)
        excludes = settings.setdefault("skills", [])
        for s in b.skills:
            if s.name in visible:
                same = visible[s.name] == s.path.resolve()
                p.add("skills", "skip", f"{s.name}: already visible to pi" + ("" if same else " (different copy wins)"))
            else:
                link = target / "skills" / s.name
                p.add("skills", "link", f"{s.name} → {s.path}",
                      lambda link=link, src=s.path: (link.parent.mkdir(parents=True, exist_ok=True), link.symlink_to(src)))
                visible[s.name] = s.path.resolve()
            if not s.enabled and f"-skills/{s.name}" not in excludes:
                excludes.append(f"-skills/{s.name}")
                p.add("skills", "write", f"{s.name}: disabled (was off in {b.source})")

    if "prompts" in parts:
        for pr in b.prompts:
            dst = target / "prompts" / f"{pr.name}.md"
            if dst.exists() or dst.is_symlink():
                p.add("prompts", "skip", f"/{pr.name}: already exists")
            else:
                p.add("prompts", "link", f"/{pr.name} → {pr.path}",
                      lambda dst=dst, src=pr.path: (dst.parent.mkdir(parents=True, exist_ok=True), dst.symlink_to(src)))

    # --- mcp: merge into pi-mcp-adapter's file. Values may be secrets: 0600, never printed.
    if "mcp" in parts and b.mcp:
        mcp_path = target / "mcp.json"
        mcp = _read_json(mcp_path)
        if mcp is None:
            p.gaps.append(f"{mcp_path} is not valid JSON: MCP servers not added; fix it and re-run")
            mcp = {}
            mcp_writable = False
        else:
            mcp_writable = True
        servers = mcp.setdefault("mcpServers", {})
        added = []
        for s in b.mcp:
            if s.name in servers:
                p.add("mcp", "skip", f"{s.name}: already configured")
            elif mcp_writable:
                # pi-mcp-adapter picks the transport itself (streamable HTTP, falls back to SSE);
                # it has no `type`, only an explicit `httpTransport` override.
                cfg = {k: v for k, v in s.config.items() if k != "type"}
                if s.config.get("type") == "sse":
                    cfg["httpTransport"] = "sse"
                servers[s.name] = {**cfg, "directTools": True}
                added.append(s.name)
                secret = " (holds secrets → 0600 file)" if s.config.get("env") or s.config.get("headers") else ""
                p.add("mcp", "write", f"{s.name}: {'http' if 'url' in s.config else 'stdio'}{secret}")
        if added:
            p.add("mcp", "write", f"mcp.json: +{len(added)} servers",
                  lambda: (_backup_once(mcp_path), _write_private(mcp_path, json.dumps(mcp, indent=2))))
        installed = any(str(x).startswith("npm:pi-mcp-adapter") for x in settings.get("packages", []))
        if not installed and settings_ok:
            # Installing hits npm and the network; only do it for the real pi dir, not a trial run.
            if target.expanduser().resolve() != Path(DEFAULT_TARGET).expanduser().resolve():
                p.gaps.append(f"pi-mcp-adapter not installed in this target: run `PI_CODING_AGENT_DIR={target} pi install npm:pi-mcp-adapter`")
            elif shutil.which("pi"):
                p.add("mcp", "run", "pi install npm:pi-mcp-adapter",
                      lambda: subprocess.run(["pi", "install", "npm:pi-mcp-adapter"], check=True,
                                             env={**os.environ, "PI_CODING_AGENT_DIR": str(target)}))
            else:
                p.gaps.append("pi not on PATH: run `pi install npm:pi-mcp-adapter` yourself, or MCP servers won't load")

    # --- memory: copy, never overwrite a file pi already has.
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

    # --- hooks + guards: data into bridge.json (per source), runtime in the bridge extension.
    want_bridge = ("memory" in parts and b.memory) or ("hooks" in parts and b.hooks) or ("guards" in parts and b.guards)
    if want_bridge:
        hooks = []
        for h in (b.hooks if "hooks" in parts else []):
            try:
                fires = not h.matcher or h.matcher == "*" or any(re.fullmatch(h.matcher, t) for t in PI_TOOLS_AS_CLAUDE)
            except re.error:
                p.gaps.append(f"hook {h.event} ({h.origin}): matcher {h.matcher!r} is not a valid regex; skipped")
                continue
            if not fires:
                p.gaps.append(f"hook {h.event} [{h.matcher}] ({h.origin}): pi has no such tool; skipped")
                continue
            hooks.append({"event": h.event, "command": h.command, "matcher": h.matcher, "env": h.env})
            # Only event/matcher/origin on screen: hook commands can carry tokens.
            p.add("hooks", "write", f"{h.event}{f' [{h.matcher}]' if h.matcher else ''} ({h.origin})")
        guards = [{"pattern": g.pattern, "action": g.action, "origin": g.origin} for g in (b.guards if "guards" in parts else [])]
        for g in guards:
            p.add("guards", "write", f"{g['action']:4} {g['origin']}")
        bridge_path = target / "agent-migrate" / "bridge.json"

        def write_bridge(hooks=hooks, guards=guards):
            data = _load(bridge_path, {"sources": {}})
            data.setdefault("sources", {})[b.source] = {"hooks": hooks, "guards": guards}
            bridge_path.parent.mkdir(parents=True, exist_ok=True)
            bridge_path.write_text(json.dumps(data, indent=2))
            ext = target / "extensions" / "agent-migrate-bridge.ts"
            ext.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ASSET, ext)

        p.add("hooks", "write", "extensions/agent-migrate-bridge.ts + agent-migrate/bridge.json", write_bridge)

    # --- sessions: text-only transcripts, skip ids pi already has.
    if "sessions" in parts:
        sess_root = target / "sessions"
        have = {f.stem.split("_", 1)[-1] for f in sess_root.glob("*/*.jsonl")} if sess_root.is_dir() else set()
        todo = [s.id for s in b.sessions() if s.id not in have]
        api, provider = API_FOR_SOURCE.get(b.source, ("pi-messages", b.source))

        def write_sessions(todo=set(todo)):
            for s in b.sessions():
                if s.id not in todo:
                    continue
                out = sess_root / _sessions_dir(s.cwd) / f"{re.sub(r'[:.]', '-', s.started)}_{s.id}.jsonl"
                # Chats can hold pasted secrets: owner-only, like opencode's exports.
                _write_private(out, "".join(json.dumps(x) + "\n" for x in _session_lines(s, api, provider)))

        if todo:
            p.add("sessions", "write", f"{len(todo)} chats (text + short tool-call lines)", write_sessions)
        if have:
            p.add("sessions", "skip", f"{len(have)} chats already in pi")

    if settings_ok and json.dumps(settings, sort_keys=True) != settings_before:
        def write_settings():
            current = _read_json(settings_path)  # re-read: `pi install` may have added packages
            if current is None:
                raise RuntimeError(f"{settings_path} became invalid JSON; not touching it")
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            _backup_once(settings_path)
            settings_path.write_text(json.dumps({**current, "skills": settings["skills"]}, indent=2))

        p.add("settings", "write", "settings.json (skill switches)", write_settings)
    return p
