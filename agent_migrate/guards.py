"""How a guard decides whether a shell command matches a rule.

Claude Code and Codex check every sub-command, so `cd x && git push` still hits a
`git push` rule. A regex on the whole string doesn't, so every runtime that enforces
migrated guards splits the command first. The TypeScript copy (SEGMENTS_TS) must stay
behaviour-identical to command_segments(); tests/test_guards.py pins both.

Over-splitting (e.g. a `;` inside quotes) only makes more segments, so it can only
make a guard stricter, never looser. That is the direction we want to fail in.
"""
from __future__ import annotations

import re

_SPLIT = re.compile(r"\n|;|&&|\|\||\||&")
_ASSIGN = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
# Wrappers that run the rest of the line as a command. Options that take a value are listed
# per wrapper (`sudo -u root`, `nice -n 10`), else the value would be taken as the command.
_WRAPPER = re.compile(
    r"^(?:sudo(?:\s+-[ugCDhpRrTt]\s+\S+|\s+-\S+)*|nice(?:\s+-n\s+\S+|\s+-\S+)*|env(?:\s+-[uSC]\s+\S+|\s+-\S+)*"
    r"|exec(?:\s+-a\s+\S+|\s+-\S+)*|xargs(?:\s+-[IdEeLnPs]\s+\S+|\s+-\S+)*|(?:command|builtin|nohup|time|eval)(?:\s+-\S+)*)(?:\s+|$)")
# `sh -c 'a && b'` is split like any other command (splitting ignores quotes), so the
# `sh -c '` opener is just one more wrapper to peel off.
_SHELL_C = re.compile(r"^(?:ba|z|da|k)?sh(?:\s+--?[A-Za-z-]+)*?\s+-[A-Za-z]*c\s+['\"]?")
_SUBST = re.compile(r"\$\(([^)]*)\)|`([^`]*)`")


def command_segments(cmd: str, depth: int = 0) -> list[str]:
    """The full command plus every simple command inside it, with wrappers stripped."""
    out = [cmd]
    if depth > 3:  # deeply nested $(...): stop, the outer string is still checked
        return out
    for m in _SUBST.finditer(cmd):
        out += command_segments(m.group(1) or m.group(2) or "", depth + 1)
    for part in _SPLIT.split(cmd):
        s = part.strip()
        while True:  # peel quotes/parens, VAR=1, sudo/env/..., sh -c ' until nothing changes
            t = s.lstrip("({'\"").rstrip(")}'\"").strip()
            t = _SHELL_C.sub("", _WRAPPER.sub("", _ASSIGN.sub("", t))).strip()
            if t == s:
                break
            s = t
        if s:
            out.append(s)
    return out


def matches(pattern: str, cmd: str) -> bool:
    rx = re.compile(pattern)
    return any(rx.search(seg) for seg in command_segments(cmd))


# Same algorithm for the TS runtimes (pi / omp bridge, prime-agent bridge, opencode plugin).
SEGMENTS_TS = r"""
// Mirror of agent_migrate/guards.py command_segments(): keep them identical.
function commandSegments(cmd: string, depth = 0): string[] {
  const out = [cmd];
  if (depth > 3) return out;
  for (const m of cmd.matchAll(/\$\(([^)]*)\)|`([^`]*)`/g)) out.push(...commandSegments(m[1] ?? m[2] ?? "", depth + 1));
  for (const part of cmd.split(/\n|;|&&|\|\||\||&/)) {
    let s = part.trim();
    for (;;) {
      let t = s.replace(/^[({'"]+/, "").replace(/[)}'"]+$/, "").trim();
      t = t.replace(/^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+/, "").replace(/^(?:sudo(?:\s+-[ugCDhpRrTt]\s+\S+|\s+-\S+)*|nice(?:\s+-n\s+\S+|\s+-\S+)*|env(?:\s+-[uSC]\s+\S+|\s+-\S+)*|exec(?:\s+-a\s+\S+|\s+-\S+)*|xargs(?:\s+-[IdEeLnPs]\s+\S+|\s+-\S+)*|(?:command|builtin|nohup|time|eval)(?:\s+-\S+)*)(?:\s+|$)/, "").replace(/^(?:ba|z|da|k)?sh(?:\s+--?[A-Za-z-]+)*?\s+-[A-Za-z]*c\s+['"]?/, "").trim();
      if (t === s) break;
      s = t;
    }
    if (s) out.push(s);
  }
  return out;
}
"""
