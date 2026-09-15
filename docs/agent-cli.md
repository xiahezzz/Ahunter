# A Hunter agent CLI

`ahunter` exposes all 46 current `/api/` operations through named commands. It
uses the existing loopback HTTP service and requires no new runtime dependencies.
It does not start the API or execute application services inside the CLI process.

## Install in this workspace

From the repository root, refresh the existing runtime's package entry points:

```bash
rtk .venv-runtime/bin/python -m pip install --no-deps --editable .
```

If the runtime is missing, first follow `advisor.runtime_env` in `agents.md`.
Use `.venv-runtime/bin/ahunter` directly, or install a personal command link:

```bash
rtk ln -s /Users/mac/Documents/Ahunter/a_hunter/.venv-runtime/bin/ahunter /Users/mac/.local/bin/ahunter
rtk ln -s /Users/mac/Documents/Ahunter/a_hunter/skills/ahunter /Users/mac/.codex/skills/ahunter
```

Create parent directories if absent. These commands intentionally fail if a
destination already exists: check an existing installation before changing it.
Keep `/Users/mac/.local/bin` on PATH. The skill link points at the versioned source,
so CLI documentation and skill updates do not require maintaining a second copy.
For a different checkout, substitute its absolute path. Python module usage
from the project is `rtk .venv-runtime/bin/python -m advisor.cli`.

## Use

```bash
rtk ahunter commands
rtk ahunter services status
rtk ahunter mx events list --limit 10
rtk ahunter research requests create --help
```

The default origin is `http://127.0.0.1:8000`. `--base-url` or `AHUNTER_BASE_URL`
may override it with an HTTP loopback origin. `--timeout` defaults to 30 seconds
for each HTTP connect/read wait; it is not a research task deadline. Global
options are accepted before or after subcommands. `--pretty` indents JSON.

Success is one JSON object on stdout with `ok`, HTTP `status`, and the unchanged
API payload in `data`. Errors are one JSON object on stderr with `ok: false`,
HTTP `status` if available, and `error.code`, `error.message`, `error.detail`.
Exit codes: 0 HTTP success, 2 usage/input, 3 transport, 4 HTTP rejection,
5 invalid response/download failure, 130 interrupted. Help is plain text.

No prompts, redirects, automatic pagination, service starts, or write retries
occur. Environment proxies and netrc credentials are ignored. Body commands
accept named options or `--json FILE` / `--json -` for stdin; complex Agent access
and ledger imports require JSON. Downloads require a new `--output PATH`, save
complete original bytes atomically, and return a path, size, SHA-256, and type.

For operational guidance, read [the skill](../skills/ahunter/SKILL.md),
[workflows](../skills/ahunter/references/workflows.md), and the
[generated command reference](../skills/ahunter/references/commands.md).

## Maintain

`advisor/cli/catalog.py` declares command paths, fields, and help.
`main.py` handles command parsing and request serialization.
`client.py` handles HTTP, error envelopes, and downloads. Business validation
stays in the Web API. Existing `advisor-*` commands continue to serve maintenance
and non-Web operations.

Every project change requires the semantic CLI/skill impact review described
in `agents.md`. Regenerate command documentation after catalog changes:

```bash
rtk .venv311/bin/python -m advisor.cli.maintenance --write
```

Run `python -m advisor.cli.maintenance --check` and relevant pytest tests through
the clean-shell wrapper in `agents.md`. The mandatory offline self-test includes
`tests/advisor/test_agent_cli.py`, which compares every Web route and query field
to CLI coverage and checks the generated reference. Request/response behavior is
tested over loopback HTTP and against the real FastAPI application using isolated
temporary databases and fake service/browser launchers. No live write is needed.
