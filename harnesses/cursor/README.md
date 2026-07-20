# sema-cursor

Tier 3 wrapper shim for the [Cursor CLI](https://cursor.com/docs/cli) that gates
prompts through `sema check` before invoking `cursor-agent`.

Cursor's hook machinery exists — including Claude Code-compatible config loading
via `plugins/hooks/hooks.json` — but hook dispatch is currently server-gated off.
Until in-harness hooks can run on every prompt submission, this wrapper performs
the same ref gate at invocation time: it scans the prompt for content-addressed
sema refs (`Handle#stub`), verdicts them against the active registry, and either
blocks stale refs (enforce), injects a model-visible warning (warn), or passes
through unchanged.

## Usage

```bash
# Default warn mode — stale refs get a prepended warning, never blocked
sema-cursor "Implement the payment gate per InclusivePaymentThreshold#abcd"

# Enforce mode — stale refs halt before cursor-agent runs
SEMA_REF_GATE=enforce sema-cursor "Implement per InclusivePaymentThreshold#0000"

# Piped prompt (stdin is prepended on stale-ref warn)
echo "Fix the threshold per Handle#dead" | sema-cursor

# Disable gating entirely
SEMA_REF_GATE=off sema-cursor hello world

# Override registry DB (e.g. a project-local vocabulary)
SEMA_REF_GATE_DB=/path/to/taxonomy.db sema-cursor "prompt text"
```

Install on your `PATH` or invoke directly: `harnesses/cursor/sema-cursor`.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `SEMA_REF_GATE` | `warn` | Gate mode: `off`, `warn`, or `enforce` |
| `SEMA_REF_GATE_DB` | *(active registry)* | Registry DB path passed to `sema check --db` |
| `SEMA_REF_GATE_LOG` | *(unset)* | Append one JSON verdict line per invocation |
| `SEMA_PYTHON` | `python3` | Interpreter with the `sema` package importable |
| `SEMA_CURSOR_AGENT` | `cursor-agent` | Agent binary override (for testing with a fake) |

## Mode semantics

| Mode | Stale refs | Unknown refs | Registry unavailable |
| --- | --- | --- | --- |
| `off` | Pass through — no gating | Pass through | Pass through |
| `warn` | Inject warning into prompt, invoke agent | Stderr note, invoke agent | Fail open, invoke agent |
| `enforce` | Exit 2, agent never runs | Stderr note, invoke agent | Fail open, invoke agent |

**Warn-mode prompt identification** (where the stale-ref warning is injected):

1. **Piped stdin** — prepend `[sema-ref-gate warning]\n<repair>\n\n` to stdin
   content and pass the modified stream to the child.
2. **Last non-flag argv element** — if the last argument exists and does not start
   with `-`, treat it as the prompt and replace it with the prefixed version.
3. **Otherwise** — print
   `sema-cursor: warn injection skipped (could not identify the prompt argument)`
   to stderr and invoke the agent unchanged.

This identification rule is a limitation of Tier 3 wrapping: prompts passed only
via flags other than the trailing positional (e.g. `--prompt-file`) cannot receive
warn injection.

## Fail-open policy

If `sema check` exits with a registry error (exit 1), times out after 60 seconds,
returns unparseable JSON, or fails to spawn, the wrapper prints
`sema-cursor: registry unavailable, gate skipped` to stderr and invokes
`cursor-agent` unmodified. The harness is never bricked by gate infrastructure.

## Evidence

Evaluation results are published at
[sema-evals hook enforcement](https://robinoppenstam.github.io/sema-evals/hook-enforcement/)
and in the sema-evals cursor-hook experiment. With `composer-2.5-fast`:

- **warn** mode shipped **25/32** tasks despite **100%** stale-ref detection —
  warn injection is advisory, not a hard stop.
- **enforce** mode halted **32/32** stale-ref invocations.
- **Zero** gate false positives across the run.

## When Cursor hooks open up

The plugin's `plugins/hooks/hooks.json` Claude-format config is expected to load
via Cursor's third-party hook support once the server flag opens dispatch. That
path is unverified until the flag lands; this wrapper remains the reliable Tier 3
gate until then.

## Tests

Conformance tests live in `harnesses/cursor/tests/` and replay the shared fixture
set used by the Claude Code hook tests.
