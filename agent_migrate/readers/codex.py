"""Read a Codex (~/.codex) setup into a neutral Bundle. Read-only: never writes under home.

Format notes (learned from real Codex 0.15x homes, Sep 2026):
- config.toml holds mcp_servers, notify, skills.config (enable/disable by path), plugins, profiles...
- hooks live in ~/.codex/hooks.json (Claude-shaped: {"hooks": {"PreToolUse": [{matcher, hooks:[{command}]}]}});
  config.toml [hooks.state] only stores trust hashes.
- rules/*.rules are Starlark: prefix_rule(pattern=[...], decision="forbidden"|"prompt"|"allow").
- sessions/**/rollout-*.jsonl: one {timestamp, type, payload} per line. Codex injects context
  (AGENTS.md, <environment_context>, <recommended_plugins>, <skill>...) as role=user messages,
  so user text must be filtered. Subagent/guardian threads are Codex-internal and skipped.
"""
from __future__ import annotations

import ast
import json
import re
import shlex
import tomllib
from datetime import datetime
from pathlib import Path

from ..model import Bundle, Guard, Hook, McpServer, MemoryDir, Message, Prompt, Session, Skill

# Codex hook event -> canonical event. Keys are normalised to snake_case first.
_HOOK_EVENTS = {"session_start": "session_start", "pre_tool_use": "pre_tool",
                "post_tool_use": "post_tool", "stop": "stop", "session_end": "session_end"}

# A user message is Codex-injected context if it starts with one of these.
_INJECTED = re.compile(r"^\s*(# AGENTS\.md instructions|<([A-Za-z_][\w-]*)[ >][\s\S]*</\2>\s*$)")

_TOOL_ARG_MAX = 200


def read(home: Path) -> Bundle:
    root = home / ".codex"
    b = Bundle(source="codex")
    if not root.is_dir():
        return b
    cfg = _load_toml(root / "config.toml", b)

    agents = root / "AGENTS.md"
    b.instructions = agents.read_text(encoding="utf-8") if agents.is_file() else None

    b.skills = _skills(root, cfg)
    if (root / "skills" / ".system").is_dir():
        b.gaps.append("skills/.system: Codex built-in system skills skipped (bundled with Codex)")

    if (root / "prompts").is_dir():
        b.prompts = [Prompt(name=p.stem, path=p) for p in sorted((root / "prompts").glob("*.md"))]

    b.mcp = _mcp(cfg.get("mcp_servers", {}), b.gaps)
    _memory(root, b)
    _hooks(root, cfg, b)
    b.guards = _guards(root, b.gaps)
    _other_gaps(root, cfg, b.gaps)

    titles = _titles(root / "session_index.jsonl")
    b.sessions = lambda: _sessions(root / "sessions", titles)
    return b


def _load_toml(path: Path, b: Bundle) -> dict:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        b.gaps.append(f"config.toml unreadable ({e}); MCP/notify/profiles not migrated")
        return {}


def _skills(root: Path, cfg: dict) -> list[Skill]:
    disabled = {str(Path(c["path"]).expanduser().resolve())
                for c in cfg.get("skills", {}).get("config", [])
                if isinstance(c, dict) and "path" in c and c.get("enabled") is False}
    out = []
    sdir = root / "skills"
    if not sdir.is_dir():
        return out
    for d in sorted(sdir.iterdir()):
        md = d / "SKILL.md"
        if d.name.startswith(".") or not md.is_file():  # is_file follows symlinks
            continue
        m = re.search(r"^---\s*\n(.*?)\n---", md.read_text(encoding="utf-8", errors="replace"), re.S)
        nm = re.search(r"^name:\s*['\"]?(.+?)['\"]?\s*$", m.group(1), re.M) if m else None
        out.append(Skill(name=nm.group(1) if nm else d.name, path=d,
                         enabled=str(md.resolve()) not in disabled))
    return out


def _mcp(servers: dict, gaps: list[str]) -> list[McpServer]:
    out = []
    for name, s in servers.items():
        if s.get("enabled") is False:
            gaps.append(f"mcp {name}: disabled in Codex, skipped")
            continue
        if "url" in s:
            headers = dict(s.get("http_headers", {}))
            # env_http_headers / bearer_token_env_var name env vars; keep them as ${VAR} refs
            # (Claude .mcp.json expands those) rather than copying secret values around.
            for h, var in s.get("env_http_headers", {}).items():
                headers[h] = "${%s}" % var
            if s.get("bearer_token_env_var"):
                headers["Authorization"] = "Bearer ${%s}" % s["bearer_token_env_var"]
            conf = {"type": "http", "url": s["url"], "headers": headers}
            known = {"url", "http_headers", "env_http_headers", "bearer_token_env_var"}
        else:
            conf = {"command": s.get("command", ""), "args": list(s.get("args", [])),
                    "env": dict(s.get("env", {}))}
            if s.get("cwd"):
                conf["cwd"] = s["cwd"]
            if s.get("env_vars"):  # env names forwarded from the parent shell
                conf["env"].update({v: "${%s}" % v for v in s["env_vars"] if v not in conf["env"]})
            known = {"command", "args", "env", "cwd", "env_vars"}
        extra = sorted(set(s) - known - {"enabled"})
        if extra:
            gaps.append(f"mcp {name}: Codex-only settings not carried: {', '.join(extra)}")
        out.append(McpServer(name=name, config=conf))
    return out


def _memory(root: Path, b: Bundle) -> None:
    mem = root / "memories"
    if (mem / "MEMORY.md").is_file():
        b.memory.append(MemoryDir(project="_global", path=mem))
        # The whole folder is copied (MEMORY.md links into rollout_summaries/), but only
        # memory_summary.md is auto-injected; the rest is read on demand.
        b.gaps.append("Codex memory: copied as global memory; only memory_summary.md is auto-loaded each turn")
    dbs = sorted(p.name for p in root.glob("memories_*.sqlite"))
    if dbs:
        b.gaps.append(f"{', '.join(dbs)}: Codex memory index DB not migrated")


def _hooks(root: Path, cfg: dict, b: Bundle) -> None:
    notify = cfg.get("notify")
    if isinstance(notify, list) and notify:
        b.hooks.append(Hook(event="stop", command=shlex.join(notify), origin="config.toml notify"))
        b.gaps.append("notify: mapped to a stop hook, but Codex passes a JSON arg (argv) while "
                      "the target sends JSON on stdin; check the script still works")
    hj = root / "hooks.json"
    if hj.is_file():
        try:
            groups = json.loads(hj.read_text(encoding="utf-8")).get("hooks", {})
        except (json.JSONDecodeError, AttributeError):
            groups = {}
            b.gaps.append("hooks.json unreadable, hooks not migrated")
        for ev, entries in groups.items():
            canon = _HOOK_EVENTS.get(re.sub(r"(?<!^)(?=[A-Z])", "_", ev).lower())
            if not canon:
                b.gaps.append(f"hooks.json {ev}: no equivalent event, not migrated")
                continue
            for e in entries:
                for h in e.get("hooks", []):
                    if h.get("command"):
                        b.hooks.append(Hook(event=canon, command=h["command"],
                                            matcher=e.get("matcher") or None, origin="hooks.json"))
    trusted = cfg.get("hooks", {}).get("state", {})
    other = {k.split(":")[0] for k in trusted if not k.startswith(str(hj))}
    if other:
        b.gaps.append(f"hooks: {len(other)} project/plugin hooks.json files trusted in Codex, not migrated")


def _guards(root: Path, gaps: list[str]) -> list[Guard]:
    out, allows = [], 0
    for f in sorted((root / "rules").glob("*.rules")) if (root / "rules").is_dir() else []:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))  # Starlark call syntax is Python's
        except SyntaxError:
            gaps.append(f"rules/{f.name}: could not parse, skipped")
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "prefix_rule"):
                continue
            try:
                kw = {k.arg: ast.literal_eval(k.value) for k in node.keywords}
            except ValueError:
                gaps.append(f"rules/{f.name}: non-literal prefix_rule skipped")
                continue
            decision = kw.get("decision", "allow")
            if decision == "allow":
                allows += 1
                continue
            action = {"forbidden": "deny", "prompt": "ask"}.get(decision)
            if not action or not kw.get("pattern"):
                gaps.append(f"rules/{f.name}: prefix_rule decision={decision!r} not migrated")
                continue
            out.append(Guard(pattern=_prefix_regex(kw["pattern"]), action=action, origin=f"rules/{f.name}"))
    if allows:
        gaps.append(f"rules: {allows} allow prefix_rule(s) not migrated (target decides its own allowlist)")
    return out


def _prefix_regex(tokens: list) -> str:
    # A token may be a list of alternatives: ["git", ["push", "fetch"]].
    parts = ["(?:%s)" % "|".join(map(re.escape, t)) if isinstance(t, list) else re.escape(t)
             for t in tokens]
    return r"^" + r"\s+".join(parts) + r"(\s|$)"


def _other_gaps(root: Path, cfg: dict, gaps: list[str]) -> None:
    plugins = cfg.get("plugins", {})
    if plugins:
        on = sum(1 for p in plugins.values() if isinstance(p, dict) and p.get("enabled"))
        gaps.append(f"plugins: {on} enabled Codex plugins ({len(plugins)} listed) not migrated; reinstall by hand")
    if cfg.get("marketplaces"):
        gaps.append(f"marketplaces: {len(cfg['marketplaces'])} plugin marketplaces not migrated")
    if cfg.get("profiles"):
        gaps.append(f"profiles: {', '.join(cfg['profiles'])} not migrated")
    if cfg.get("model_providers"):
        gaps.append(f"model_providers: {', '.join(cfg['model_providers'])} not migrated")
    settings = [k for k in ("model", "model_reasoning_effort", "approval_policy", "sandbox_mode",
                            "approvals_reviewer", "service_tier") if k in cfg]
    if settings:
        gaps.append("settings: " + ", ".join(f"{k}={cfg[k]}" for k in settings) + " not migrated")
    for key in ("agents", "features", "shell_environment_policy", "sandbox_workspace_write",
                "tui", "desktop", "projects"):
        if cfg.get(key):
            gaps.append(f"config [{key}]: Codex-only settings not migrated")
    for name, what in (("computer-use", "computer-use app"), ("browser", "browser settings"),
                       ("auth.json", "Codex login (auth.json)"), ("history.jsonl", "prompt history")):
        if (root / name).exists():
            gaps.append(f"{name}: {what} not migrated")
    dbs = sorted(p.name for p in root.glob("*.sqlite") if not p.name.startswith("memories_"))
    if dbs:
        gaps.append(f"sqlite state not migrated: {', '.join(dbs)}")


def _titles(index: Path) -> dict[str, str]:
    titles = {}
    if index.is_file():
        for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("id") and r.get("thread_name"):
                titles[r["id"]] = r["thread_name"]  # later lines are renames; last wins
    return titles


def _ms(ts: str | None) -> int:
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except (AttributeError, ValueError):
        return 0


def _sessions(sdir: Path, titles: dict[str, str]):
    if not sdir.is_dir():
        return
    for f in sorted(sdir.rglob("rollout-*.jsonl")):
        s = _session(f, titles)
        if s:
            yield s


def _session(f: Path, titles: dict[str, str]) -> Session | None:
    meta, model, msgs = {}, None, []

    def add(role, text, ts):
        if not text.strip():
            return
        if msgs and msgs[-1].role == role:  # merge consecutive same-role messages
            msgs[-1].text += "\n\n" + text
        else:
            msgs.append(Message(role=role, text=text, ts_ms=_ms(ts), model=model if role == "assistant" else None))

    with f.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            p, ts, typ = r.get("payload") or {}, r.get("timestamp"), r.get("type")
            if typ == "session_meta":
                if meta:  # forked threads embed their parent's meta later on; the first is ours
                    continue
                meta = p
                # Subagent / guardian-review threads are Codex internals, not user conversations.
                if p.get("thread_source") not in (None, "user"):
                    return None
            elif typ == "turn_context":
                model = p.get("model") or model
            elif typ == "response_item":
                pt = p.get("type")
                if pt == "message" and p.get("role") == "user":
                    add("user", "\n".join(c.get("text", "") for c in p.get("content", [])
                                          if c.get("type") == "input_text" and not _INJECTED.match(c.get("text", ""))), ts)
                elif pt == "message" and p.get("role") == "assistant":
                    add("assistant", "\n".join(c.get("text", "") for c in p.get("content", [])
                                               if c.get("type") == "output_text"), ts)
                elif pt in ("function_call", "custom_tool_call"):
                    args = p.get("arguments") if pt == "function_call" else p.get("input")
                    args = " ".join(str(args or "").split())[:_TOOL_ARG_MAX]
                    add("assistant", f"→ {p.get('name', '?')}: {args}", ts)
    if not any(m.role == "user" for m in msgs):
        return None
    sid = meta.get("id") or f.stem.split("-", 6)[-1]
    return Session(id=sid, cwd=meta.get("cwd", ""), started=meta.get("timestamp", ""),
                   messages=msgs, title=titles.get(sid))
