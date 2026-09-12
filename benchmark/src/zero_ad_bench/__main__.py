"""Command line entry: run an episode, regenerate its report, or verify its artifacts."""

import argparse
import json
import signal
import sys
from pathlib import Path

from zero_ad_bench import PACKAGE_VERSION, investigate, report
from zero_ad_bench import experiment as experiments
from zero_ad_bench.agents import make_controller
from zero_ad_bench.engine import DEFAULT_ENGINE, EngineProcess
from zero_ad_bench.environment import Episode, RunOptions
from zero_ad_bench.model_agent import ModelController
from zero_ad_bench.providers import COMMAND_PRESETS, make_provider
from zero_ad_bench.scenario import Scenario


def _pairs(values):
    result = {}
    for item in values or []:
        key, separator, value = item.partition("=")
        if not separator:
            raise argparse.ArgumentTypeError(f"Expected NAME=VALUE, got {item}")
        result[key] = value
    return result


def run(args):
    scenario = Scenario.load(args.scenario)
    if args.turn_limit is not None:
        if not 1 <= args.turn_limit <= 12000:
            raise SystemExit("--turn-limit must be between 1 and 12000")
        scenario.turn_limit = args.turn_limit
    mod_sources = {name: Path(path) for name, path in _pairs(args.mod_source).items()}
    seats = scenario.external_seats()
    specs = _pairs(args.controller_seat)
    experiment = (
        json.loads(Path(args.experiment_config).read_text()) if args.experiment_config else None
    )

    def build(spec):
        if spec == "model":
            if experiment is None:
                raise SystemExit("--experiment-config is required for the model controller")
            return ModelController(make_provider(experiment["provider"]), experiment)
        return make_controller(spec)

    controllers = {seat: build(specs.get(str(seat), args.controller)) for seat in seats}
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    options = RunOptions(
        decision_deadline_s=args.decision_deadline,
        telemetry=not args.no_telemetry,
        save_replay=not args.no_replay,
        max_consecutive_failures=args.max_failures,
        experiment_id=experiment["id"] if experiment else args.experiment,
        label=args.label,
        turn_limit_override=args.turn_limit,
    )
    engine = EngineProcess(
        output / f"engine-{scenario.id}-{Path(args.output).name}-{id(options):x}",
        engine=args.engine,
        mods=scenario.mods,
        mod_sources=mod_sources,
        process_deadline_s=args.process_deadline,
    )
    episode = Episode(scenario, controllers, engine, output, options, mod_sources)

    def interrupt(_signal, _frame):
        episode.request_interrupt()

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        engine.ready()
        result = episode.run()
    finally:
        engine.close()
    print(
        json.dumps(
            {
                "episode": str(episode.artifacts.directory) if episode.artifacts else None,
                "status": result["status"] if result else "failed",
                "result": result["result"] if result else None,
            }
        )
    )
    return (
        {"completed": 0, "invalid": 3, "interrupted": 2}.get(result["status"], 1) if result else 1
    )


def regenerate(args):
    result = report.build_result(args.episode)
    text = report.build_report(args.episode, result)
    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        (out / "report.md").write_text(text)
        print(json.dumps({"result": result["result"], "out_dir": str(out)}))
    else:
        print(text)
    return 0


def verify(args):
    outcome = report.verify(
        args.episode,
        replay=args.replay,
        engine=args.engine,
        mod_sources={name: Path(path) for name, path in _pairs(args.mod_source).items()},
    )
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0 if outcome.get("ok") else 1


def check(args):
    """Validate a provider configuration without spending: catalog, executable, key presence."""
    import os  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    from urllib import request  # noqa: PLC0415

    experiment = json.loads(Path(args.experiment_config).read_text())
    provider = experiment["provider"]
    outcome = {
        "experiment": experiment["id"],
        "kind": provider["kind"],
        "model": provider.get("model"),
    }
    defaults = {"anthropic": "ANTHROPIC_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
    if provider["kind"] in ("anthropic", "openai_compatible", "openrouter"):
        env = provider.get("api_key_env", defaults.get(provider["kind"], "OPENAI_API_KEY"))
        outcome["api_key_env"] = env
        outcome["api_key_present"] = bool(os.environ.get(env))
    if provider["kind"] == "openrouter":
        base = (provider.get("base_url") or "https://openrouter.ai/api/v1").rstrip("/")
        with request.urlopen(f"{base}/models", timeout=30) as response:  # noqa: S310
            catalog = json.loads(response.read().decode())["data"]
        entry = next((m for m in catalog if m["id"] == provider["model"]), None)
        outcome["catalog_models"] = len(catalog)
        outcome["model_found"] = entry is not None
        if entry is not None:
            pricing = entry.get("pricing", {})
            parameters = entry.get("supported_parameters", [])
            outcome.update(
                {
                    "context_length": entry.get("context_length"),
                    "supports_tools": "tools" in parameters,
                    "supports_reasoning": "reasoning" in parameters,
                    "catalog_usd_per_million": {
                        "input": float(pricing.get("prompt", 0)) * 1e6,
                        "output": float(pricing.get("completion", 0)) * 1e6,
                    },
                    "configured_usd_per_million": experiment.get("pricing_usd_per_million"),
                }
            )
    if provider["kind"] == "command":
        preset = COMMAND_PRESETS.get(provider.get("preset", ""), {})
        argv = provider.get("argv") or preset.get("argv", [])
        executable = shutil.which(argv[0]) if argv else None
        outcome["executable"] = executable
        if executable:
            version = subprocess.run(
                [executable, "--version"], capture_output=True, text=True, timeout=30, check=False
            )
            outcome["version"] = (version.stdout or version.stderr).strip()[:200]
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0 if outcome.get("model_found", True) and outcome.get("executable", True) else 1


def run_experiment(args):
    config = (
        json.loads(Path(args.experiment_config).read_text()) if args.experiment_config else None
    )
    plan, scenarios = experiments.build_plan(
        args.suite,
        args.split,
        args.controllers.split(","),
        experiment_config=config,
        trials_per_seed=args.trials,
        scenario_ids=args.scenarios.split(",") if args.scenarios else None,
        engine=args.engine,
        options={"decision_deadline_s": args.decision_deadline},
    )
    rows, _summary = experiments.run_plan(
        plan,
        scenarios,
        args.output,
        experiment_config=config,
        engine=args.engine,
        decision_deadline_s=args.decision_deadline,
        process_deadline_s=args.process_deadline,
        mod_sources={name: Path(path) for name, path in _pairs(args.mod_source).items()},
    )
    print(
        json.dumps(
            {
                "output": args.output,
                "attempted": len(rows),
                "accounting": experiments.analysis.accounting(rows),
            },
            sort_keys=True,
        )
    )
    return 0


def run_investigate(args):
    text = investigate.investigate(args.experiment, limit=args.limit, window=args.window)
    print(text if args.print else f"wrote {Path(args.experiment) / 'failure-investigation.md'}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="zero_ad_bench")
    parser.add_argument("--version", action="version", version=PACKAGE_VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run", help="run one episode and write its artifacts")
    runner.add_argument("--scenario", required=True)
    runner.add_argument("--output", required=True)
    runner.add_argument(
        "--controller", default="noop", help="noop, raid_recovery, sleep:SECONDS, or model"
    )
    runner.add_argument("--experiment-config", help="experiment JSON for the model controller")
    runner.add_argument(
        "--turn-limit",
        type=int,
        help="override the scenario horizon for smoke runs; recorded as an override",
    )
    runner.add_argument("--controller-seat", action="append", metavar="SEAT=SPEC")
    runner.add_argument("--mod-source", action="append", metavar="NAME=PATH")
    runner.add_argument("--engine", default=str(DEFAULT_ENGINE))
    runner.add_argument("--experiment", default="local")
    runner.add_argument("--label")
    runner.add_argument("--decision-deadline", type=float, default=30.0)
    runner.add_argument("--process-deadline", type=float, default=1800.0)
    runner.add_argument("--max-failures", type=int, default=3)
    runner.add_argument("--no-telemetry", action="store_true")
    runner.add_argument("--no-replay", action="store_true")
    runner.set_defaults(handler=run)
    reporter = commands.add_parser("report", help="regenerate result.json and report.md")
    reporter.add_argument("episode")
    reporter.add_argument("--out-dir")
    reporter.set_defaults(handler=regenerate)
    verifier = commands.add_parser(
        "verify", help="check checksums, results, and optionally replay"
    )
    verifier.add_argument("episode")
    verifier.add_argument("--replay", action="store_true")
    verifier.add_argument("--engine", default=str(DEFAULT_ENGINE))
    verifier.add_argument("--mod-source", action="append", metavar="NAME=PATH")
    verifier.set_defaults(handler=verify)
    checker = commands.add_parser("check", help="validate a provider config without spending")
    checker.add_argument("--experiment-config", required=True)
    checker.set_defaults(handler=check)
    trial = commands.add_parser("experiment", help="run a preregistered trial set from a suite")
    trial.add_argument("--suite", required=True)
    trial.add_argument("--split", default="development")
    trial.add_argument(
        "--controllers", default="noop,random,scripted", help="comma-separated specs"
    )
    trial.add_argument("--scenarios", help="comma-separated scenario ids (default: all)")
    trial.add_argument("--trials", type=int, default=1, help="trials per seed")
    trial.add_argument("--output", required=True)
    trial.add_argument("--experiment-config", help="experiment JSON for model trials")
    trial.add_argument("--mod-source", action="append", metavar="NAME=PATH")
    trial.add_argument("--engine", default=str(DEFAULT_ENGINE))
    trial.add_argument("--decision-deadline", type=float, default=30.0)
    trial.add_argument("--process-deadline", type=float, default=1800.0)
    trial.set_defaults(handler=run_experiment)
    inv = commands.add_parser(
        "investigate", help="write a failure investigation for an experiment"
    )
    inv.add_argument("experiment")
    inv.add_argument("--limit", type=int, default=3)
    inv.add_argument("--window", type=int, default=6)
    inv.add_argument("--print", action="store_true")
    inv.set_defaults(handler=run_investigate)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
