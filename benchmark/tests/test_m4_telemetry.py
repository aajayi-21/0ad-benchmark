"""Live telemetry, evaluator, offline-report, interruption, and concurrency gates for M4."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "benchmark/src"
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))

from zero_ad_bench import evaluation, report  # noqa: E402
from zero_ad_bench.agents import (  # noqa: E402
    RaidRecoveryController,
    ScriptedController,
    SleepingController,
)
from zero_ad_bench.engine import EngineProcess  # noqa: E402
from zero_ad_bench.environment import Episode, RunOptions  # noqa: E402
from zero_ad_bench.scenario import Scenario  # noqa: E402
from zero_ad_bench.telemetry import read_jsonl  # noqa: E402


FIXTURES = Path(__file__).parent / "fixtures/m4"
SCENARIOS = FIXTURES / "scenarios"
MOD_SOURCES = {"m4_fixture": FIXTURES}


class TestM4Telemetry(unittest.TestCase):
    def setUp(self):
        output = os.environ.get("ZERO_AD_TEST_OUTPUT")
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="m4-", dir=output))
        print(f"\nEvidence: {self.directory}", flush=True)

    def engine(self, name, scenario):
        engine = EngineProcess(
            self.directory / name,
            mods=scenario.mods,
            mod_sources=MOD_SOURCES,
            process_deadline_s=300,
        )
        self.addCleanup(engine.close)
        engine.ready()
        return engine

    def run_episode(self, scenario_name, controller, name, **options):
        scenario = Scenario.load(SCENARIOS / f"{scenario_name}.json")
        engine = self.engine(name, scenario)
        episode = Episode(
            scenario,
            {1: controller},
            engine,
            self.directory / "runs",
            RunOptions(**{"decision_deadline_s": 20, **options}),
            MOD_SOURCES,
        )
        result = episode.run()
        return episode, result

    def test_raid_recovery_trace_offline_report_and_replay(self):
        episode, result = self.run_episode("raid_recovery_v1", RaidRecoveryController(), "raid")
        directory = episode.artifacts.directory
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["result"], "success", result)
        self.assertEqual(result["objective"]["evaluator"], "entity_count_v1")
        self.assertGreater(result["objective"]["achieved_turn"], 100)
        self.assertEqual(result["terminal_reason"], "turn_limit")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["final_turn"], 500)
        manifest = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "completed")
        self.assertFalse(manifest["stopped_on_success"])
        for name in (
            "decisions",
            "model-calls",
            "observations",
            "actions",
            "events",
            "snapshots",
            "hashes",
        ):
            self.assertIn(f"{name}.jsonl", manifest["files"], name)
        for name in (
            "result.json",
            "report.md",
            "resolved-scenario.json",
            "replay/commands.txt",
            "replay/metadata.json",
            "engine-logs/engine.log",
        ):
            self.assertIn(name, manifest["files"], name)
        events, truncated = read_jsonl(directory / "events.jsonl")
        self.assertFalse(truncated)
        decisions, _ = read_jsonl(directory / "decisions.jsonl")
        by_decision = {d["decision_id"]: d for d in decisions}
        kinds = {e["type"] for e in events}
        for kind in (
            "command",
            "attacked",
            "destroyed",
            "training_finished",
            "research_finished",
            "construction_finished",
            "queue_production_finished",
            "ownership_changed",
            "renamed",
        ):
            self.assertIn(kind, kinds, kind)
        # Every ledger event joins to the decision whose interval contains its turn.
        interval = episode.scenario.decision_turns
        for event in events:
            decision = by_decision[event["decision_id"]]
            self.assertGreater(event["turn"], decision["turn"], event)
            self.assertLessEqual(event["turn"], decision["turn"] + interval, event)
        sequence = [e["seq"] for e in events if e["seq"] is not None]
        self.assertEqual(sequence, sorted(sequence))
        self.assertEqual(len(sequence), len(set(sequence)))
        losses = [
            e
            for e in events
            if e["type"] == "destroyed" and e["cause"] == "killed" and e["entity"]["owner"] == 1
        ]
        self.assertTrue(losses, "the raid must kill at least one civilian")
        self.assertTrue(all(e["killer"]["attacker_owner"] == 2 for e in losses), losses)
        raiders = [e for e in events if e["type"] == "destroyed" and e["entity"]["owner"] == 2]
        self.assertTrue(raiders)
        self.assertTrue(all(e["cause"] in ("killed", "died") for e in raiders), raiders)
        renames = [e for e in events if e["type"] == "destroyed" and e["cause"] == "renamed"]
        self.assertTrue(renames, "foundation replacement must be labeled renamed, not a loss")
        scripted = [e for e in events if e["type"] == "command" and e["source"] == "simulation"]
        agent = [e for e in events if e["type"] == "command" and e["source"] == "agent"]
        self.assertTrue(scripted and agent)
        self.assertTrue(all(e["parent_action_id"] for e in agent))
        research = next(e for e in events if e["type"] == "research_finished")
        self.assertEqual(research["technology"], "gather_capacity_basket")
        snapshots, _ = read_jsonl(directory / "snapshots.jsonl")
        self.assertEqual([s["turn"] for s in snapshots][:3], [0, 25, 50])
        stats = snapshots[-1]["players"]["1"]["statistics"]
        # StatisticsTracker increments class counters, not `total`, for losses and kills.
        self.assertEqual(stats["unitsLost"]["Unit"], len(losses))
        self.assertEqual(stats["enemyUnitsKilled"]["Unit"], len(raiders))
        metrics = snapshots[-1]["interval_metrics"]["1"]
        self.assertEqual(metrics["turns"], interval)
        self.assertGreater(metrics["worker_turns"], 0)
        hashes, _ = read_jsonl(directory / "hashes.jsonl")
        self.assertEqual(len(hashes), len(snapshots))
        actions, _ = read_jsonl(directory / "actions.jsonl")
        applied = [a for a in actions if a["kind"] == "result" and a["stage"] == "applied"]
        self.assertTrue(any(a["action_id"] == "house" for a in applied))
        self.assertTrue(
            any(
                a["kind"] == "lifecycle" and a["event"] == "construction_finished" for a in actions
            )
        )
        tools, _ = read_jsonl(directory / "model-calls.jsonl")
        self.assertEqual(tools, [], "the scripted controller reads only its frozen observation")
        # Offline regeneration reproduces the live result and report byte for byte.
        regenerated = report.build_result(directory)
        self.assertEqual(regenerated, json.loads((directory / "result.json").read_text()))
        self.assertEqual(
            report.build_report(directory, regenerated), (directory / "report.md").read_text()
        )
        out = self.directory / "regenerated"
        env = {**os.environ, "PYTHONPATH": str(PACKAGE)}
        subprocess.run(
            [
                sys.executable,
                "-m",
                "zero_ad_bench",
                "report",
                str(directory),
                "--out-dir",
                str(out),
            ],
            check=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(json.loads((out / "result.json").read_text()), regenerated)
        verified = report.verify(directory, replay=True, mod_sources=MOD_SOURCES)
        self.assertTrue(verified["ok"], verified)
        self.assertEqual(verified["checksum_mismatches"], {})
        self.assertEqual(
            verified["replay"]["boundary_hashes_ok"],
            verified["replay"]["boundary_hashes_expected"],
        )
        self.assertTrue(verified["replay"]["final_hash_matches"], verified["replay"])
        (directory / "events.jsonl").open("a").write('{"tampered": true}\n')
        tampered = report.verify(directory)
        self.assertIn("events.jsonl", tampered["checksum_mismatches"])
        self.assertFalse(tampered["ok"])

    def test_telemetry_toggle_preserves_gameplay_projection(self):
        traces = []
        for telemetry in (True, False):
            episode, result = self.run_episode(
                "raid_recovery_v1",
                RaidRecoveryController(),
                f"telemetry-{telemetry}",
                telemetry=telemetry,
            )
            directory = episode.artifacts.directory
            self.assertEqual(result["status"], "completed", result)
            events, _ = read_jsonl(directory / "events.jsonl")
            hashes, _ = read_jsonl(directory / "hashes.jsonl")
            snapshots, _ = read_jsonl(directory / "snapshots.jsonl")
            ledger = [e for e in events if e["seq"] is not None]
            commands = [
                {
                    k: e[k]
                    for k in (
                        "decision_id",
                        "seat",
                        "source",
                        "sim_time_ms",
                        "parent_action_id",
                        "command",
                    )
                }
                for e in events
                if e["type"] == "command"
            ]
            self.assertEqual(bool(ledger), telemetry)
            self.assertEqual(all(s["telemetry"] == telemetry for s in snapshots), True)
            projection = [
                {k: s[k] for k in ("turn", "stop_reason", "player_states")}
                | {"players": s["players"]}
                for s in snapshots
            ]
            traces.append(
                {
                    "hashes": [h["hash"] for h in hashes],
                    "commands": commands,
                    "projection": projection,
                    "result": {k: result[k] for k in ("result", "final_turn", "objective")},
                }
            )
        self.assertEqual(traces[0], traces[1])

    def test_engine_failure_timeout_and_forfeit_accounting(self):
        episode, result = self.run_episode("turn_error_v1", RaidRecoveryController(), "error")
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["result"], "invalid")
        self.assertEqual(result["failure"]["kind"], "engine_error")
        self.assertEqual(result["failure"]["code"], "advance_failed")
        directory = episode.artifacts.directory
        manifest = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "failed")
        decisions, _ = read_jsonl(directory / "decisions.jsonl")
        self.assertEqual(len(decisions), 1)
        self.assertTrue((directory / "engine-logs/engine.log").is_file())
        self.assertTrue((directory / "report.md").is_file())
        verified = report.verify(directory)
        self.assertEqual(verified["classification"], "failed")
        self.assertTrue(verified["result_matches"])
        episode, result = self.run_episode(
            "raid_recovery_v1",
            SleepingController(3),
            "sleep",
            decision_deadline_s=0.5,
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["result"], "failure")
        self.assertEqual(result["administrative"]["1"]["kind"], "forfeit")
        self.assertEqual(result["terminal_reason"], "game_end")
        decisions, _ = read_jsonl(episode.artifacts.directory / "decisions.jsonl")
        self.assertEqual([d["outcome"] for d in decisions], ["timeout", "timeout", "forfeit"])
        self.assertTrue(all(d["elapsed_s"] < 1 for d in decisions))
        actions, _ = read_jsonl(episode.artifacts.directory / "actions.jsonl")
        resign = next(a for a in actions if a["kind"] == "result" and a["action_id"] == "forfeit")
        self.assertEqual(resign["stage"], "applied")
        self.assertEqual(result["player_states"]["1"], "defeated")

        def overspend(gateway):
            if gateway.turn == 0:
                for _ in range(9):
                    gateway.inspect("briefing", max_chars=2048)
            return []

        episode, result = self.run_episode(
            "raid_recovery_v1", ScriptedController(overspend), "budget"
        )
        self.assertEqual(result["status"], "completed", result)
        decisions, _ = read_jsonl(episode.artifacts.directory / "decisions.jsonl")
        self.assertEqual(decisions[0]["outcome"], "controller_error")
        self.assertIn("BudgetExceeded", decisions[0]["error"])
        self.assertEqual(decisions[0]["reads_used"], 8)
        self.assertEqual(decisions[1]["outcome"], "submitted")
        calls, _ = read_jsonl(episode.artifacts.directory / "model-calls.jsonl")
        self.assertEqual(len(calls), 8)
        self.assertTrue(
            all(c["operation"] == "inspect" and c["http_status"] == 200 for c in calls)
        )
        self.assertTrue(all("text" in c["response"] for c in calls))

    def test_interrupted_runs_keep_partial_traces(self):
        env = {**os.environ, "PYTHONPATH": str(PACKAGE), "PYTHONUNBUFFERED": "1"}
        for mode in ("kill", "term"):
            with self.subTest(mode=mode):
                output = self.directory / f"interrupt-{mode}"
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "zero_ad_bench",
                        "run",
                        "--scenario",
                        str(SCENARIOS / "raid_recovery_v1.json"),
                        "--output",
                        str(output),
                        "--controller",
                        "sleep:0.4",
                        "--decision-deadline",
                        "30",
                        f"--mod-source=m4_fixture={FIXTURES}",
                        "--process-deadline",
                        "300",
                    ],
                    env=env,
                    start_new_session=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                episode_dir = None
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline and process.poll() is None:
                    candidates = list(output.glob("episode_*/decisions.jsonl"))
                    if candidates and len(read_jsonl(candidates[0])[0]) >= 3:
                        episode_dir = candidates[0].parent
                        break
                    time.sleep(0.2)
                self.assertIsNotNone(episode_dir, process.stdout.read() if process.poll() else "")
                if mode == "kill":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.send_signal(signal.SIGTERM)
                process.wait(timeout=60)
                manifest = json.loads((episode_dir / "manifest.json").read_text())
                self.addCleanup(
                    subprocess.run,
                    ["pkill", "-9", "-P", str(manifest["engine_process"]["pid"])],
                    check=False,
                )
                verified = report.verify(episode_dir)
                for stream in ("decisions", "events", "snapshots", "observations"):
                    records, _ = read_jsonl(episode_dir / f"{stream}.jsonl")
                    self.assertTrue(records, stream)
                if mode == "kill":
                    self.assertEqual(manifest["status"], "running")
                    self.assertEqual(verified["classification"], "incomplete")
                    self.assertEqual(verified["result"], "incomplete")
                else:
                    self.assertEqual(process.returncode, 2)
                    self.assertEqual(manifest["status"], "interrupted")
                    self.assertEqual(verified["classification"], "interrupted")
                    self.assertEqual(verified["checksum_mismatches"], {})
                    self.assertTrue(verified["result_matches"])
                    self.assertEqual(verified["result"], "incomplete")
                self.assertFalse(verified["ok"])
                text = report.build_report(episode_dir)
                self.assertIn("incomplete", text)

    def test_concurrent_episodes_use_isolated_directories(self):
        scenario = Scenario.load(SCENARIOS / "raid_recovery_v1.json")
        engines = [self.engine(f"concurrent-{i}", scenario) for i in range(2)]

        def run(engine):
            episode = Episode(
                scenario,
                {1: RaidRecoveryController()},
                engine,
                self.directory / "shared",
                RunOptions(decision_deadline_s=20),
                MOD_SOURCES,
            )
            return episode.run()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, engines))
        self.assertEqual({r["status"] for r in results}, {"completed"})
        self.assertNotEqual(results[0]["episode_id"], results[1]["episode_id"])
        directories = sorted((self.directory / "shared").glob("episode_*"))
        self.assertEqual(len(directories), 2)
        for directory in directories:
            manifest = json.loads((directory / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "completed")
            self.assertTrue(report.verify(directory)["ok"])
        self.assertEqual(results[0]["result"], results[1]["result"])
        hashes = [[h["hash"] for h in read_jsonl(d / "hashes.jsonl")[0]] for d in directories]
        self.assertEqual(hashes[0], hashes[1], "same seeds and controller reproduce the hashes")

    def test_evaluators_on_known_score_fixtures(self):
        def snapshot(turn, phase="village", civilians=8, stop=""):
            return {
                "turn": turn,
                "stop_reason": stop,
                "terminated": bool(stop),
                "truncated": stop == "turn_limit",
                "player_states": {"1": "active", "2": "active"},
                "players": {
                    "1": {
                        "phase": phase,
                        "templates": {
                            "units/athen/support_civilian": civilians,
                            "structures/athen/civil_centre": 1,
                        },
                        "classes": {"Unit": civilians},
                    }
                },
            }

        outcome = {"terminal_reason": None, "player_states": {}, "turn": 50}
        phase = {
            "evaluator": "reached_phase_v1",
            "player": 1,
            "params": {"phase": "town", "by_turn": 100},
        }
        pending = evaluation.evaluate(phase, [snapshot(0), snapshot(50)], outcome)
        self.assertIsNone(pending["success"])
        self.assertEqual(pending["censored_at_turn"], 50)
        reached = evaluation.evaluate(phase, [snapshot(0), snapshot(100, "town")], outcome)
        self.assertTrue(reached["success"])
        self.assertEqual(reached["achieved_turn"], 100)
        late = evaluation.evaluate(
            phase,
            [snapshot(0), snapshot(150, "town", stop="turn_limit")],
            {**outcome, "terminal_reason": "turn_limit", "turn": 150},
        )
        self.assertFalse(late["success"])
        self.assertEqual(late["censored_at_turn"], 150)
        count = {
            "evaluator": "entity_count_v1",
            "player": 1,
            "params": {
                "template_suffix": "support_civilian",
                "min_count": 9,
                "after_turn": 40,
                "by_turn": 400,
            },
        }
        early = evaluation.evaluate(
            count, [snapshot(0, civilians=9), snapshot(25, civilians=9)], outcome
        )
        self.assertIsNone(early["success"], "counts before after_turn do not satisfy the goal")
        recovered = evaluation.evaluate(
            count, [snapshot(0, civilians=9), snapshot(50, civilians=9)], outcome
        )
        self.assertEqual((recovered["success"], recovered["achieved_turn"]), (True, 50))
        preserve = {
            "evaluator": "preserve_entity_v1",
            "player": 1,
            "params": {"template_suffix": "civil_centre", "until_turn": 100},
        }
        kept = evaluation.evaluate(preserve, [snapshot(0), snapshot(100)], outcome)
        self.assertTrue(kept["success"])
        lost = [
            snapshot(0),
            {
                **snapshot(50),
                "players": {"1": {"phase": "village", "templates": {}, "classes": {}}},
            },
        ]
        self.assertFalse(evaluation.evaluate(preserve, lost, outcome)["success"])
        self.assertEqual(evaluation.score(kept, outcome, "completed", {"1": None}, []), "success")
        self.assertEqual(
            evaluation.score(
                kept, outcome, "completed", {"1": None}, [{"reason": "ledger_overflow"}]
            ),
            "invalid",
        )
        self.assertEqual(
            evaluation.score(kept, outcome, "completed", {"1": {"kind": "forfeit"}}, []), "failure"
        )
        self.assertEqual(evaluation.score(kept, outcome, "failed", {"1": None}, []), "invalid")
        self.assertEqual(
            evaluation.score(pending, outcome, "interrupted", {"1": None}, []), "incomplete"
        )


if __name__ == "__main__":
    unittest.main()
