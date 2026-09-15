# Project instructions

## Project context

This repository develops a text-based strategic AI agent benchmark on top of 0 A.D.
Read [the implementation specification](docs/agent-benchmark/implementation-spec.md) before
planning or changing benchmark behavior. Read
[the source review](docs/agent-benchmark/source-review.md) when working on engine integration.
Reference those documents; do not copy their contents here.

The specification describes proposed behavior, not features already implemented. Verify claims
against the current checkout and distinguish existing behavior, planned changes, and tested results.
If the local specification is missing, report that limitation and continue independent work;
request the missing context before making decisions that depend on it.

Follow the user's current scope and applicable directory instructions. Resolve routine choices
using the specification and existing code. Record material design changes in the local build docs
and update affected contracts when an authorized implementation changes them.

## Coding style and maintainability

- Use the repository's existing conventions consistently. Follow `.editorconfig`,
  `eslint.config.mjs`, `ruff.toml`, `.markdownlint.yaml`, and `.pre-commit-config.yaml`.
  Match the surrounding subsystem's naming, layout, imports, and error-handling patterns.
- Use tabs and the configured brace style in JavaScript; use four spaces and the repository's
  Ruff configuration in Python. Match established C++ indentation, braces, and ownership patterns.
  Consistency means following each language's conventions, not imposing one format on every file.
- Prefer straightforward code, descriptive names, small cohesive functions, and explicit data flow.
  Make ownership, lifetimes, units, and side effects clear. Avoid speculative abstractions,
  unnecessary dependencies, and clever code that makes ordinary changes difficult.
- Keep interfaces narrow and responsibilities separate. Reuse existing engine facilities and
  helpers before introducing parallel implementations. Centralize shared rules and schemas.
- Explain non-obvious decisions, invariants, and tradeoffs in comments.
  Avoid narrating obvious code.
  Document public interfaces, units, error behavior, and compatibility expectations.
- Handle failures explicitly and preserve useful context. Do not swallow exceptions, silently
  substitute success, or leave background work and resources running after an error.
- Keep changes focused. Avoid unrelated formatting, renames, generated-file edits, or vendor edits.
  Preserve existing license notices and follow the repository's copyright-header conventions.

## Benchmark and engine correctness

- Preserve ordinary game mechanics unless the task explicitly requires changing them. Keep the
  benchmark integration small and maintainable relative to upstream.
- Maintain the separation between agent-visible observations and privileged evaluator state.
  Validate player authority and every action field at the boundary. Do not expose raw evaluation,
  hidden state, or debug commands through agent tools.
- Treat observations and inspection tools as read-only. Do not consume another component's event
  buffers or change simulation behavior while reporting state.
- Make simulation time, decision boundaries, command ordering, and terminal conditions explicit.
  Distinguish milliseconds, seconds, engine turns, and wall-clock deadlines in names and schemas.
- Keep simulation work on the main thread through the supported bridge. Use exception-safe
  resource management, bounded requests, cancellation, and clean shutdown for external interfaces.
- Preserve reproducibility: version contracts, record resolved configuration and seeds, and keep
  scenario interventions replayable. Distinguish accepted commands from successfully executed work.
- Keep provider-specific model code outside the simulation. Keep credentials, private seeds,
  evaluator artifacts, and other agents' private context out of model observations and commits.
- Keep MCP or other tool adapters thin; the runner and engine contracts own authority and timing.

## Validation and reporting

- Read the affected implementation, call sites, and relevant tests before editing. Use the existing
  build and test infrastructure rather than inventing a competing workflow.
- Run relevant formatters, linters, and tests for the changed files. Use the configured pre-commit
  hooks where available. Do not apply repository-wide automatic formatting for a small change.
- Add meaningful regression coverage for behavior changes, especially visibility, authority,
  exact stepping, event capture, lifecycle failures, and replay consistency. Put both turn limits
  and wall-clock timeouts on game integration tests; never rely on an unbounded polling loop.
- Test important failure cases as well as success. Do not weaken assertions or disable checks to
  make a change appear correct. Distinguish pre-existing failures from introduced regressions.
- For documentation-only changes, check links, formatting, and source accuracy; engine tests are
  unnecessary unless the task also changes executable behavior.
- Inspect the final diff and run `git diff --check`. Report what changed, why, which checks ran,
  and any unresolved limits. Never claim a build, test, replay, or deployment succeeded without
  evidence. If dependencies or binaries are unavailable, state what remains unverified.

## Repository and artifact hygiene

- Inspect Git status first; preserve work that predates the task. Stage only intended files.
  Do not reset, discard, force-push, or rewrite shared history without explicit authorization.
- Commit or push when requested. Verify the target remote and branch, and use ordinary
  fast-forward pushes. If the remote advances, reconcile safely before retrying.
- Keep build plans, specifications, decisions, and local artifacts under `docs/agent-benchmark/`.
  Executable benchmark source and released test fixtures belong in tracked source directories.
- This file, `CLAUDE.md`, and the benchmark docs directory are intentionally ignored. Do not
  force-add them unless the user changes that policy. Explain when a push contains only ignore
  rules rather than the ignored files themselves.
- Keep `AGENTS.md` and `CLAUDE.md` consistent; `CLAUDE.md` delegates shared guidance to this file.
