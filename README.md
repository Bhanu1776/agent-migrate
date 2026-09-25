# agent-migrate

Move your coding-agent setup from one harness to another with one command.

```
claude-code ─┐                      ┌─→ pi · oh-my-pi · prime-agent
codex ───────┴─→  neutral bundle  ──┴─→ opencode · hermes
```

Readers turn a harness's config into a neutral bundle. Writers turn the bundle into another harness's files. Each new harness needs one adapter, not one converter for every pair.

## Supported

| Target | Default dir | Notes |
|---|---|---|
| `pi` | `~/.pi/agent` | Bridge extension for memory, hooks, and guards. Uses [`pi-mcp-adapter`](https://www.npmjs.com/package/pi-mcp-adapter) for MCP. |
| `oh-my-pi` | `~/.omp/agent` | omp already reads Claude and Codex setups, so the tool adds only what omp can't see. Import chats with `omp --from-claude` / `--from-codex`. |
| `prime-agent` | `~/.prime/agent` | Native MCP. The only tool is `ipython`, so guards and hooks see `!cmd` lines and string literals only. |
| `opencode` | `~/.config/opencode` | Guards become `permission.bash` rules. A plugin runs hooks and memory. Chats come in through `opencode import`. |
| `hermes` | `~/.hermes` | `SOUL.md`, `config.yaml` blocks, and native shell hooks and approvals. Secrets go in `.env`. Chats are not migrated. |

## What it moves

| Part | From | To pi (other targets map to their own native equivalents) |
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
./agent-migrate <source> <target> --dry-run      # show the plan, write nothing
./agent-migrate <source> <target> --target /tmp/test   # try it on a scratch dir
./agent-migrate <source> <target>                # apply to the target's default dir
```

- Sources: `claude-code`, `codex`
- Targets: `pi`, `oh-my-pi`, `prime-agent`, `opencode`, `hermes`
- Add `--only skills,mcp,memory` to move some parts only. Add `-v` to list every item.

```sh
./agent-migrate claude-code opencode --dry-run
./agent-migrate codex pi --only skills,mcp,memory
node tests/verify_pi_target.mjs /tmp/test ~/some/repo   # pi only: check the result with pi's own loader
```

You can run a second source into the same target. Each source keeps its own marked block.

To run it from anywhere, symlink `agent-migrate` into a folder on your `PATH`.

## Before you apply

- Read the **"not migrated"** list at the end of the dry run. That list is your to-do list.
- If you already set up the target by hand (for example your own memory or hook extension), remove that first, or those things run twice.
- Hooks, including plugin hooks, are copied as they are. Check the **hooks** rows in the dry run.
- Account-bound things are never copied: logins, API keys in auth files, and hosted connectors (Slack, Gmail, and so on).

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

## License

MIT. See [LICENSE](LICENSE).
