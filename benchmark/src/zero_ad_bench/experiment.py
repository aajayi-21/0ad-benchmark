"""Preregistered trial sets: freeze the plan, run every episode, account for all of them."""

import copy
import json
import subprocess
from pathlib import Path

from zero_ad_bench import (
    ARTIFACT_SCHEMA_VERSION,
    PACKAGE_VERSION,
    PROTOCOL_VERSION,
    analysis,
    planning,
    provenance,
)
from zero_ad_bench.agents import make_controller
from zero_ad_bench.engine import DEFAULT_ENGINE, EngineError, EngineProcess
from zero_ad_bench.environment import Episode, RunOptions
from zero_ad_bench.model_agent import SCAFFOLD_VERSION, ModelController
from zero_ad_bench.providers import make_provider
from zero_ad_bench.scenario import Scenario
from zero_ad_bench.telemetry import sha256_file, utc_now, write_json_atomic


def load_suite(path):
    path = Path(path)
    suite = json.loads(path.read_text())
    if suite.get("schema_version") != 1:
        raise ValueError("Unsupported suite schema")
    scenarios = {}
    for scenario_id in suite["scenarios"]:
        scenario_path = path.parent / f"{scenario_id}.json"
        scenarios[scenario_id] = Scenario.load(scenario_path)
    return suite, scenarios, path


def git_commit(root):
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def build_plan(  # noqa: PLR0913 - explicit operator inputs are pinned independently
    suite_path,
    split,
    controller_specs,
    *,
    experiment_config=None,
    trials_per_seed=1,
    scenario_ids=None,
    engine=DEFAULT_ENGINE,
    options=None,
    mod_sources=None,
    seed_file=None,
):
    """Build the preregistration: everything determining the trial set, hashed before any run."""
    suite, scenarios, path = load_suite(suite_path)
    seeds = planning.seeds_for_split(suite, split, seed_file)
    runtime = planning.runtime_options(options, engine=engine, mod_sources=mod_sources)
    if split != "development" and runtime["turn_limit_override"] is not None:
        raise ValueError("Scored runs cannot override the registered horizon")
    if "model" in controller_specs:
        if experiment_config is None:
            raise ValueError("Model trials require an experiment configuration")
        planning.episode_reservation([experiment_config])
    planning.validate_experiment_budget(
        runtime["experiment_budget"],
        paid=("model" in controller_specs and experiment_config["provider"]["kind"] != "mock"),
    )
    chosen = [s for s in suite["scenarios"] if scenario_ids is None or s in scenario_ids]
    trials = []
    for scenario_id in chosen:
        for seed in seeds:
            for trial in range(trials_per_seed):
                for spec in controller_specs:
                    trials.append(
                        {
                            "trial_id": f"{scenario_id}/{seed}/{spec}/{trial}",
                            "scenario": scenario_id,
                            "seed": seed,
                            "ai_seed": seed + suite.get("ai_seed_offset", 1000),
                            "controller": spec,
                            "trial": trial,
                        }
                    )
    plan = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "preregistered_utc": utc_now(),
        "suite": {
            "id": suite["id"],
            "path": str(path),
            "sha256": sha256_file(path),
            "frozen": suite.get("frozen"),
        },
        "scenarios": {
            s: {
                "sha256": sha256_file(path.parent / f"{s}.json"),
                "schedule": scenarios[s].describe()["schedule"],
                "objective": scenarios[s].describe()["objective"],
                "family": scenarios[s].family,
                "baseline": scenarios[s].baseline,
                "resolved": scenarios[s].describe(),
            }
            for s in chosen
        },
        "split": split,
        "seeds": seeds,
        "controllers": controller_specs,
        "trials_per_seed": trials_per_seed,
        "trial_count": len(trials),
        "trials": trials,
        "experiment_config": copy.deepcopy(experiment_config),
        "options": runtime,
        "inputs": provenance.capture(
            list(dict.fromkeys(m for s in chosen for m in scenarios[s].mods)), mod_sources, engine
        ),
        "versions": {
            **suite.get("versions", {}),
            "runner": PACKAGE_VERSION,
            "protocol": PROTOCOL_VERSION,
            "scaffold": SCAFFOLD_VERSION,
            "engine_binary_sha256": sha256_file(engine) if Path(engine).is_file() else None,
            "git_commit": git_commit(provenance.ROOT),
            "runtime": provenance.runtime_versions(),
        },
        "analysis": suite.get("analysis", {}),
        "exclusion_rules": (
            "episodes whose status is not completed, or whose result is invalid or incomplete, "
            "are excluded from every denominator and listed"
        ),
    }
    return plan, scenarios


def make_trial_controller(spec, scenario, seed, experiment_config):
    if spec == "scripted":
        spec = scenario.baseline
    if spec == "model":
        if experiment_config is None:
            raise ValueError("model trials need an experiment configuration")
        return ModelController(make_provider(experiment_config["provider"]), experiment_config)
    if spec == "random":
        return make_controller(f"random:{seed}")
    return make_controller(spec)


def run_plan(  # noqa: PLR0913 - explicit operator overrides must match the frozen plan
    plan,
    scenarios,
    output_root,
    *,
    experiment_config=None,
    engine=None,
    decision_deadline_s=None,
    process_deadline_s=None,
    mod_sources=None,
    resume=True,
):
    """Execute the resolved preregistration and report all attempts, including bounded retries."""
    runtime = planning.validate_runtime(
        plan,
        {
            "engine": engine,
            "decision_deadline_s": decision_deadline_s,
            "process_deadline_s": process_deadline_s,
            "mod_sources": mod_sources,
        },
    )
    if experiment_config is not None and experiment_config != plan["experiment_config"]:
        raise ValueError("Model configuration differs from preregistration")
    config = copy.deepcopy(plan["experiment_config"])
    output_root = Path(output_root)

    def reserve(trial):
        count = len(scenarios[trial["scenario"]].external_seats())
        return planning.episode_reservation(
            [config] * count if trial["controller"] == "model" else []
        )

    def execute(trial):
        return run_trial(
            trial,
            scenarios[trial["scenario"]],
            output_root,
            config,
            runtime["engine"],
            runtime["decision_deadline_s"],
            runtime["process_deadline_s"],
            runtime["mod_sources"],
        )

    def finish(rows, plan_hash):
        summary = analysis.summarize(rows, {s: scenarios[s].describe() for s in plan["scenarios"]})
        write_json_atomic(output_root / "experiment-summary.json", summary)
        (output_root / "experiment-report.md").write_text(
            render_report(plan, rows, summary, plan_hash)
        )
        return {"accounting": analysis.accounting(rows)}

    rows, _plan_hash = planning.execute_schedule(
        plan,
        scenarios,
        output_root,
        stream="experiment",
        key="trial_id",
        schedule=plan["trials"],
        run_one=execute,
        reserve=reserve,
        resume=resume,
        finish=finish,
    )
    summary = json.loads((output_root / "experiment-summary.json").read_text())
    return rows, summary


def run_trial(
    trial,
    scenario,
    output_root,
    experiment_config,
    engine,
    decision_deadline_s,
    process_deadline_s,
    mod_sources,
):
    scenario = scenario.with_seeds(trial["seed"], trial["ai_seed"])
    episode_root = output_root / "episodes"
    label = trial.get("attempt_id") or trial["trial_id"].replace("/", "_")
    row = {
        **trial,
        "controller_name": None,
        "episode": None,
        "status": "failed",
        "result": "invalid",
        "terminal_reason": None,
        "final_turn": None,
        "achieved_turn": None,
        "administrative": None,
        "cost_usd": None,
        "model_attempts": None,
        "failure": None,
    }
    process = None
    episode = None
    try:
        # One instance per seat: a controller's private state must never be shared across seats.
        controllers = {
            seat: make_trial_controller(
                trial["controller"], scenario, trial["seed"], experiment_config
            )
            for seat in (scenario.external_seats() or [1])
        }
        row["controller_name"] = next(iter(controllers.values())).name
        process = EngineProcess(
            output_root / "engines" / label,
            engine=engine,
            mods=scenario.mods,
            mod_sources=mod_sources,
            process_deadline_s=process_deadline_s,
        )
        process.ready()
        options = RunOptions(
            decision_deadline_s=decision_deadline_s,
            experiment_id=f"{trial['scenario']}:{trial['controller']}",
            label=trial["trial_id"],
            attempt_id=trial.get("attempt_id"),
        )
        episode = Episode(
            scenario,
            controllers,
            process,
            episode_root,
            options,
            mod_sources,
        )
        result = episode.run()
        usage = result.get("model_usage") or {}
        administrative = result.get("administrative") or {}
        row.update(
            {
                "episode": str(episode.artifacts.directory.relative_to(output_root))
                if episode.artifacts
                else None,
                "status": result["status"],
                "result": result["result"],
                "terminal_reason": result["terminal_reason"],
                "final_turn": result["final_turn"],
                "achieved_turn": (result.get("objective") or {}).get("achieved_turn"),
                "administrative": next((v for v in administrative.values() if v), None),
                "cost_usd": None if usage.get("cost_unavailable") else usage.get("cost_usd"),
                "model_attempts": usage.get("attempts"),
                "failure": result.get("failure"),
                "player_states": result.get("player_states"),
            }
        )
        if scenario.family == "full_game" and result["status"] == "completed":
            row["result"] = full_game_result(result)
    except EngineError as exc:
        row["failure"] = exc.record()
    except Exception as exc:  # noqa: BLE001 - account for construction and runner defects
        row["failure"] = {
            "kind": "runner_error",
            "code": type(exc).__name__,
            "message": str(exc)[:500],
        }
    finally:
        if episode is not None and episode.artifacts is not None:
            row["episode"] = str(episode.artifacts.directory.relative_to(output_root))
        result = episode.result if episode is not None else None
        usage = (result or {}).get("model_usage") or {}
        row["budget_consumed"] = {
            "tokens": usage.get("budget_tokens", 0),
            "cost_usd": usage.get("budget_cost_usd", 0),
        }
        if episode is not None and episode.result is None:
            row["budget_consumed"] = trial.get("reservation", row["budget_consumed"])
        if process is not None:
            process.close()
    return row


def full_game_result(result):
    states = result.get("player_states") or {}
    if result.get("administrative") and any(result["administrative"].values()):
        return "loss"
    if states.get("1") == "won":
        return "win"
    if states.get("1") == "defeated":
        return "loss"
    return "draw" if result.get("truncated") else "invalid"


def render_report(plan, rows, summary, plan_hash):
    lines = [
        f"# Experiment report: {plan['suite']['id']} / {plan['split']} split",
        "",
        (
            f"Preregistration `{plan_hash}` ({plan['trial_count']} trials, {len(rows)} "
            f"attempted). Versions: {json.dumps(plan['versions'], sort_keys=True)}"
        ),
        "",
        "## Accounting",
        "",
    ]
    account = analysis.accounting(rows)
    lines.append(
        f"Attempted {account['attempted']}, valid {account['valid']}, excluded "
        f"{account['excluded']} ({json.dumps(account['exclusion_reasons'], sort_keys=True)}); "
        f"by status {json.dumps(account['by_status'], sort_keys=True)}."
    )
    lines += ["", "## Results by scenario and controller", ""]
    lines.append(
        "| scenario | controller | valid/attempted | success rate | 95% CI (seed bootstrap) "
        "| median turn (censored) | cost USD |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for scenario_id, controllers in summary.items():
        for controller, entry in controllers.items():
            acc = entry["accounting"]
            cost = entry["cost"]["total_usd"]
            if "match" in entry:
                m = entry["match"]
                lines.append(
                    f"| {scenario_id} | {controller} | {acc['valid']}/{acc['attempted']} | match "
                    f"score {m['match_score']} (W{m['win']} D{m['draw']} L{m['loss']}) | - | - | "
                    f"{cost} |"
                )
                continue
            s = entry["success"]
            ci = s["ci95"]
            t = entry["time_to_success"]
            rate = (
                "n/a" if s["rate"] is None else f"{s['rate']:.2f} ({s['successes']}/{s['valid']})"
            )
            interval = (
                "n/a"
                if not ci
                else f"[{ci['low']:.2f}, {ci['high']:.2f}] over {ci['clusters']} seeds"
            )
            median = "n/a" if t["median_turn"] is None else str(t["median_turn"])
            lines.append(
                f"| {scenario_id} | {controller} | {acc['valid']}/{acc['attempted']} | {rate} | "
                f"{interval} | {median} ({t['censored_at_horizon']} censored) | {cost} |"
            )
    excluded = [r for r in rows if r not in analysis.valid_rows(rows)]
    lines += ["", "## Excluded and failed attempts", ""]
    if excluded:
        for r in excluded:
            lines.append(
                f"- {r['trial_id']}: status {r['status']}, result {r['result']}, failure "
                f"{json.dumps(r.get('failure'))}, episode {r.get('episode')}"
            )
    else:
        lines.append("None.")
    lines += [
        "",
        "## Notes",
        "",
        (
            "Rates use valid completed episodes only; intervals are percentile bootstraps "
            "clustered by seed; medians are omitted when half or more of the valid episodes are "
            "censored at the horizon. Costs are provider-reported when available. Each row of "
            "experiment.jsonl is one attempted episode with its artifact directory."
        ),
        "",
    ]
    return "\n".join(lines)
