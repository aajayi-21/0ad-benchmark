"""Immutable execution plans and durable accounting shared by experiments and competitions."""

import copy
import fcntl
import hashlib
import json
import math
from pathlib import Path

from zero_ad_bench import provenance
from zero_ad_bench.engine import DEFAULT_ENGINE
from zero_ad_bench.model_agent import validate_budget_config
from zero_ad_bench.telemetry import sha256_file, utc_now, write_json_atomic


def seeds_for_split(suite, split, seed_file=None):
    seeds = suite["seed_splits"][split]
    if isinstance(seeds, dict):
        if seed_file is None:
            raise ValueError(f"Split {split!r} requires an operator seed file")
        data = Path(seed_file).read_bytes()
        if hashlib.sha256(data).hexdigest() != seeds["sha256"]:
            raise ValueError("Private seed file does not match the preregistered SHA-256")
        values = json.loads(data)
        if not isinstance(values, list) or len(values) != seeds["count"]:
            raise ValueError("Private seed file has the wrong seed count")
        seeds = values
    elif seed_file is not None:
        raise ValueError("A seed file is only accepted for a committed private split")
    if (
        not seeds
        or any(isinstance(s, bool) or not isinstance(s, int) or not 0 <= s < 2**32 for s in seeds)
        or len(set(seeds)) != len(seeds)
    ):
        raise ValueError("Seed bundles require distinct unsigned 32-bit integers")
    return list(seeds)


def runtime_options(options=None, *, engine=DEFAULT_ENGINE, mod_sources=None):
    values = {
        "decision_deadline_s": 30.0,
        "process_deadline_s": 1800.0,
        "turn_limit_override": None,
        "max_attempts": 2,
        "experiment_budget": None,
        **(options or {}),
        "engine": str(Path(engine).resolve()),
        "mod_sources": {k: str(Path(v).resolve()) for k, v in (mod_sources or {}).items()},
    }
    for name in ("decision_deadline_s", "process_deadline_s"):
        value = values[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be finite and positive")
    if (
        isinstance(values["max_attempts"], bool)
        or not isinstance(values["max_attempts"], int)
        or not 1 <= values["max_attempts"] <= 10
    ):
        raise ValueError("max_attempts must be between 1 and 10")
    horizon = values["turn_limit_override"]
    if horizon is not None and (
        isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 12000
    ):
        raise ValueError("turn_limit_override must be an integer from 1 to 12000")
    return values


def validate_runtime(plan, overrides):
    runtime = plan["options"]
    for name, value in overrides.items():
        if value is None:
            continue
        if name == "engine":
            value = str(Path(value).resolve())
        elif name == "mod_sources":
            value = {k: str(Path(v).resolve()) for k, v in value.items()}
        if runtime[name] != value:
            raise ValueError(f"Runtime {name} differs from preregistration")
    return copy.deepcopy(runtime)


def canonical(plan):
    return {k: v for k, v in plan.items() if k != "preregistered_utc"}


def validate_inputs(plan, scenarios):
    for name, expected in plan["scenarios"].items():
        if scenarios[name].describe() != expected["resolved"]:
            raise ValueError(f"Resolved scenario {name} differs from preregistration")
        if scenarios[name].path and sha256_file(scenarios[name].path) != expected["sha256"]:
            raise ValueError(f"Scenario file {name} changed after preregistration")
    runtime = plan["options"]
    if sha256_file(runtime["engine"]) != plan["versions"]["engine_binary_sha256"]:
        raise ValueError("Engine binary changed after preregistration")
    if provenance.runtime_versions() != plan["versions"]["runtime"]:
        raise ValueError("Python runtime or provider SDK changed after preregistration")
    mods = list(dict.fromkeys(m for name in plan["scenarios"] for m in scenarios[name].mods))
    current = provenance.capture(mods, runtime["mod_sources"], runtime["engine"])
    if current != plan["inputs"]:
        raise ValueError("Gameplay inputs changed after preregistration")
    if plan["split"] != "development" and current["lfs_pointers"]:
        raise ValueError("Scored runs require materialized gameplay assets, not LFS pointers")
    if plan["split"] != "development":
        for name in plan["scenarios"]:
            if any(
                v is None
                for v in scenarios[name]
                .content_hashes(runtime["mod_sources"], runtime["engine"])
                .values()
            ):
                raise ValueError(f"Scored scenario {name} has unresolved declared assets")
    return mods


def open_plan(directory, plan, scenarios):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "preregistration.json"
    if path.is_file() and canonical(json.loads(path.read_text())) != canonical(plan):
        raise ValueError("An earlier preregistration differs; use a new output directory")
    mods = validate_inputs(plan, scenarios)
    if not path.is_file():
        write_json_atomic(path, plan)
    provenance.archive(
        directory, plan["inputs"], mods, plan["options"]["mod_sources"], plan["options"]["engine"]
    )
    return sha256_file(path)


def validate_experiment_budget(budget, *, paid):
    if budget is None and not paid:
        return
    if not isinstance(budget, dict):
        raise TypeError("Paid batches require an experiment token and dollar budget")
    for name in ("tokens", "cost_usd"):
        value = budget.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"Experiment {name} ceiling must be finite and positive")
        if name == "tokens" and not isinstance(value, int):
            raise ValueError("Experiment tokens ceiling must be an integer")


def episode_reservation(configs):
    result = {"tokens": 0, "cost_usd": 0.0}
    for config in configs:
        validate_budget_config(config, paid=config["provider"]["kind"] != "mock")
        limits = config.get("budgets") or {}
        result["tokens"] += limits.get("episode_tokens") or 0
        result["cost_usd"] += limits.get("episode_cost_usd") or 0
    return result


class AttemptJournal:
    """Per-attempt atomic records are authoritative; JSONL is their completed-row projection."""

    def __init__(self, directory, stream, key, *, resume=True):
        self.directory = Path(directory)
        self.attempts = self.directory / "attempts"
        self.attempts.mkdir(exist_ok=True)
        self.path = self.directory / f"{stream}.jsonl"
        self.key = key
        files = sorted(self.attempts.glob("*.json"))
        if files and not resume:
            raise ValueError("Use a new directory to rerun a preregistered schedule")
        self.rows = []
        for path in files:
            row = json.loads(path.read_text())
            if row["status"] == "running":
                row.update(
                    status="interrupted",
                    result="invalid",
                    finished_utc=utc_now(),
                    failure={
                        "kind": "interrupted",
                        "message": "Previous runner did not finalize this attempt",
                    },
                    budget_consumed=row["reservation"],
                )
                # Episodes carry the attempt ID even if the runner died before publishing its row.
                for manifest in (self.directory / "episodes").glob("*/manifest.json"):
                    if (
                        json.loads(manifest.read_text()).get("options", {}).get("attempt_id")
                        == row["attempt_id"]
                    ):
                        row["episode"] = str(manifest.parent.relative_to(self.directory))
                        break
                write_json_atomic(path, row)
            self.rows.append(row)
        if self.path.is_file() and not files and self.path.stat().st_size:
            raise ValueError("Accounting rows without attempt identities require a new directory")
        self._project()

    def _project(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in self.rows))
        temporary.replace(self.path)

    def attempts_for(self, trial):
        return [r for r in self.rows if r[self.key] == trial[self.key]]

    def finished(self, trial, limit):
        rows = self.attempts_for(trial)
        return len(rows) >= limit or any(
            r["status"] == "completed" and r["result"] != "invalid" for r in rows
        )

    def can_reserve(self, reservation, ceiling):
        if ceiling is None:
            return True
        return all(
            sum(r["budget_consumed"][name] for r in self.rows) + reservation[name] <= ceiling[name]
            for name in ("tokens", "cost_usd")
        )

    def begin(self, trial, reservation):
        attempt = len(self.attempts_for(trial)) + 1
        identity = hashlib.sha256(f"{trial[self.key]}:{attempt}".encode()).hexdigest()[:24]
        row = {
            **trial,
            "attempt": attempt,
            "attempt_id": identity,
            "sequence": len(self.rows),
            "started_utc": utc_now(),
            "finished_utc": None,
            "status": "running",
            "result": "invalid",
            "episode": None,
            "failure": None,
            "reservation": reservation,
            "budget_consumed": reservation,
            "cost_usd": None,
            "achieved_turn": None,
            "administrative": None,
            "sides": None,
        }
        path = self.attempts / f"{row['sequence']:08d}-{identity}.json"
        write_json_atomic(path, row)
        return row, path

    def finish(self, started, path, result):
        row = {**started, **result, "finished_utc": utc_now()}
        row.setdefault("budget_consumed", started["reservation"])
        write_json_atomic(path, row)
        self.rows.append(row)
        self._project()
        return row


def execute_schedule(plan, scenarios, directory, **kwargs):
    """Hold exclusive ownership while recovering, executing and reporting a schedule."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".runner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another runner already owns this output directory") from exc
        return _execute_schedule(plan, scenarios, directory, **kwargs)


def _execute_schedule(  # noqa: PLR0913 - shared execution with explicit adapters for each track
    plan,
    scenarios,
    directory,
    *,
    stream,
    key,
    schedule,
    run_one,
    reserve,
    resume=True,
    finish=None,
):
    """Run/resume bounded attempts. Every constructor call already has a durable identity."""
    directory = Path(directory)
    plan_hash = open_plan(directory, plan, scenarios)
    journal = AttemptJournal(directory, stream, key, resume=resume)
    manifest_path = directory / f"{stream}-manifest.json"
    manifest = {
        "schema_version": plan["schema_version"],
        "preregistration_sha256": plan_hash,
        "status": "running",
        "started_utc": utc_now(),
        "finished_utc": None,
        "scheduled": len(schedule),
        "inputs_archive_sha256": sha256_file(directory / "inputs.zip"),
    }
    write_json_atomic(manifest_path, manifest)
    status = "completed"
    failure = None
    try:
        for trial in schedule:
            while not journal.finished(trial, plan["options"]["max_attempts"]):
                validate_inputs(plan, scenarios)
                reservation = reserve(trial)
                if not journal.can_reserve(reservation, plan["options"]["experiment_budget"]):
                    status = "budget_stop"
                    break
                started, path = journal.begin(trial, reservation)
                try:
                    result = run_one(started)
                except KeyboardInterrupt:
                    result = {
                        "status": "interrupted",
                        "result": "invalid",
                        "failure": {
                            "kind": "interrupted",
                            "message": "Operator interrupted runner",
                        },
                    }
                    status = "interrupted"
                except Exception as exc:  # noqa: BLE001 - retain every failed attempt
                    result = {
                        "status": "failed",
                        "result": "invalid",
                        "failure": {
                            "kind": "runner_error",
                            "code": type(exc).__name__,
                            "message": str(exc)[:500],
                        },
                    }
                # Catch changes during even the final attempt, not just before the next one.
                try:
                    validate_inputs(plan, scenarios)
                except Exception as exc:  # noqa: BLE001 - retain this attempt before stopping
                    result.update(
                        status="failed",
                        result="invalid",
                        sides=None,
                        failure={"kind": "provenance_changed", "message": str(exc)[:500]},
                    )
                    status = "failed"
                    failure = result["failure"]
                journal.finish(started, path, result)
                if result["status"] == "interrupted":
                    status = "interrupted"
                if status != "completed":
                    break
            if status != "completed":
                break
        if finish is not None:
            manifest.update(finish(journal.rows, plan_hash))
    except BaseException as exc:
        status = "failed"
        failure = {"kind": "runner_error", "code": type(exc).__name__, "message": str(exc)[:500]}
        raise
    finally:
        manifest.update(
            status=status, failure=failure, finished_utc=utc_now(), rows=len(journal.rows)
        )
        write_json_atomic(manifest_path, manifest)
    return journal.rows, plan_hash
