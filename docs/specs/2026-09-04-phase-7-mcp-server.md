# Phase 7: MCP server

**Goal:** clients that cannot run a shell, or that benefit from typed tool discovery, can query, validate and plan through opsmith over MCP.
**Depends on:** phase 6 (the skill and validator commands define what the tools expose).
**Size:** S.
**Ships as:** 1.0.x.

## Scope

1. `opsmith mcp` running a stdio MCP server over the core library.
2. Read, validate and plan tools only.
3. Resources for the config, schema and skill text.
4. Installer support for harness MCP configuration.

## Non-goals

- Mutating tools. `env create`, `release`, `update`, `run` and `destroy` stay CLI-only: MCP tool calls in current harnesses block without streamed output and hit tool timeouts, and a person should see Terraform and Ansible output and be able to interrupt. Tool descriptions say which CLI command to run instead.
- HTTP transport. Stdio is enough for local harnesses; add HTTP only if a hosted client needs it.

## Design

### Server

`opsmith/mcp/server.py` using the official Python MCP SDK's FastMCP. The server is a thin adapter: each tool calls the same core function the CLI command calls and returns its pydantic result as JSON. No logic lives in the MCP layer.

Working directory: the server takes `--src-dir` like the CLI, defaulting to the process working directory, and rejects paths outside it.

### Tools

| Tool | Arguments | Returns |
|------|-----------|---------|
| `get_config` | – | current `deployments.yml` as JSON, or null |
| `validate_config` | `config_yaml?: string` | `ValidateResult`; validates the given text or the file on disk |
| `get_schema` | `format: "json" \| "markdown"` | schema v2 |
| `list_environments` | – | names, provider, region, strategy |
| `get_status` | `env: string` | environment state summary: VM, registry, urls, deployed snapshot |
| `list_recipes` | – | catalog entries |
| `get_recipe` | `name: string` | recipe metadata, inputs, README text |
| `analyze_repo` | – | the phase 5 inventory |
| `render_compose` | `env: string` | rendered compose and masked env keys |
| `plan` | `env: string` or the `env create` inputs | `PlanResult` from phase 6, no side effects |
| `explain_error` | `error: object` | the hint and documentation excerpt for an error code |
| `check_templates` | `env?: string` | the phase 2 `template check` result: drift, invalid overlays, hand edits |
| `list_templates` | `env?: string` | every template with origin and variables, from the phase 2 registry |

Each description ends with the CLI equivalent so a harness that has a shell can prefer it.

### Resources

- `opsmith://config` — the config file.
- `opsmith://schema` — JSON schema.
- `opsmith://skill` — the `SKILL.md` text, so clients without skill support still get the instructions.
- `opsmith://recipes/<name>/readme` — recipe READMEs.

### Prompts

One MCP prompt, `deploy-project`, that expands to the skill's golden workflow with the environment name and provider filled in. Optional; cheap to provide.

### Installer

`opsmith agent install --mcp` from phase 6 writes the configuration for each selected target, using `uvx opsmith mcp` as the command so no global install is required. Formats per harness live in the same path module as the skill locations and are verified at implementation time.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/mcp/server.py` | server and tool adapters |
| `opsmith/cli/commands/mcp.py` | `opsmith mcp [--src-dir]` |
| `opsmith/cli/agent_install.py` | MCP config writers |
| `pyproject.toml` | add `mcp` dependency, or an extra `opsmith-cli[mcp]` if the dependency footprint matters |
| `README.md`, `docs/agents.md` | MCP section |

## Acceptance criteria

1. A harness with the server configured can call `validate_config`, `plan` and `list_recipes` and receives the same JSON the CLI prints with `--output json`.
2. No tool in the server modifies the file system, cloud resources, or the config; a test asserts the tool list against an allow-list.
3. `opsmith agent install --target claude --mcp` produces a working configuration on a clean machine with `uvx` available.

## Tests

- `test_mcp_server.py`: in-memory client session listing tools and calling each with a fixture project; allow-list assertion; error mapping to MCP error responses.

## Risks and open questions

- Tool schemas are derived from pydantic models; large results such as `render_compose` should be truncated with a note rather than failing client limits.
- If a client-side need for mutations appears, the answer is a job model with progress notifications, not blocking tools. Out of scope until asked for.
