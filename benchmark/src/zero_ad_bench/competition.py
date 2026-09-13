"""Preregistered two-seat matches between external agents, reported by matchup and side.

A competition manifest names a match scenario (two external seats, no built-in AI), seed
splits, and pairing rules. Participants are controller specifications given at plan time; a
model participant is pinned by the content hash of its experiment configuration. Every match
is one episode of the ordinary multi-seat scheduler with a separate controller instance per
seat, so a mirror match runs a participant against a fresh copy of itself.
"""

import copy
import json
from pathlib import Path

from zero_ad_bench import (
    ARTIFACT_SCHEMA_VERSION,
    PACKAGE_VERSION,
    PROTOCOL_VERSION,
    analysis,
    planning,
    provenance,
    report,
)
from zero_ad_bench.agents import make_controller
from zero_ad_bench.engine import DEFAULT_ENGINE, EngineError, EngineProcess
from zero_ad_bench.environment import Episode, RunOptions
from zero_ad_bench.experiment import git_commit, load_suite
from zero_ad_bench.model_agent import SCAFFOLD_VERSION, ModelController
from zero_ad_bench.providers import make_provider
from zero_ad_bench.telemetry import read_jsonl, sha256_file, utc_now, write_json_atomic


MATCH_LABELS = ("win", "draw", "loss")
SEATS = ("1", "2")


def parse_participant(text):
    """Parse `id=controller[:config]`, e.g. `muse=model:benchmark/experiments/x.json`."""
    identifier, _, spec = text.partition("=")
    if not identifier or not spec:
        raise ValueError(f"Participant {text!r} must look like id=controller[:config]")
    controller, _, config = spec.partition(":")
    if controller == "model" and not config:
        raise ValueError(f"Participant {identifier!r} needs a configuration path after model:")
    if controller != "model" and config:
        raise ValueError(f"Participant {identifier!r}: only model controllers take a path")
    return identifier, {"controller": controller, "config": config or None}


def parse_matchups(text, participants):
    """Parse `a:b,a:c`; a participant against itself is a mirror matchup."""
    pairs = []
    for item in text.split(","):
        first, _, second = item.partition(":")
        if first not in participants or second not in participants:
            raise ValueError(f"Matchup {item!r} names an unknown participant")
        pairs.append((first, second))
    return pairs


def participant_summary(participant):
    """Return what the preregistration records: the controller and its config by hash."""
    entry = dict(participant)
    if participant.get("config"):
        entry["config_sha256"] = sha256_file(participant["config"])
        entry["resolved_config"] = json.loads(Path(participant["config"]).read_text())
        entry["config_id"] = entry["resolved_config"].get("id")
    return entry


def build_match_plan(  # noqa: PLR0913 - explicit operator inputs are pinned independently
    competition_path,
    split,
    participants,
    matchups,
    *,
    trials_per_pair=None,
    scenario_ids=None,
    engine=DEFAULT_ENGINE,
    options=None,
    mod_sources=None,
    seed_file=None,
):
    """Build the match preregistration: everything that determines the schedule, hashed first."""
    competition, scenarios, path = load_suite(competition_path)
    seeds = planning.seeds_for_split(competition, split, seed_file)
    runtime = planning.runtime_options(options, engine=engine, mod_sources=mod_sources)
    if split != "development" and runtime["turn_limit_override"] is not None:
        raise ValueError("Scored runs cannot override the registered horizon")
    resolved_participants = {name: participant_summary(p) for name, p in participants.items()}
    configs = [
        p["resolved_config"] for p in resolved_participants.values() if p["controller"] == "model"
    ]
    planning.episode_reservation(configs)
    planning.validate_experiment_budget(
        runtime["experiment_budget"], paid=any(c["provider"]["kind"] != "mock" for c in configs)
    )
    pairing = competition.get("pairing", {})
    trials = trials_per_pair or int(pairing.get("trials_per_pair", 1))
    swap = bool(pairing.get("swap_seats", True))
    ordered = list(matchups)
    if pairing.get("mirror_first", True):
        ordered = [m for m in ordered if m[0] == m[1]] + [m for m in ordered if m[0] != m[1]]
    chosen = [s for s in competition["scenarios"] if scenario_ids is None or s in scenario_ids]
    for scenario_id in chosen:
        if len(scenarios[scenario_id].external_seats()) != 2:
            raise ValueError(f"{scenario_id} is not a two-seat match scenario")
    offset = competition.get("ai_seed_offset", 1000)
    matches = []
    for scenario_id in chosen:
        for first, second in ordered:
            for seed in seeds:
                for trial in range(trials):
                    sides = [(1, first, second)]
                    if first != second and swap:
                        sides.append((2, second, first))
                    for side, seat_one, seat_two in sides:
                        matchup = f"{first}-vs-{second}"
                        matches.append(
                            {
                                "match_id": f"{scenario_id}/{seed}/{matchup}/{side}/{trial}",
                                "seed_pair": f"{scenario_id}/{seed}/{matchup}/{trial}",
                                "scenario": scenario_id,
                                "seed": seed,
                                "ai_seed": seed + offset,
                                "matchup": matchup,
                                "mirror": first == second,
                                "side": side,
                                "trial": trial,
                                "participants": {"1": seat_one, "2": seat_two},
                            }
                        )
    plan = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "preregistered_utc": utc_now(),
        "competition": {
            "id": competition["id"],
            "path": str(path),
            "sha256": sha256_file(path),
            "frozen": competition.get("frozen"),
        },
        "scenarios": {
            s: {
                "sha256": sha256_file(path.parent / f"{s}.json"),
                "schedule": scenarios[s].describe()["schedule"],
                "objective": scenarios[s].describe()["objective"],
                "family": scenarios[s].family,
                "resolved": scenarios[s].describe(),
            }
            for s in chosen
        },
        "split": split,
        "seeds": seeds,
        "participants": resolved_participants,
        "matchups": [f"{a}-vs-{b}" for a, b in ordered],
        "pairing": {**pairing, "trials_per_pair": trials, "swap_seats": swap},
        "scheduling": competition.get("scheduling", {}),
        "timeout_policy": competition.get("timeout_policy", {}),
        "result_rules": competition.get("result_rules", {}),
        "match_count": len(matches),
        "matches": matches,
        "options": runtime,
        "inputs": provenance.capture(
            list(dict.fromkeys(m for s in chosen for m in scenarios[s].mods)), mod_sources, engine
        ),
        "versions": {
            **competition.get("versions", {}),
            "runner": PACKAGE_VERSION,
            "protocol": PROTOCOL_VERSION,
            "scaffold": SCAFFOLD_VERSION,
            "engine_binary_sha256": sha256_file(engine) if Path(engine).is_file() else None,
            "git_commit": git_commit(provenance.ROOT),
            "runtime": provenance.runtime_versions(),
        },
        "analysis": competition.get("analysis", {}),
        "exclusion_rules": (
            "matches whose episode status is not completed or whose sides are undecided are "
            "invalid runs: excluded from every denominator and listed"
        ),
    }
    return plan, scenarios


def make_participant_controller(participant, scenario, seed):
    spec = participant["controller"]
    if spec == "model":
        config = copy.deepcopy(participant.get("resolved_config"))
        if config is None:
            config = json.loads(Path(participant["config"]).read_text())
        return ModelController(make_provider(config["provider"]), config)
    if spec == "scripted":
        spec = scenario.baseline
    if spec == "random":
        return make_controller(f"random:{seed}")
    return make_controller(spec)


def seat_costs(directory, seats):
    """Provider-reported cost per seat; None for a seat whose provider reported none."""
    calls, _ = read_jsonl(directory / "model-calls.jsonl")
    calls = report.provider_attempts(calls)
    costs = {}
    for seat in seats:
        rows = [c for c in calls if c.get("kind") == "model" and c.get("seat") == seat]
        if not rows:
            continue
        if any(c.get("cost_usd") is None for c in rows):
            costs[str(seat)] = None
        else:
            costs[str(seat)] = round(sum(c["cost_usd"] for c in rows), 6)
    return costs or None


def run_match(  # noqa: PLR0913 - one keyword per operator setting
    match,
    scenario,
    participants,
    output_root,
    *,
    engine=DEFAULT_ENGINE,
    decision_deadline_s=30.0,
    process_deadline_s=1800,
    mod_sources=None,
    turn_limit_override=None,
):
    """Play one match: two fresh controller instances, one engine, one episode, one row."""
    scenario = scenario.with_seeds(match["seed"], match["ai_seed"])
    if turn_limit_override is not None:
        # Development runs only; the preregistration records the shortened horizon.
        scenario.turn_limit = int(turn_limit_override)
    seats = scenario.external_seats()
    label = match.get("attempt_id") or match["match_id"].replace("/", "_")
    row = {
        **match,
        "controllers": {},
        "episode": None,
        "status": "failed",
        "result": "invalid",
        "sides": None,
        "administrative": None,
        "terminal_reason": None,
        "final_turn": None,
        "cost_usd": None,
        "failure": None,
    }
    process = None
    episode = None
    try:
        controllers = {
            seat: make_participant_controller(
                participants[match["participants"][str(seat)]], scenario, match["seed"]
            )
            for seat in seats
        }
        row["controllers"] = {str(seat): controllers[seat].name for seat in seats}
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
            experiment_id=f"{match['scenario']}:{match['matchup']}",
            label=match["match_id"],
            attempt_id=match.get("attempt_id"),
            turn_limit_override=turn_limit_override,
        )
        episode = Episode(
            scenario, controllers, process, output_root / "episodes", options, mod_sources
        )
        result = episode.run()
        directory = episode.artifacts.directory
        objective = result.get("objective") or {}
        administrative = result.get("administrative") or {}
        row.update(
            {
                "episode": str(directory.relative_to(output_root)),
                "status": result["status"],
                "result": result["result"],
                "sides": objective.get("sides"),
                "administrative": {
                    seat: (entry or {}).get("kind") for seat, entry in administrative.items()
                },
                "terminal_reason": result.get("terminal_reason"),
                "final_turn": result.get("final_turn"),
                "cost_usd": seat_costs(directory, seats),
                "failure": result.get("failure"),
            }
        )
    except EngineError as exc:
        row["failure"] = {**exc.record(), "stage": "engine"}
    except Exception as exc:  # noqa: BLE001 - a runner defect is accounted for, not hidden
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
            row["budget_consumed"] = match.get("reservation", row["budget_consumed"])
        if process is not None:
            process.close()
    return row


def valid_matches(rows):
    return [r for r in rows if r["status"] == "completed" and r.get("sides")]


def perspectives(rows, participant):
    """One row per valid match the participant played: its seat, outcome, and seed pair."""
    out = []
    for r in valid_matches(rows):
        administrative = r.get("administrative") or {}
        for seat, who in r["participants"].items():
            if who != participant:
                continue
            out.append(
                {
                    "match_id": r["match_id"],
                    "seed": r["seed"],
                    "seed_pair": r["seed_pair"],
                    "seat": seat,
                    "outcome": r["sides"][seat],
                    "administrative": bool(administrative.get("1") or administrative.get("2")),
                }
            )
    return out


def _score(perspective_rows):
    wins = sum(1 for p in perspective_rows if p["outcome"] == "win")
    draws = sum(1 for p in perspective_rows if p["outcome"] == "draw")
    return (wins + 0.5 * draws) / len(perspective_rows)


def outcome_counts(perspective_rows, *, interval=False):
    counts = {
        label: sum(1 for p in perspective_rows if p["outcome"] == label) for label in MATCH_LABELS
    }
    counts["administrative"] = {
        label: sum(1 for p in perspective_rows if p["outcome"] == label and p["administrative"])
        for label in MATCH_LABELS
    }
    counts["valid"] = len(perspective_rows)
    counts["match_score"] = _score(perspective_rows) if perspective_rows else None
    if interval:
        counts["ci95"] = (
            analysis.bootstrap_ci(perspective_rows, _score) if perspective_rows else None
        )
    return counts


def summarize_matches(rows):
    """Outcomes per matchup: by participant (with seed-pair bootstrap) and by seat."""
    summary = {"matchups": {}, "accounting": analysis.accounting(rows), "rating": None}
    for matchup in sorted({r["matchup"] for r in rows}):
        subset = [r for r in rows if r["matchup"] == matchup]
        valid = valid_matches(subset)
        names = []
        for r in subset:
            for who in r["participants"].values():
                if who not in names:
                    names.append(who)
        entry = {
            "participants": names,
            "mirror": subset[0]["mirror"],
            "attempted": len(subset),
            "valid": len(valid),
            "invalid": len(subset) - len(valid),
            "seed_pairs": len({r["seed_pair"] for r in valid}),
            "by_participant": {},
            "by_seat": {},
        }
        for who in names:
            rows_for = perspectives(valid, who)
            counts = outcome_counts(rows_for, interval=True)
            counts["by_seat"] = {
                seat: outcome_counts([p for p in rows_for if p["seat"] == seat]) for seat in SEATS
            }
            entry["by_participant"][who] = counts
        for seat in SEATS:
            seat_rows = [
                {
                    "seed": r["seed"],
                    "outcome": r["sides"][seat],
                    "administrative": bool(
                        (r.get("administrative") or {}).get("1")
                        or (r.get("administrative") or {}).get("2")
                    ),
                }
                for r in valid
            ]
            entry["by_seat"][seat] = outcome_counts(seat_rows)
        summary["matchups"][matchup] = entry
    return summary


def run_match_plan(  # noqa: PLR0913 - explicit overrides must agree with the frozen plan
    plan,
    scenarios,
    participants,
    output_root,
    *,
    engine=None,
    decision_deadline_s=None,
    process_deadline_s=None,
    mod_sources=None,
    turn_limit_override=None,
    resume=True,
):
    """Play the resolved matches with immutable inputs and durable bounded attempts."""
    runtime = planning.validate_runtime(
        plan,
        {
            "engine": engine,
            "decision_deadline_s": decision_deadline_s,
            "process_deadline_s": process_deadline_s,
            "mod_sources": mod_sources,
            "turn_limit_override": turn_limit_override,
        },
    )
    if {name: participant_summary(p) for name, p in participants.items()} != plan["participants"]:
        raise ValueError("Participants differ from preregistration")
    resolved = copy.deepcopy(plan["participants"])
    output_root = Path(output_root)

    def reserve(match):
        return planning.episode_reservation(
            [
                resolved[name]["resolved_config"]
                for name in match["participants"].values()
                if resolved[name]["controller"] == "model"
            ]
        )

    def execute(match):
        return run_match(
            match,
            scenarios[match["scenario"]],
            resolved,
            output_root,
            engine=runtime["engine"],
            decision_deadline_s=runtime["decision_deadline_s"],
            process_deadline_s=runtime["process_deadline_s"],
            mod_sources=runtime["mod_sources"],
            turn_limit_override=runtime["turn_limit_override"],
        )

    def finish(rows, plan_hash):
        summary = summarize_matches(rows)
        write_json_atomic(output_root / "competition-summary.json", summary)
        (output_root / "competition-report.md").write_text(
            render_match_report(plan, rows, summary, plan_hash)
        )
        return {"accounting": summary["accounting"]}

    rows, _plan_hash = planning.execute_schedule(
        plan,
        scenarios,
        output_root,
        stream="competition",
        key="match_id",
        schedule=plan["matches"],
        run_one=execute,
        reserve=reserve,
        resume=resume,
        finish=finish,
    )
    summary = json.loads((output_root / "competition-summary.json").read_text())
    return rows, summary


def _wdl(counts):
    administrative = counts["administrative"]
    return (
        f"W{counts['win']} D{counts['draw']} L{counts['loss']} "
        f"(administrative W{administrative['win']} D{administrative['draw']} "
        f"L{administrative['loss']})"
    )


def render_match_report(plan, rows, summary, plan_hash):
    account = summary["accounting"]
    lines = [
        f"# Competition report: {plan['competition']['id']} / {plan['split']} split",
        "",
        (
            f"Preregistration `{plan_hash}` ({plan['match_count']} matches, {len(rows)} "
            f"attempted). Versions: {json.dumps(plan['versions'], sort_keys=True)}"
        ),
        "",
        "Participants: "
        + ", ".join(
            f"`{name}` = {p['controller']}"
            + (
                f" ({p.get('config_id')}, config `{p['config_sha256'][:12]}`)"
                if p.get("config")
                else ""
            )
            for name, p in plan["participants"].items()
        ),
        "",
        "## Accounting",
        "",
        (
            f"Attempted {account['attempted']}, valid {account['valid']}, excluded "
            f"{account['excluded']} ({json.dumps(account['exclusion_reasons'], sort_keys=True)}); "
            f"by status {json.dumps(account['by_status'], sort_keys=True)}."
        ),
        "",
        "## Outcomes by matchup and participant",
        "",
        (
            "| Matchup | Participant | Valid / attempted | Seed pairs | W D L (administrative) "
            "| Match score | 95% CI (seed-pair bootstrap) |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for matchup, entry in summary["matchups"].items():
        for who, counts in entry["by_participant"].items():
            ci = counts.get("ci95")
            interval = "n/a" if not ci else f"[{ci['low']:.2f}, {ci['high']:.2f}]"
            score = "n/a" if counts["match_score"] is None else f"{counts['match_score']:.2f}"
            lines.append(
                f"| {matchup} | {who} | {entry['valid']}/{entry['attempted']} | "
                f"{entry['seed_pairs']} | {_wdl(counts)} | {score} | {interval} |"
            )
    lines += [
        "",
        "## Outcomes by side",
        "",
        (
            "| Matchup | Seat 1 W D L | Seat 2 W D L | Participant as seat 1 "
            "| Participant as seat 2 |"
        ),
        "| --- | --- | --- | --- | --- |",
    ]
    for matchup, entry in summary["matchups"].items():
        as_one = "; ".join(
            f"{who}: {_wdl(c['by_seat']['1'])}" for who, c in entry["by_participant"].items()
        )
        as_two = "; ".join(
            f"{who}: {_wdl(c['by_seat']['2'])}" for who, c in entry["by_participant"].items()
        )
        lines.append(
            f"| {matchup} | {_wdl(entry['by_seat']['1'])} | {_wdl(entry['by_seat']['2'])} | "
            f"{as_one} | {as_two} |"
        )
    excluded = [r for r in rows if r not in valid_matches(rows)]
    lines += ["", "## Excluded and failed attempts", ""]
    if excluded:
        for r in excluded:
            lines.append(
                f"- {r['match_id']}: status {r['status']}, result {r['result']}, failure "
                f"{json.dumps(r.get('failure'))}, episode {r.get('episode')}"
            )
    else:
        lines.append("None.")
    lines += [
        "",
        "## Rules and notes",
        "",
        (
            "Batches are committed together and applied in ascending seat order; each seat's "
            "controller decides concurrently under one shared deadline from the same completed "
            "turn. In-game results come from the engine's player states; a forfeit or budget "
            "stop is an administrative loss and the surviving seat an administrative win, and "
            "forfeits at the same decision are an administrative draw. Invalid runs are engine "
            "or runner failures and are never a defeat. Match score is (wins + 0.5 draws) / "
            "valid matches; intervals are percentile bootstraps clustered by seed pair. In a "
            "mirror matchup the participant holds both seats, so its counts cover every "
            "match twice and the by-side rows are the informative ones. No rating is "
            "computed: outcomes are published by side and matchup."
        ),
        "",
    ]
    return "\n".join(lines)
