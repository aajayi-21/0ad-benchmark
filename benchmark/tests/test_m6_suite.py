"""Suite, baseline, preregistration, analysis, and accounting gates for M6."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "benchmark/src"
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))

from zero_ad_bench import analysis, experiment, report  # noqa: E402
from zero_ad_bench.agents import CONTROLLERS, make_controller  # noqa: E402
from zero_ad_bench.engine import EngineProcess  # noqa: E402
from zero_ad_bench.environment import Episode, RunOptions  # noqa: E402
from zero_ad_bench.telemetry import read_jsonl  # noqa: E402


SUITE = ROOT / "benchmark/scenarios/suite_v1.json"
GOAL_SCENARIOS = [
    "economy_phase_town_v1",
    "raid_defense_v1",
    "recovery_v1",
    "expansion_v1",
    "scouting_response_v1",
]


class TestM6Suite(unittest.TestCase):
    def setUp(self):
        output = os.environ.get("ZERO_AD_TEST_OUTPUT")
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="m6-", dir=output))
        print(f"\nEvidence: {self.directory}", flush=True)

    def run_scenario(self, scenario, controller, name, seed=101, turn_limit=None, deadline=20):
        scenario = scenario.with_seeds(seed, seed + 1000)
        if turn_limit is not None:
            scenario.turn_limit = turn_limit
        engine = EngineProcess(self.directory / name, mods=scenario.mods, process_deadline_s=600)
        self.addCleanup(engine.close)
        engine.ready()
        episode = Episode(
            scenario,
            {1: controller},
            engine,
            self.directory / "runs",
            RunOptions(decision_deadline_s=deadline, turn_limit_override=turn_limit),
        )
        return episode, episode.run()

    def test_suite_manifest_is_frozen_and_consistent(self):
        suite, scenarios, _ = experiment.load_suite(SUITE)
        self.assertEqual(suite["scenarios"], [*GOAL_SCENARIOS, "full_game_conquest_v1"])
        self.assertEqual(len(suite["seed_splits"]["development"]), 3)
        self.assertEqual(len(suite["seed_splits"]["public_evaluation"]), 5)
        private = suite["seed_splits"]["private_evaluation"]
        self.assertEqual(private["count"], 5)
        self.assertEqual(len(private["sha256"]), 64)
        self.assertFalse(
            set(suite["seed_splits"]["development"])
            & set(suite["seed_splits"]["public_evaluation"])
        )
        for scenario_id, scenario in scenarios.items():
            self.assertTrue(scenario.objective["stop_on_success"], scenario_id)
            self.assertIn(scenario.baseline, CONTROLLERS, scenario_id)
            self.assertTrue(scenario.objective["public"], scenario_id)
            self.assertNotIn("Seed", scenario.objective["public"])
            self.assertIn("agent_benchmark", scenario.mods)
        self.assertEqual(scenarios["full_game_conquest_v1"].controllers[2]["kind"], "petra")
        # The game's own setup turns victory conditions into trigger scripts; so must the runner.
        resolved = scenarios["full_game_conquest_v1"].resolve()["settings"]
        self.assertEqual(resolved["VictoryConditions"], ["conquest"])
        self.assertEqual(
            resolved["TriggerScripts"],
            ["scripts/TriggerHelper.js", "scripts/ConquestCommon.js", "scripts/Conquest.js"],
        )
        self.assertEqual(
            scenarios["raid_defense_v1"].resolve()["settings"]["TriggerScripts"],
            ["scripts/suite_raid.js"],
        )
        self.assertEqual(suite["versions"]["scaffold"], "2")

    def test_scripted_baselines_demonstrate_feasibility(self):
        _, scenarios, _ = experiment.load_suite(SUITE)
        for scenario_id in GOAL_SCENARIOS:
            with self.subTest(scenario=scenario_id):
                scenario = scenarios[scenario_id]
                episode, result = self.run_scenario(
                    scenario, make_controller(scenario.baseline), scenario_id
                )
                self.assertEqual(result["status"], "completed", result)
                self.assertEqual(result["result"], "success", (scenario_id, result["objective"]))
                self.assertLessEqual(result["objective"]["achieved_turn"], scenario.turn_limit)
                verified = report.verify(episode.artifacts.directory)
                self.assertTrue(verified["ok"], verified)
                if scenario_id == GOAL_SCENARIOS[-1]:
                    # A relative episode path must replay too: the engine runs elsewhere.
                    relative = os.path.relpath(episode.artifacts.directory, Path.cwd())
                    replayed = report.verify(relative, replay=True)
                    self.assertTrue(replayed["ok"], replayed)
                    self.assertTrue(replayed["replay"]["final_hash_matches"], replayed)

    def test_noop_and_random_baselines_are_legal_and_reproducible(self):
        _, scenarios, _ = experiment.load_suite(SUITE)
        scenario = scenarios["recovery_v1"]
        _, noop = self.run_scenario(scenario, make_controller("noop"), "noop", turn_limit=400)
        self.assertEqual(
            (noop["status"], noop["decision_outcomes"]), ("completed", {"submitted": 16})
        )
        hashes = []
        for name in ("random-a", "random-b"):
            episode, random_result = self.run_scenario(
                scenario, make_controller("random:7"), name, turn_limit=400
            )
            self.assertEqual(random_result["status"], "completed", random_result)
            actions = [
                a
                for a in read_jsonl(episode.artifacts.directory / "actions.jsonl")[0]
                if a["kind"] == "result"
            ]
            self.assertGreater(len(actions), 10)
            self.assertFalse(
                [a for a in actions if a.get("reason") == "invalid_schema"],
                "random actions must be schema-valid",
            )
            hashes.append(
                [h["hash"] for h in read_jsonl(episode.artifacts.directory / "hashes.jsonl")[0]]
            )
        self.assertEqual(hashes[0], hashes[1], "a seeded random baseline replays identically")

    def test_wall_sets_are_rejected_instead_of_invalidating_the_episode(self):
        _, scenarios, _ = experiment.load_suite(SUITE)
        scenario = scenarios["economy_phase_town_v1"]
        test = self

        class WallBuilder:
            name = "wall"

            def decide(self, gateway):
                view = gateway.observation()
                workers = [e for e in view["own_entities"] if e["buildable"]]
                test.assertTrue(workers)
                test.assertFalse(
                    [n for e in workers for n in e["buildable"] if "wallset" in n],
                    "wall sets must not be advertised as buildable",
                )
                if view["turn"] != 0:
                    return []
                centres = [
                    e for e in view["own_entities"] if e["template"].endswith("civil_centre")
                ]
                centre = centres[0]["position"]
                return [
                    {
                        "action_id": "wall-0",
                        "type": "build",
                        "units": [workers[0]["handle"]],
                        "template": "structures/wallset_palisade",
                        "position": {"x": centre["x"] + 30, "z": centre["z"]},
                        "angle": 0,
                        "queued": False,
                        "autorepair": True,
                        "autocontinue": False,
                    }
                ]

        episode, result = self.run_scenario(scenario, WallBuilder(), "wall", turn_limit=100)
        self.assertEqual(result["status"], "completed", result)
        results = [
            a
            for a in read_jsonl(episode.artifacts.directory / "actions.jsonl")[0]
            if a["kind"] == "result" and a["action_id"] == "wall-0"
        ]
        self.assertEqual(
            [(a["stage"], a["reason"]) for a in results], [("rejected", "unavailable_template")]
        )
        log = (self.directory / "wall" / "engine.log").read_text()
        self.assertNotIn("Error creating foundation", log)

    def test_petra_versus_petra_sanity_check_uses_an_observer_seat(self):
        scenario = experiment.load_suite(SUITE)[1]["full_game_conquest_v1"]
        scenario.controllers[1] = {
            "kind": "petra",
            "civilization": "athen",
            "difficulty": 3,
            "behavior": "balanced",
        }
        self.assertEqual(scenario.external_seats(), [])
        episode, result = self.run_scenario(
            scenario, make_controller("noop"), "petra", turn_limit=150, deadline=20
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(episode.seats, [1])
        observations = read_jsonl(episode.artifacts.directory / "observations.jsonl")[0]
        self.assertEqual(observations[0]["observation"]["track"], "native_ai_diagnostic")
        events = read_jsonl(episode.artifacts.directory / "events.jsonl")[0]
        self.assertTrue(
            any(e["type"] == "command" and e["source"] == "builtin_ai" for e in events)
        )

    def test_preregistered_experiment_accounts_for_every_attempt(self):
        suite_dir = self.directory / "suite"
        suite_dir.mkdir()
        suite, _scenarios, path = experiment.load_suite(SUITE)
        # A tiny preregistered set: one feasible scenario, one seed, plus a scenario that fails.
        broken = json.loads((path.parent / "recovery_v1.json").read_text())
        broken["id"] = "broken_v1"
        broken["map"]["path"] = "maps/random/suite_missing"
        (suite_dir / "broken_v1.json").write_text(json.dumps(broken))
        for name in ("scouting_response_v1",):
            (suite_dir / f"{name}.json").write_text((path.parent / f"{name}.json").read_text())
        tiny = dict(suite)
        tiny["scenarios"] = ["scouting_response_v1", "broken_v1"]
        tiny["seed_splits"] = {
            "development": [101, 102],
            "public_evaluation": [201],
            "private_evaluation": {"count": 1, "sha256": "0" * 64},
        }
        (suite_dir / "suite_tiny.json").write_text(json.dumps(tiny))
        plan, plan_scenarios = experiment.build_plan(
            suite_dir / "suite_tiny.json", "development", ["noop", "scripted"]
        )
        self.assertEqual(plan["trial_count"], 8)
        self.assertEqual(plan["versions"]["scaffold"], "2")
        self.assertTrue(plan["versions"]["engine_binary_sha256"])
        output = self.directory / "experiment"
        rows, summary = experiment.run_plan(
            plan, plan_scenarios, output, decision_deadline_s=20, process_deadline_s=600
        )
        self.assertEqual(len(rows), 8)
        manifest = json.loads((output / "experiment-manifest.json").read_text())
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(len(manifest["preregistration_sha256"]), 64)
        pre = json.loads((output / "preregistration.json").read_text())
        self.assertEqual(
            [t["trial_id"] for t in pre["trials"]], [t["trial_id"] for t in plan["trials"]]
        )
        broken_rows = [r for r in rows if r["scenario"] == "broken_v1"]
        self.assertTrue(
            all(r["status"] == "failed" and r["failure"] for r in broken_rows), broken_rows
        )
        accounting = analysis.accounting(rows)
        self.assertEqual((accounting["attempted"], accounting["excluded"]), (8, 4))
        scout = summary["scouting_response_v1"]
        self.assertEqual(scout["scripted"]["success"]["successes"], 2)
        self.assertEqual(scout["scripted"]["success"]["ci95"]["clusters"], 2)
        self.assertEqual(scout["noop"]["success"]["rate"], 0.0)
        self.assertEqual(scout["scripted"]["time_to_success"]["censored_at_horizon"], 0)
        self.assertEqual(scout["noop"]["time_to_success"]["censored_at_horizon"], 2)
        broken = summary["broken_v1"]["scripted"]
        self.assertEqual(broken["accounting"]["exclusion_reasons"], {"engine_error": 2})
        self.assertIsNone(broken["success"]["rate"])
        text = (output / "experiment-report.md").read_text()
        self.assertIn("Excluded and failed attempts", text)
        self.assertIn("broken_v1/101/noop/0", text)
        # Resuming the same directory re-runs nothing and keeps the plan.
        rows_again, _ = experiment.run_plan(
            plan, plan_scenarios, output, decision_deadline_s=20, process_deadline_s=600
        )
        self.assertEqual(len(rows_again), 8)
        self.assertEqual(len(read_jsonl(output / "experiment.jsonl")[0]), 8)
        with self.assertRaises(ValueError):
            other, _ = experiment.build_plan(
                suite_dir / "suite_tiny.json", "development", ["noop"]
            )
            experiment.run_plan(other, plan_scenarios, output)

    def test_analysis_known_score_fixtures(self):
        def row(seed, result, status="completed", achieved=None, cost=None):
            return {
                "trial_id": f"s/{seed}/c/0",
                "scenario": "s",
                "controller": "c",
                "seed": seed,
                "status": status,
                "result": result,
                "achieved_turn": achieved,
                "cost_usd": cost,
            }

        rows = [
            row(1, "success", achieved=100),
            row(1, "success", achieved=120),
            row(2, "failure"),
            row(3, "success", achieved=90),
            row(4, "invalid", status="invalid"),
            row(5, "invalid", status="failed"),
        ]
        rate = analysis.success_rate(rows)
        self.assertEqual((rate["successes"], rate["valid"], rate["attempted"]), (3, 4, 6))
        self.assertAlmostEqual(rate["rate"], 0.75)
        self.assertEqual(rate["ci95"]["clusters"], 3)
        self.assertLessEqual(rate["ci95"]["low"], 0.75)
        self.assertGreaterEqual(rate["ci95"]["high"], 0.75)
        self.assertEqual(analysis.bootstrap_ci(rows[:2], len)["low"], 2)
        timing = analysis.time_to_success(rows, horizon=500)
        self.assertEqual(
            (timing["successes"], timing["censored_at_horizon"], timing["median_turn"]),
            (3, 1, 100),
        )
        self.assertIsNone(
            analysis.time_to_success([row(1, "failure"), row(2, "success", achieved=50)], 500)[
                "median_turn"
            ]
        )
        accounting = analysis.accounting(rows)
        self.assertEqual(accounting["exclusion_reasons"], {"invalid": 1, "failed": 1})
        match = analysis.match_score(
            [row(1, "win"), row(2, "draw"), row(3, "loss"), row(4, "invalid", status="invalid")]
        )
        self.assertEqual((match["valid_matches"], match["match_score"]), (3, 0.5))
        self.assertEqual(
            analysis.cost_summary([row(1, "success", cost=0.5), row(2, "success")])[
                "episodes_without_cost"
            ],
            1,
        )


if __name__ == "__main__":
    unittest.main()
