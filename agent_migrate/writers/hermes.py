"""Hermes Agent writer (github.com/NousResearch/hermes-agent): Bundle -> Plan under ~/.hermes.

Where each part lands (docs: hermes-agent.nousresearch.com/docs, source: hermes-agent main, Sep 2026):
- instructions -> SOUL.md, the only global context file Hermes loads every session (owned block).
- skills       -> symlinks in skills/<source>/; "off" skills -> config.yaml skills.disabled.
- prompts      -> skills in skills/agent-migrate-prompts/ (Hermes has no prompt templates; its docs
                  say "for reusable prompt workflows, create a skill"). Skills are /name commands.
- mcp          -> config.yaml mcp_servers; env/header values go to .env (0600) and are referenced
                  as ${VAR}, which Hermes interpolates at load time (tools/mcp_tool_config.py).
- memory       -> skills memory-<project>: Hermes memory (memories/MEMORY.md) is global and capped
                  at 2,200 chars, so per-project memory can't live there.
- hooks        -> config.yaml hooks (shell hooks: JSON on stdin, Claude-style block replies).
- guards       -> config.yaml approvals.deny (fnmatch globs) when a rule converts to a glob; "ask"
                  rules and the rest go to a small pre_tool_call guard script we ship.
- sessions     -> not written: history lives in SQLite state.db. Gap line points at Hermes's own
                  `hermes sessions import`.

config.yaml is the user's file and Python has no YAML writer, so we never re-serialize it. Our
entries live in marked blocks (`# agent-migrate:<key>:start/end`) as one JSON value per line
(JSON is valid YAML), which lets us read them back and replace them in place. A key we can't
extend safely (the user already set that child, or a flow-style value) goes to a side file + gap.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from datetime import datetime
from pathlib import Path

from .. import guards as guard_lib
from ..model import Bundle, Plan
from .pi import _upsert_block

try:  # stdlib-only tool: use a YAML parser to vet config.yaml only when one happens to be installed
    import yaml
except ImportError:
    yaml = None

DEFAULT_TARGET = "~/.hermes"
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")

HOOK_EVENTS = {"session_start": "on_session_start", "pre_tool": "pre_tool_call", "post_tool": "post_tool_call",
               "stop": "on_session_end", "session_end": "on_session_finalize"}
# Claude tool names (what hook matchers use) -> Hermes tool names (tools/*_tool.py registry).
TOOLS = {"Bash": "terminal", "Read": "read_file", "Write": "write_file", "Edit": "patch",
         "MultiEdit": "patch", "Grep": "search_files", "Glob": "search_files"}

HERMES_NOTES = """## Hermes mechanics (added by agent-migrate — the instructions below were written for another harness)

- Tools: `terminal` (Bash), `read_file`, `write_file`, `patch` (Edit), `search_files` (Grep/Glob). Subagents: `delegate_task`.
- Skills: `/<skill-name>`. Migrated slash commands are skills too. MCP tools are named `mcp_<server>_<tool>`.
- Memory: the `memory` tool. Memories migrated from another harness are `memory-*` skills: load the one for the current project.
- Chat title: `/title <name>`."""

GUARD_PY = '''#!/usr/bin/env python3
"""agent-migrate guard: Hermes pre_tool_call shell hook for migrated permission rules.
Rules live in guards.json next to this file. deny -> block; ask -> Hermes approval prompt.
am_guards.py is a verbatim copy of agent_migrate/guards.py: every sub-command is checked,
so `cd x && git push` still hits a `git push` rule."""
import json, re, sys
from pathlib import Path

from am_guards import command_segments

payload = json.load(sys.stdin)
cmd = (payload.get("tool_input") or {}).get("command") or ""
segs = command_segments(cmd)
rules = []
for src in json.loads((Path(__file__).parent / "guards.json").read_text())["sources"].values():
    for g in src:
        try:
            rules.append({**g, "rx": re.compile(g["pattern"])})
        except re.error:  # a broken rule must not fail-closed every terminal call
            print(f"agent-migrate: guard rule ({g.get('origin')}) has an invalid regex; skipped", file=sys.stderr)
for action in ("deny", "ask"):
    for g in rules:
        if g["action"] == action and any(g["rx"].search(s) for s in segs):
            if action == "deny":
                print(json.dumps({"action": "block", "message": f"Blocked by migrated rule ({g['origin']})"}))
            else:
                print(json.dumps({"action": "approve", "message": f"Migrated rule wants your OK ({g['origin']})",
                                  "rule_key": "agent-migrate"}))
            sys.exit(0)
print("{}")
'''


# ---------- small file helpers (same contract as the pi writer) ----------

def _write_private(path: Path, data: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(path, 0o600)


def _backup_once(path: Path):
    """Back up a file we're about to modify for the first time (no marker of ours inside yet)."""
    if path.is_file() and "agent-migrate:" not in path.read_text(errors="replace"):
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{STAMP}"))


def _yaml_ok(text: str) -> bool:
    """False only when a YAML parser is installed and rejects the text."""
    if yaml is None or not text.strip():
        return True
    try:
        yaml.safe_load(text)
        return True
    except yaml.YAMLError:
        return False


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _slug(name: str) -> str:
    """Hermes skill names: ^[a-z0-9][a-z0-9._-]*$, max 64 (tools/skill_manager_tool.py)."""
    return re.sub(r"[^a-z0-9._]+", "-", name.lower()).strip("-._")[:64] or "unnamed"


def _frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return {}, text
    fm = dict(re.findall(r"^([A-Za-z_-]+):[ \t]*['\"]?(.*?)['\"]?[ \t]*$", m.group(1), re.M))
    return fm, text[m.end():]


def _skill_md(name: str, description: str, body: str) -> str:
    # json.dumps gives a double-quoted scalar: valid YAML whatever the description contains.
    return f"---\nname: {json.dumps(name)}\ndescription: {json.dumps(description[:1000])}\n---\n\n{body.strip()}\n"


# ---------- config.yaml: marked JSON-per-line blocks ----------

def _owned_rx(key: str):
    return re.compile(rf"^([ \t]*)# agent-migrate:{re.escape(key)}:start\n(.*?)^[ \t]*# agent-migrate:{re.escape(key)}:end[ \t]*\n?",
                      re.M | re.S)


def _parse_owned(body: str) -> dict:
    out = {}
    for m in re.finditer(r'^[ \t]*("(?:[^"\\]|\\.)*"): (.*)$', body, re.M):
        out[json.loads(m.group(1))] = json.loads(m.group(2))
    return out


def _render(key: str, indent: str, children: dict) -> str:
    lines = [f"{indent}# agent-migrate:{key}:start"]
    lines += [f"{indent}{json.dumps(c)}: {json.dumps(v)}" for c, v in children.items()]
    return "\n".join(lines + [f"{indent}# agent-migrate:{key}:end"]) + "\n"


def _user_section(text: str, key: str):
    """(match of the top-level `key:` line, list of the section's body lines) or (None, [])."""
    km = re.search(rf"^{re.escape(key)}:(.*)$", text, re.M)
    if not km:
        return None, []
    body = []
    for line in text[km.end() + 1:].splitlines():
        if line.strip() and not line[0].isspace() and not line.startswith("#"):
            break
        body.append(line)
    return km, body


def _children(text: str, key: str) -> set[str]:
    """Child keys of a top-level mapping: the user's own plus the ones in our block."""
    m = _owned_rx(key).search(text)
    owned = set(_parse_owned(m.group(2))) if m else set()
    if m:
        text = text[:m.start()] + text[m.end():]
    km, body = _user_section(text, key)
    content = [l for l in body if l.strip() and not l.lstrip().startswith("#")]
    if not content:
        return owned
    indent = len(content[0]) - len(content[0].lstrip())
    names = {re.match(r"""\s*["']?([^"':#]+?)["']?\s*:""", l) for l in content if len(l) - len(l.lstrip()) == indent}
    return owned | {n.group(1).strip() for n in names if n}


def _owned(text: str, key: str) -> dict:
    m = _owned_rx(key).search(text)
    return _parse_owned(m.group(2)) if m else {}


def _merge_value(old, new):
    if isinstance(old, list) and isinstance(new, list):
        return old + [x for x in new if x not in old]
    return old  # a server/entry we already wrote wins: re-runs never rewrite it


def _merge_key(text: str, key: str, children: dict) -> tuple[str, dict]:
    """Put `children` under top-level `key`. Returns (new text, children that could not be merged)."""
    m = _owned_rx(key).search(text)
    rest = text[:m.start()] + text[m.end():] if m else text
    km, body = _user_section(rest, key)
    user_kids = _children(rest, key) if km else set()
    leftover = {c: v for c, v in children.items() if c in user_kids}
    children = {c: v for c, v in children.items() if c not in user_kids}
    if m:  # our block exists: merge and replace it in place
        cur = _parse_owned(m.group(2))
        for c, v in children.items():
            cur[c] = _merge_value(cur[c], v) if c in cur else v
        return text[:m.start()] + _render(key, m.group(1), cur) + text[m.end():], leftover
    if not children:
        return text, leftover
    if not km:  # no such key yet: append a new top-level section
        sep = "" if not text or text.endswith("\n") else "\n"
        return f"{text}{sep}{key}:\n{_render(key, '  ', children)}", leftover
    value = km.group(1).split("#")[0].strip()
    content = [l for l in body if l.strip() and not l.lstrip().startswith("#")]
    if value in ("{}", "null", "~") or (value == "" and not content):
        return f"{text[:km.start()]}{key}:\n{_render(key, '  ', children)}{text[km.end() + 1:]}", leftover
    if value == "" and not content[0].lstrip().startswith("-"):  # block mapping: insert under the key line
        indent = content[0][:len(content[0]) - len(content[0].lstrip())]
        return f"{text[:km.end() + 1]}{_render(key, indent, children)}{text[km.end() + 1:]}", leftover
    return text, {**leftover, **children}  # flow style / list / scalar: don't touch


def _merge_config(text: str, want: dict) -> tuple[str, dict]:
    leftovers = {}
    for key, children in want.items():
        if children:
            text, left = _merge_key(text, key, children)
            if left:
                leftovers[key] = left
    return text, leftovers


# ---------- mcp / hooks / guards translation ----------

def _env_name(*parts: str) -> str:
    return "AGENT_MIGRATE_MCP_" + "_".join(re.sub(r"[^A-Z0-9]+", "_", p.upper()).strip("_") for p in parts)


def _mcp_entry(name: str, cfg: dict, secrets: dict) -> dict:
    """Hermes mcp_servers shape (docs/reference/mcp-config-reference.md). Values of env/headers
    move to .env; the config keeps ${VAR} references, so config.yaml never holds a secret."""
    out = {k: cfg[k] for k in ("command", "args", "cwd", "url") if k in cfg}
    if cfg.get("type") == "sse" or cfg.get("transport") == "sse":
        out["transport"] = "sse"
    for field, tag in (("env", "ENV"), ("headers", "HDR")):
        vals = {}
        for k, v in (cfg.get(field) or {}).items():
            if isinstance(v, str) and re.fullmatch(r"\$\{[^}]+\}", v):
                vals[k] = v  # already a reference, not a secret
            else:
                var = _env_name(name, tag, k)
                secrets[var] = str(v)
                vals[k] = "${" + var + "}"
        if vals:
            out[field] = vals
    return out


def _dotenv_line(var: str, value: str) -> str:
    # Hermes expands ${NAME} in every .env value, quoted or not (hermes_cli/env_loader.py), and dotenv
    # has no escape for it. `${:-$}` (empty name, default "$") resolves to a lone "$" in one pass, so
    # `${:-$}{HOME}` loads as the literal `${HOME}`. Single quotes take no backslash escapes at all.
    value = value.replace("${", "${:-$}{")
    if "'" not in value and "\n" not in value:
        return f"{var}='{value}'"
    return var + '="' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _matcher(m: str | None) -> tuple[bool, str | None]:
    """Claude matcher regex -> Hermes tool-name regex. (False, _) when it hits no Hermes tool."""
    if not m or m == "*":
        return True, None
    try:
        hits = sorted({h for c, h in TOOLS.items() if re.fullmatch(m, c)})
    except re.error:
        return False, None
    return (True, "|".join(hits)) if hits else (False, None)


def _hook_command(cmd: str, env: dict) -> str:
    # Hermes runs hooks with shlex.split + shell=False, so wrap in sh -c to keep pipes, ~ and $VARS.
    return shlex.join(["env", *[f"{k}={v}" for k, v in env.items()], "sh", "-c", cmd] if env else ["sh", "-c", cmd])


def _glob_escape(s: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", s)


def _regex_to_globs(rx: str) -> list[str] | None:
    """Readers' guard regexes (anchored literals, .*, \\s+, (?:a|b), prefix tails) -> fnmatch globs
    for approvals.deny. None when the regex uses anything else."""
    if not rx.startswith("^"):
        return None
    s, tails = rx[1:], [""]
    for tail in (r"(\s.*)?$", r"(\s|$)"):
        if s.endswith(tail):
            s, tails = s[:-len(tail)], ["", " *"]
            break
    else:
        if not s.endswith("$") or s.endswith("\\$"):
            return None
        s = s[:-1]

    def literal(i: int, stop: str) -> tuple[str, int] | None:
        out = ""
        while i < len(s) and s[i] not in stop:
            if s.startswith(".*", i):
                out, i = out + "*", i + 2
            elif s.startswith(r"\s+", i):
                out, i = out + " ", i + 3
            elif s[i] == "\\" and i + 1 < len(s) and not s[i + 1].isalnum():
                out, i = out + _glob_escape(s[i + 1]), i + 2
            elif s[i] in ".^$*+?()[]{}|\\":
                return None
            else:
                out, i = out + _glob_escape(s[i]), i + 1
        return out, i

    outs, i = [""], 0
    while i < len(s):
        if s.startswith("(?:", i):
            alts, i = [], i + 3
            while True:
                r = literal(i, "|)")
                if not r or r[1] >= len(s):
                    return None
                alts.append(r[0])
                i = r[1] + 1
                if s[r[1]] == ")":
                    break
            outs = [o + a for o in outs for a in alts]
        else:
            r = literal(i, "(")
            if not r or r[1] == i:
                return None
            outs, i = [o + r[0] for o in outs], r[1]
    return [o + t for o in outs for t in tails]


def _visible_skills(root: Path) -> set[str]:
    names, seen = set(), set()
    for dirpath, dirs, files in os.walk(root, followlinks=True) if root.is_dir() else []:
        real = os.path.realpath(dirpath)
        if real in seen:  # a symlink loop (skills/x/up -> skills) would walk forever
            dirs[:] = []
            continue
        seen.add(real)
        if "SKILL.md" in files:
            names.add(Path(dirpath).name)
    return names


# ---------- plan ----------

def plan(b: Bundle, target: Path, home: Path, parts: set[str]) -> Plan:
    p = Plan(gaps=list(b.gaps))
    cfg_path, env_path = target / "config.yaml", target / ".env"
    cfg_text = _read(cfg_path)
    want: dict[str, dict] = {"mcp_servers": {}, "skills": {}, "approvals": {}, "hooks": {}}
    visible = _visible_skills(target / "skills")
    done_disabled = _owned(cfg_text, "skills").get("disabled", [])
    done_deny = _owned(cfg_text, "approvals").get("deny", [])
    done_hooks = _owned(cfg_text, "hooks")

    if "instructions" in parts and b.instructions:
        soul = target / "SOUL.md"

        def write_soul():
            soul.parent.mkdir(parents=True, exist_ok=True)
            _backup_once(soul)
            text = _upsert_block(_read(soul), "hermes-notes", HERMES_NOTES)
            soul.write_text(_upsert_block(text, b.source, f"# Instructions migrated from {b.source}\n\n{b.instructions}"))

        p.add("instructions", "write", f"SOUL.md ← {b.source} instructions ({len(b.instructions)} chars)", write_soul)
        if len(b.instructions) + len(HERMES_NOTES) > 20_000:
            p.gaps.append("SOUL.md is over 20,000 chars: Hermes truncates it; trim it or raise context_file_max_chars in config.yaml")

    if "skills" in parts:
        for s in b.skills:
            if s.name in visible:
                p.add("skills", "skip", f"{s.name}: already in Hermes skills")
            else:
                link = target / "skills" / b.source / s.name.replace("/", "-")
                p.add("skills", "link", f"{s.name} → {s.path}",
                      lambda link=link, src=s.path: (link.parent.mkdir(parents=True, exist_ok=True), link.symlink_to(src)))
                visible.add(s.name)
            if not s.enabled and s.name not in done_disabled:
                want["skills"].setdefault("disabled", []).append(s.name)
                p.add("skills", "write", f"{s.name}: disabled (was off in {b.source})")

    if "prompts" in parts:
        dollar = []
        for pr in b.prompts:
            name = _slug(pr.name)
            if name in visible:
                p.add("prompts", "skip", f"/{name}: a skill with that name exists")
                continue
            visible.add(name)
            fm, body = _frontmatter(_read(pr.path))
            if "$ARGUMENTS" in body or re.search(r"\$\d", body):
                dollar.append(name)
            desc = fm.get("description") or next((l.strip("# ").strip() for l in body.splitlines() if l.strip()), name)
            dst = target / "skills" / "agent-migrate-prompts" / name / "SKILL.md"
            p.add("prompts", "write", f"/{name} (skill) ← {pr.path}",
                  lambda dst=dst, md=_skill_md(name, desc, body): (dst.parent.mkdir(parents=True, exist_ok=True), dst.write_text(md)))
        if dollar:
            p.gaps.append(f"{len(dollar)} commands use $ARGUMENTS/$1 (e.g. /{dollar[0]}): Hermes appends your text after "
                          "the skill instead of substituting; edit them if the placement matters")

    if "mcp" in parts and b.mcp:
        existing = _children(cfg_text, "mcp_servers")
        secrets: dict[str, str] = {}
        for s in b.mcp:
            if s.name in existing:
                p.add("mcp", "skip", f"{s.name}: already configured")
                continue
            want["mcp_servers"][s.name] = _mcp_entry(s.name, s.config, secrets)
            held = " (values → .env, 0600)" if s.config.get("env") or s.config.get("headers") else ""
            p.add("mcp", "write", f"{s.name}: {'http' if 'url' in s.config else 'stdio'}{held}")
        have = set(re.findall(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", _read(env_path), re.M))
        new_env = {k: v for k, v in secrets.items() if k not in have}
        if new_env:
            def write_env(new_env=new_env):
                _backup_once(env_path)
                text = _read(env_path)
                m = _owned_rx("mcp-env").search(text)
                lines = [_dotenv_line(k, v) for k, v in new_env.items()]
                if m:
                    block = m.group(0).rstrip("\n").splitlines()
                    text = text[:m.start()] + "\n".join(block[:-1] + lines + block[-1:]) + "\n" + text[m.end():]
                else:
                    sep = "" if not text or text.endswith("\n") else "\n"
                    text += sep + "\n".join(["# agent-migrate:mcp-env:start", *lines, "# agent-migrate:mcp-env:end"]) + "\n"
                _write_private(env_path, text)

            p.add("mcp", "write", f".env: +{len(new_env)} MCP env/header values (0600)", write_env)

    if "memory" in parts:
        for m in b.memory:
            name = _slug("memory-" + m.project)
            dst = target / "skills" / "agent-migrate-memory" / name
            files = [f for f in m.path.rglob("*") if f.is_file()]
            new = [f for f in files if not (dst / f.relative_to(m.path)).exists()]
            if name in visible and not new:
                p.add("memory", "skip", f"{m.project}: up to date")
                continue
            visible.add(name)
            index = _read(m.path / "MEMORY.md") or "\n".join(f"- [{f.name}]({f.name})" for f in files)
            where = "all projects" if m.project == "_global" else f"the project {m.project} (path with / turned into -)"
            md = _skill_md(name, f"Facts remembered from {b.source} for {where}. Load it when working there.",
                           f"# Memory for {where}\n\nEach linked file holds one fact; read the ones that matter.\n\n{index}")

            def copy(m=m, dst=dst, new=new, md=md):
                for f in new:
                    out = dst / f.relative_to(m.path)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, out)
                if not (dst / "SKILL.md").exists():
                    (dst / "SKILL.md").write_text(md)

            p.add("memory", "copy", f"{m.project} → skill {name}: {len(new)} files", copy)
        if b.memory:
            p.gaps.append("Hermes memory (memories/MEMORY.md) is global and capped at 2,200 chars: migrated memories are "
                          "memory-* skills; move key facts into it with the `memory` tool if you want them always loaded")

    if "hooks" in parts and b.hooks:
        tool_hooks = False
        for h in b.hooks:
            ok, matcher = _matcher(h.matcher)
            if not ok:
                p.gaps.append(f"hook {h.event} [{h.matcher}] ({h.origin}): Hermes has no such tool; skipped")
                continue
            entry = {"command": _hook_command(h.command, h.env)}
            if matcher and h.event in ("pre_tool", "post_tool"):
                entry = {"matcher": matcher, **entry}
            # Never the command text: it can carry tokens, and plans are printed.
            label = f"{HOOK_EVENTS[h.event]}{f' [{matcher}]' if matcher else ''} ({h.origin})"
            if entry in done_hooks.get(HOOK_EVENTS[h.event], []):
                p.add("hooks", "skip", label)
                continue
            tool_hooks |= h.event in ("pre_tool", "post_tool")
            want["hooks"].setdefault(HOOK_EVENTS[h.event], []).append(entry)
            p.add("hooks", "write", label)
        if tool_hooks:
            p.gaps.append("tool hooks get Hermes tool names in tool_name (terminal, write_file, patch, ...): "
                          "fix scripts that compare against Claude names like Bash")

    if "guards" in parts and b.guards:
        scripted = []
        for g in b.guards:
            globs = _regex_to_globs(g.pattern) if g.action == "deny" else None
            if globs and all(x in done_deny for x in globs):
                p.add("guards", "skip", f"deny {g.origin}: already in approvals.deny")
            elif globs:
                want["approvals"].setdefault("deny", []).extend(globs)
                p.add("guards", "write", f"deny {g.origin} → approvals.deny")
            else:
                scripted.append({"pattern": g.pattern, "action": g.action, "origin": g.origin})
                p.add("guards", "write", f"{g.action:4} {g.origin} → guard hook")
        if scripted:
            gdir = target / "agent-migrate"
            guard_hook = {"matcher": "terminal", "command": shlex.join(["python3", str(gdir / "guard.py")]),
                          "timeout": 10, "fail_closed": True}
            if guard_hook not in done_hooks.get("pre_tool_call", []):
                want["hooks"].setdefault("pre_tool_call", []).append(guard_hook)

            def write_guards(scripted=scripted):
                data = json.loads(_read(gdir / "guards.json") or '{"sources": {}}')
                data.setdefault("sources", {})[b.source] = scripted
                gdir.mkdir(parents=True, exist_ok=True)
                (gdir / "guards.json").write_text(json.dumps(data, indent=2))
                (gdir / "guard.py").write_text(GUARD_PY)
                (gdir / "am_guards.py").write_text(Path(guard_lib.__file__).read_text())

            if json.loads(_read(gdir / "guards.json") or "{}").get("sources", {}).get(b.source) == scripted:
                p.add("guards", "skip", "agent-migrate/guards.json: up to date")
            else:
                p.add("guards", "write", "agent-migrate/guard.py + guards.json (pre_tool_call hook)", write_guards)
            if any(g["action"] == "ask" for g in scripted):
                p.gaps.append("'ask' rules prompt through the shell-hook approve reply, which needs a Hermes newer than 0.19; "
                              "older ones let those commands run")

    if want["hooks"]:
        p.gaps.append("Hermes asks once before running each new shell hook: approve them on first launch "
                      "(or `hermes --accept-hooks`), then check with `hermes hooks list`")

    new_cfg, leftovers = _merge_config(cfg_text, want)
    # ponytail: without PyYAML a broken config.yaml can't be detected; we still only splice text into it.
    if new_cfg != cfg_text and not (_yaml_ok(cfg_text) and _yaml_ok(new_cfg)):
        # Never write into a file Hermes can't parse (or that our splice would break): hand it all over.
        p.gaps.append(("config.yaml does not parse as YAML" if not _yaml_ok(cfg_text) else
                       "merging into config.yaml would break its YAML") + ": it was left untouched")
        new_cfg, leftovers = cfg_text, {k: v for k, v in want.items() if v}
    if new_cfg != cfg_text:
        keys = ", ".join(k for k, v in want.items() if v)

        def write_cfg():
            _backup_once(cfg_path)
            cur = _read(cfg_path)
            text, _ = _merge_config(cur, want)  # re-merge against what's on disk now
            if not (_yaml_ok(cur) and _yaml_ok(text)):
                raise RuntimeError("config.yaml stopped parsing since the plan; left untouched")
            _write_private(cfg_path, text)

        p.add("settings", "write", f"config.yaml ({keys})", write_cfg)
    elif cfg_text:
        p.add("settings", "skip", "config.yaml: up to date")
    if leftovers:
        snippet = target / "agent-migrate" / "config-snippet.yaml"
        body = "".join(f"{k}:\n" + "".join(f"  {json.dumps(c)}: {json.dumps(v)}\n" for c, v in kids.items())
                       for k, kids in leftovers.items())
        p.add("settings", "write", f"agent-migrate/config-snippet.yaml ({', '.join(leftovers)})",
              lambda: _write_private(snippet, body))
        p.gaps.append(f"config.yaml already sets {', '.join(f'{k}.{c}' for k, kids in leftovers.items() for c in kids)}: "
                      f"merge {snippet} into it by hand")

    if "sessions" in parts and next(b.sessions(), None) is not None:
        p.gaps.append("chats: Hermes keeps history in SQLite (state.db), so they're not written; import one at a time "
                      "with `hermes sessions import --from claude|codex <file>` (Hermes after 0.19)")
    return p
