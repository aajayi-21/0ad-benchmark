"""Command line entry: run an episode, regenerate its report, or verify its artifacts."""

import argparse
import json
import signal
import sys
from pathlib import Path

from zero_ad_bench import PACKAGE_VERSION, report
from zero_ad_bench.agents import make_controller
from zero_ad_bench.engine import DEFAULT_ENGINE, EngineProcess
from zero_ad_bench.environment import Episode, RunOptions
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
    mod_sources = {name: Path(path) for name, path in _pairs(args.mod_source).items()}
    seats = scenario.external_seats()
    specs = _pairs(args.controller_seat)
    controllers = {seat: make_controller(specs.get(str(seat), args.controller)) for seat in seats}
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    options = RunOptions(
        decision_deadline_s=args.decision_deadline,
        telemetry=not args.no_telemetry,
        save_replay=not args.no_replay,
        max_consecutive_failures=args.max_failures,
        experiment_id=args.experiment,
        label=args.label,
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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="zero_ad_bench")
    parser.add_argument("--version", action="version", version=PACKAGE_VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run", help="run one episode and write its artifacts")
    runner.add_argument("--scenario", required=True)
    runner.add_argument("--output", required=True)
    runner.add_argument("--controller", default="noop", help="noop, raid_recovery, sleep:SECONDS")
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
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
