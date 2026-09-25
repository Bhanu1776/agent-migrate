"""The neutral middle format. Readers fill a Bundle; writers turn a Bundle into a Plan.

N harnesses need N readers + N writers instead of N*N converters, so everything here is
harness-agnostic. Claude Code's shapes are the lingua franca where a choice was needed
(MCP server dicts, hook tool names), because they're the de-facto standard others copy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

PARTS = ("instructions", "skills", "prompts", "mcp", "memory", "hooks", "guards", "sessions")


@dataclass
class Skill:
    name: str
    path: Path  # directory that holds SKILL.md
    enabled: bool = True


@dataclass
class Prompt:
    name: str  # becomes the /command name
    path: Path  # a single .md file


@dataclass
class McpServer:
    name: str
    # Claude/.mcp.json shape: {type?, command, args, env} for stdio, {type?, url, headers} for
    # http/sse. `type` ("stdio" | "http" | "sse") is optional; writers map it to their schema.
    # May hold secrets: writers must only put it in 0600 files and never print values.
    config: dict


@dataclass
class Hook:
    # Canonical events: session_start, pre_tool, post_tool, stop, session_end.
    # Anything else a reader finds becomes a gap line, not a Hook.
    event: str
    command: str  # shell command; gets Claude-style JSON on stdin
    matcher: str | None = None  # regex on Claude tool names (Bash, Read, Edit, Write...)
    env: dict = field(default_factory=dict)  # e.g. CLAUDE_PLUGIN_ROOT for plugin hooks
    origin: str = ""  # "settings.json" or "plugin:<name>", for the report


@dataclass
class Guard:
    pattern: str  # Python/JS-compatible regex, matched against the full shell command
    action: str  # "ask" (human confirms) or "deny"
    origin: str = ""


@dataclass
class MemoryDir:
    project: str  # project slug (Claude style: path with non-alnum -> "-"), or "_global"
    path: Path  # directory holding MEMORY.md + one file per fact


@dataclass
class Message:
    role: str  # "user" | "assistant"
    text: str
    ts_ms: int
    model: str | None = None


@dataclass
class Session:
    id: str
    cwd: str
    started: str  # ISO 8601
    messages: list[Message]
    title: str | None = None


@dataclass
class Bundle:
    source: str
    instructions: str | None = None
    skills: list[Skill] = field(default_factory=list)
    prompts: list[Prompt] = field(default_factory=list)
    mcp: list[McpServer] = field(default_factory=list)
    memory: list[MemoryDir] = field(default_factory=list)
    hooks: list[Hook] = field(default_factory=list)
    guards: list[Guard] = field(default_factory=list)
    # Sessions are lazy: hundreds of MB, and dry runs only need a count.
    sessions: Callable[[], Iterator[Session]] = lambda: iter(())
    # Things the reader saw but cannot express in the model: shown in the gap report.
    gaps: list[str] = field(default_factory=list)


@dataclass
class Action:
    part: str
    kind: str  # "write" | "link" | "copy" | "skip" | "run"
    desc: str
    apply: Callable[[], None] | None = None  # None for skip/info rows


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def add(self, part, kind, desc, apply=None):
        self.actions.append(Action(part, kind, desc, apply))
