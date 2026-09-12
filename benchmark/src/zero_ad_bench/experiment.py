"""Preregistered trial sets: freeze the plan, run every episode, account for all of them."""

import json
import subprocess
from pathlib import Path

from zero_ad_bench import ARTIFACT_SCHEMA_VERSION, PACKAGE_VERSION, PROTOCOL_VERSION, analysis
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


def build_plan(
    suite_path,
    split,
    controller_specs,
    *,
    experiment_config=None,
    trials_per_seed=1,
    scenario_ids=None,
    engine=DEFAULT_ENGINE,
    options=None,
):
    """Build the preregistration: everything determining the trial set, hashed before any run."""
    suite, scenarios, path = load_suite(suite_path)
    seeds = suite["seed_splits"][split]
    if isinstance(seeds, dict):
        message = f"Split {split!r} is not held in the repository; supply its seed file"
        raise TypeError(message)
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
    root = path.resolve().parents[1]
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
            }
            for s in chosen
        },
        "split": split,
        "seeds": seeds,
        "controllers": controller_specs,
        "trials_per_seed": trials_per_seed,
        "trial_count": len(trials),
        "trials": trials,
        "experiment_config": experiment_config,
        "options": options or {},
        "versions": {
            **suite.get("versions", {}),
            "runner": PACKAGE_VERSION,
            "protocol": PROTOCOL_VERSION,
            "scaffold": SCAFFOLD_VERSION,
            "engine_binary_sha256": sha256_file(engine) if Path(engine).is_file() else None,
            "git_commit": git_commit(root),
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


def run_plan(  # noqa: PLR0913 - one keyword per operator setting
    plan,
    scenarios,
    output_root,
    *,
    experiment_config=None,
    engine=DEFAULT_ENGINE,
    decision_deadline_s=30.0,
    process_deadline_s=1800,
    mod_sources=None,
    resume=True,
):
    """Run every trial in the plan, appending one accounting row per attempt as it finishes."""
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    plan_path = output_root / "preregistration.json"
    if plan_path.is_file():
        existing = json.loads(plan_path.read_text())
        if (
            existing["trials"] != plan["trials"]
            or existing["suite"]["sha256"] != plan["suite"]["sha256"]
        ):
            raise ValueError(
                "An earlier preregistration in this directory differs; use a new directory"
            )
    else:
        write_json_atomic(plan_path, plan)
    plan_hash = sha256_file(plan_path)
    rows_path = output_root / "experiment.jsonl"
    done = {}
    if resume and rows_path.is_file():
        for line in rows_path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                done[row["trial_id"]] = row
    manifest = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "preregistration_sha256": plan_hash,
        "status": "running",
        "started_utc": utc_now(),
        "finished_utc": None,
    }
    write_json_atomic(output_root / "experiment-manifest.json", manifest)
    rows = list(done.values())
    with rows_path.open("a", encoding="utf-8") as handle:
        for trial in plan["trials"]:
            if trial["trial_id"] in done:
                continue
            row = run_trial(
                trial,
                scenarios[trial["scenario"]],
                output_root,
                experiment_config,
                engine,
                decision_deadline_s,
                process_deadline_s,
                mod_sources,
            )
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            rows.append(row)
    summary = analysis.summarize(rows, {s: scenarios[s].describe() for s in plan["scenarios"]})
    write_json_atomic(output_root / "experiment-summary.json", summary)
    (output_root / "experiment-report.md").write_text(
        render_report(plan, rows, summary, plan_hash)
    )
    manifest.update(
        {
            "status": "completed",
            "finished_utc": utc_now(),
            "rows": len(rows),
            "accounting": analysis.accounting(rows),
        }
    )
    write_json_atomic(output_root / "experiment-manifest.json", manifest)
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
    controller = make_trial_controller(
        trial["controller"], scenario, trial["seed"], experiment_config
    )
    episode_root = output_root / "episodes"
    label = trial["trial_id"].replace("/", "_")
    row = {
        **trial,
        "controller_name": controller.name,
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
    try:
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
        )
        episode = Episode(
            scenario,
            dict.fromkeys(scenario.external_seats(), controller) or {1: controller},
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
    finally:
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
