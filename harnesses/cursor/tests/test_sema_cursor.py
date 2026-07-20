"""Tests for the Cursor agent shim (harnesses/cursor/sema-cursor).

The wrapper is a standalone executable that gates via ``sema check``, then
execs the agent binary from ``SEMA_CURSOR_AGENT`` (fallback ``cursor-agent``)
with the same args. These tests drive it as a subprocess against a fake agent
and a temp registry with one minted pattern — the canonical stub is an honest
content hash, not a hardcoded fixture.

Covers:
- conformance replay of the shared refcheck fixture under enforce
- warn-mode prompt injection (last argv vs piped stdin vs skipped)
- enforce blocking of stdin-borne stale refs
- off mode (no gating), unknown refs, fail-open, exit-code propagation
- SEMA_REF_GATE_LOG verdict log
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WRAPPER = Path(__file__).parent.parent / "sema-cursor"
CONFORMANCE = ROOT / "src" / "sema" / "core" / "tests" / "fixtures" / "refcheck_conformance.json"

HANDLE = "InclusivePaymentThreshold"

# A known-valid pattern (passes the pydantic mint schema); also used by the
# sema-evals babel-hook pilot.
PATTERN = {
    "handle": HANDLE,
    "mechanism": "Compare the integer payment amount with the configured minimum before accepting the task.",
    "gloss": "An inclusive minimum payment threshold in token base units.",
    "invariants": [
        "A payment of exactly 100000000 base units is accepted.",
        "A payment below 100000000 base units is rejected.",
    ],
    "parameters": [
        {
            "name": "minimum_base_units",
            "type": "integer",
            "range": [100000000],
            "value": 100000000,
            "description": "Minimum accepted payment in token base units.",
        },
        {
            "name": "comparison",
            "type": "enum",
            "range": ["greater-than-or-equal", "greater-than"],
            "value": "greater-than-or-equal",
            "description": "Comparator applied at the payment boundary.",
        },
    ],
    "failure_modes": [
        "Replacing the inclusive comparison with a strict comparison rejects the boundary value."
    ],
    "_meta": {"path": ["Society", "Protocols"], "ring": 2, "tier": 3},
}


@pytest.fixture(scope="session")
def registry(tmp_path_factory):
    """Temp registry DB with PATTERN minted; returns (db_path, canonical_stub)."""
    from sema.core.mint import mint_pattern
    from sema.taxonomy_graph.graph_store import GraphStore

    db_path = tmp_path_factory.mktemp("cursor-ref-gate") / "canon.db"
    result = mint_pattern(PATTERN, GraphStore(str(db_path)))
    assert result.success, result.errors
    stub = result.sema_ref.split("#")[1]
    return db_path, stub


@pytest.fixture(scope="session")
def fake_agent(tmp_path_factory):
    """Executable fake agent that records argv/stdin and exits FAKE_EXIT (default 0)."""
    root = tmp_path_factory.mktemp("fake-cursor-agent")
    agent = root / "fake-agent"
    agent.write_text(
        """#!/bin/sh
# Record argv (NUL-separated: arguments may be multiline) and stdin; print FAKE_OK.
ROOT="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
: > "$ROOT/argv.txt"
for a in "$@"; do
  printf '%s\\0' "$a" >> "$ROOT/argv.txt"
done
if [ -t 0 ]; then
  :
else
  cat > "$ROOT/stdin.txt"
fi
printf 'FAKE_OK\\n'
exit "${FAKE_EXIT:-0}"
"""
    )
    agent.chmod(0o755)
    return agent


def stale_stub(canonical: str) -> str:
    """A syntactically valid 4-hex stub guaranteed to differ from canonical."""
    return "0000" if canonical != "0000" else "0001"


def _clear_agent_records(agent: Path) -> None:
    for name in ("argv.txt", "stdin.txt"):
        path = agent.parent / name
        if path.exists():
            path.unlink()


def agent_ran(agent: Path) -> bool:
    return (agent.parent / "argv.txt").exists()


def agent_argv(agent: Path) -> list[str]:
    text = (agent.parent / "argv.txt").read_text()
    if not text:
        return []
    # NUL-separated so multiline arguments survive; trailing NUL yields a
    # final empty piece — drop it.
    parts = text.split("\0")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


def agent_stdin(agent: Path) -> str:
    path = agent.parent / "stdin.txt"
    return path.read_text() if path.exists() else ""


def run_wrapper(
    args,
    mode,
    db,
    agent: Path,
    stdin_text=None,
    log=None,
    sema_python=None,
    extra_env=None,
):
    """Invoke the cursor shim as a subprocess against the fake agent."""
    _clear_agent_records(agent)
    env = os.environ.copy()
    env["SEMA_REF_GATE"] = mode
    env["SEMA_CURSOR_AGENT"] = str(agent)
    env["SEMA_PYTHON"] = sema_python if sema_python is not None else sys.executable
    if db is not None:
        env["SEMA_REF_GATE_DB"] = str(db)
    else:
        env.pop("SEMA_REF_GATE_DB", None)
    if log is not None:
        env["SEMA_REF_GATE_LOG"] = str(log)
    else:
        env.pop("SEMA_REF_GATE_LOG", None)
    if extra_env:
        env.update(extra_env)
    # Closed/empty pipe still counts as "not a tty"; pass DEVNULL when unused
    # so gate text does not accidentally read pytest's own stdin.
    kwargs = {
        "args": [str(WRAPPER), *args],
        "env": env,
        "capture_output": True,
        "text": True,
        "timeout": 120,
    }
    if stdin_text is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin_text
    return subprocess.run(**kwargs)


def _load_conformance_cases():
    raw = json.loads(CONFORMANCE.read_text())
    return [pytest.param(case, id=case["name"]) for case in raw]


def _subst(text: str, canon: str, stale: str) -> str:
    return text.replace("{CANON}", canon).replace("{STALE}", stale)


class TestConformance:
    @pytest.mark.parametrize("case", _load_conformance_cases())
    def test_conformance_enforce(self, registry, fake_agent, case):
        db, canon = registry
        stale = stale_stub(canon)
        payload = _subst(case["payload"], canon, stale)
        expected_stale = [_subst(r, canon, stale) for r in case["expected"]["stale"]]

        result = run_wrapper([payload], "enforce", db, fake_agent)

        if expected_stale:
            assert result.returncode == 2
            assert "sema-cursor: BLOCKED" in result.stderr
            for ref in expected_stale:
                assert ref in result.stderr
            assert not agent_ran(fake_agent)
        else:
            assert result.returncode == 0
            assert "FAKE_OK" in result.stdout
            assert agent_ran(fake_agent)
            assert agent_argv(fake_agent)[-1] == payload


class TestWarnMode:
    def test_warn_prepends_to_last_arg_prompt(self, registry, fake_agent):
        db, canon = registry
        stale = stale_stub(canon)
        payload = f"Implement per {HANDLE}#{stale}."
        result = run_wrapper([payload], "warn", db, fake_agent)
        assert result.returncode == 0
        assert agent_ran(fake_agent)
        last = agent_argv(fake_agent)[-1]
        assert last.startswith("[sema-ref-gate warning]")
        assert payload in last

    def test_warn_with_piped_stdin(self, registry, fake_agent):
        db, canon = registry
        stale = stale_stub(canon)
        stdin_text = f"Implement per {HANDLE}#{stale}."
        result = run_wrapper(["-p"], "warn", db, fake_agent, stdin_text=stdin_text)
        assert result.returncode == 0
        assert agent_ran(fake_agent)
        assert agent_stdin(fake_agent).startswith("[sema-ref-gate warning]")

    def test_warn_unidentifiable_prompt_skips_injection(self, registry, fake_agent):
        db, canon = registry
        stale = stale_stub(canon)
        # Ref lives inside a flag-like arg: gate text sees it, but last arg
        # starts with "-" and there is no stdin → injection skipped.
        flag = f"--flag-with-{HANDLE}#{stale}"
        result = run_wrapper([flag], "warn", db, fake_agent)
        assert result.returncode == 0
        assert "warn injection skipped" in result.stderr
        assert agent_ran(fake_agent)
        assert agent_argv(fake_agent) == [flag]


class TestEnforceAndModes:
    def test_enforce_blocks_stdin_borne_stale_ref(self, registry, fake_agent):
        db, canon = registry
        stale = stale_stub(canon)
        stdin_text = f"Implement per {HANDLE}#{stale}."
        result = run_wrapper(["-p"], "enforce", db, fake_agent, stdin_text=stdin_text)
        assert result.returncode == 2
        assert "sema-cursor: BLOCKED" in result.stderr
        assert not agent_ran(fake_agent)

    def test_off_mode_never_gates(self, registry, fake_agent, tmp_path):
        _, canon = registry
        stale = stale_stub(canon)
        payload = f"Implement per {HANDLE}#{stale}."
        missing_db = tmp_path / "does-not-exist.db"
        result = run_wrapper([payload], "off", missing_db, fake_agent)
        assert result.returncode == 0
        assert "FAKE_OK" in result.stdout
        assert agent_ran(fake_agent)
        # No gate messaging in off mode even with a missing registry path.
        assert "BLOCKED" not in result.stderr
        assert "sema-ref-gate" not in result.stderr
        assert "registry unavailable" not in result.stderr

    def test_unknown_ref_passes_with_note(self, registry, fake_agent):
        db, _ = registry
        payload = "see PR#12ab"
        result = run_wrapper([payload], "enforce", db, fake_agent)
        assert result.returncode == 0
        assert "PR#12ab" in result.stderr
        assert agent_ran(fake_agent)

    def test_fail_open_when_registry_unavailable(self, registry, fake_agent):
        db, canon = registry
        stale = stale_stub(canon)
        payload = f"Implement per {HANDLE}#{stale}."
        result = run_wrapper(
            [payload],
            "enforce",
            db,
            fake_agent,
            sema_python="/usr/bin/env-nonexistent-python",
        )
        assert "registry unavailable" in result.stderr
        assert agent_ran(fake_agent)
        assert result.returncode == 0

    def test_exit_code_propagation(self, registry, fake_agent):
        db, canon = registry
        payload = f"Implement per {HANDLE}#{canon}."
        result = run_wrapper(
            [payload],
            "enforce",
            db,
            fake_agent,
            extra_env={"FAKE_EXIT": "7"},
        )
        assert result.returncode == 7


class TestVerdictLog:
    def test_verdict_log_records_enforce_then_warn(self, registry, fake_agent, tmp_path):
        db, canon = registry
        stale = stale_stub(canon)
        payload = f"Implement per {HANDLE}#{stale}."
        log = tmp_path / "gate.jsonl"

        blocked = run_wrapper([payload], "enforce", db, fake_agent, log=log)
        assert blocked.returncode == 2
        lines = log.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["mode"] == "enforce"
        assert entry["blocked"] is True
        assert f"{HANDLE}#{stale}" in entry["stale"]

        warned = run_wrapper([payload], "warn", db, fake_agent, log=log)
        assert warned.returncode == 0
        lines = log.read_text().splitlines()
        assert len(lines) == 2
        second = json.loads(lines[1])
        assert second["mode"] == "warn"
        assert second["blocked"] is False
        assert f"{HANDLE}#{stale}" in second["stale"]
