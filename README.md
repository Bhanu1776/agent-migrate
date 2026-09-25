# agent-migrate

Move your coding-agent setup from one harness to another with one command.

```
claude-code ─┐                      ┌─→ pi
codex ───────┴─→  neutral bundle  ──┴─→ (more writers welcome)
```

Readers turn a harness's config into a neutral bundle. Writers turn the bundle into another harness's files. Each new harness needs one adapter, not one converter for every pair.

## What it moves

| Part | From | To pi |
|---|---|---|
| Instructions | `CLAUDE.md`, `AGENTS.md` | A marked block in `AGENTS.md`. Your own text is kept and backed up. |
| Skills | user and plugin skills | Symlinks for skills pi can't already see. Skills that were "off" stay off. |
| Prompts | `commands/`, `prompts/` | `/name` commands. Plugin commands get the plugin name as a prefix. |
| MCP servers | `.claude.json`, `config.toml` | `mcp.json` (mode 0600) plus [`pi-mcp-adapter`](https://www.npmjs.com/package/pi-mcp-adapter) |
| Memory | Claude project memory, Codex memories | `memory/<project>`. It goes into the prompt on every turn. |
| Hooks | settings, plugin hooks, Codex `notify` | A bridge extension runs them. Claude-style JSON goes in on stdin, and exit code 2 blocks the action. |
| Guards | permission deny/ask rules, Codex `.rules` | pi asks you before a matching bash command. If no one can answer, it blocks the command. |
| Chats | transcripts, rollouts | pi sessions for `/resume`. Only the text and short tool-call lines are kept. |

Every run ends with a **"not migrated"** list: logins, account connectors, keybindings, and harness-only settings. That list is your to-do list.

## Use

Needs Python 3.11+ and only the standard library.

```sh
./agent-migrate claude-code pi --dry-run        # show the plan, write nothing
./agent-migrate claude-code pi --target /tmp/pi-test
node tests/verify_pi_target.mjs /tmp/pi-test ~/some/repo   # check the result with pi's own loader
./agent-migrate claude-code pi                  # apply to ~/.pi/agent
./agent-migrate codex pi --only skills,mcp,memory
```

To run it from anywhere, symlink `agent-migrate` into a folder on your `PATH`.

## Guarantees

- `--dry-run` writes nothing.
- Runs are idempotent: a second run does not duplicate chats, blocks, or links.
- Secret-bearing config goes only into 0600 files and is never printed.
- It never deletes anything you wrote. Existing files get a backup with a timestamp.

## Add a harness

1. Reader: `agent_migrate/readers/<name>.py` with `read(home) -> Bundle`.
2. Writer: `agent_migrate/writers/<name>.py` with `plan(bundle, target, home, parts) -> Plan`.
3. Register it in `READERS` or `WRITERS` in `agent_migrate/cli.py`.

The neutral types are in `agent_migrate/model.py`.

## Test

```sh
python3 -m unittest discover -s tests -t .
```
