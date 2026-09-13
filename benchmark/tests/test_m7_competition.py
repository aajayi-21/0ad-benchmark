"""Competition gates for M7: arrival order, context isolation, match rules, replay, accounting."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "benchmark/src"
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))

from zero_ad_bench import competition, investigate, report  # noqa: E402
from zero_ad_bench.agents import (  # noqa: E402
    DecisionResult,
    ScriptedController,
    SleepingController,
    make_controller,
)
from zero_ad_bench.engine import EngineProcess  # noqa: E402
from zero_ad_bench.environment import Episode, RunOptions  # noqa: E402
from zero_ad_bench.model_agent import ModelController  # noqa: E402
from zero_ad_bench.providers import MockProvider  # noqa: E402
from zero_ad_bench.scenario import Scenario  # noqa: E402
from zero_ad_bench.telemetry import read_jsonl  # noqa: E402


COMPETITION = ROOT / "benchmark/scenarios/competition_v1.json"
MATCH = ROOT / "benchmark/scenarios/match_conquest_v1.json"
MOCK_CONFIG = json.loads((ROOT / "benchmark/experiments/mock_pilot_v1.json").read_text())
PARTICIPANTS = {
    "eco": {"controller": "economy", "config": None},
    "idle": {"controller": "noop", "config": None},
}


class Lagged:
    """A controller that answers after a fixed delay: the same decisions, a different arrival."""

    def __init__(self, inner, seconds):
        self.inner = inner
        self.seconds = seconds
        self.name = f"lagged-{inner.name}"

    def decide(self, gateway):
        time.sleep(self.seconds)
        return self.inner.decide(gateway)


class Resigner:
    name = "resigner"

    def __init__(self, at_turn):
        self.at_turn = at_turn

    def decide(self, gateway):
        turn = gateway.observation()["turn"]
        return [{"action_id": f"resign-{turn}", "type": "resign"}] if turn >= self.at_turn else []


class Broken:
    name = "broken"

    def decide(self, gateway):  # noqa: ARG002
        raise RuntimeError("this controller always fails")


class SeatPolicy:
    """A mock model that stores a seat-specific secret in its notes and plan every decision."""

    def __init__(self, seat):
        self.seat = seat
        self.secret = f"SECRET-SEAT-{seat}-{os.urandom(4).hex()}"

    def __call__(self, req, _index):
        decision = req.messages[0]["content"].split("Decision ")[1].split(" ")[0]
        if req.messages[-1]["role"] != "user":
            return {"tool_calls": [], "text": "done", "stop_reason": "end_turn"}
        return {
            "tool_calls": [
                {"name": "write_notes", "input": {"text": f"{self.secret} decision {decision}"}},
                {
                    "name": "submit_actions",
                    "input": {
                        "actions": [{"action_id": f"s{self.seat}-d{decision}", "type": "wait"}],
                        "plan": f"{self.secret} plan {decision}",
                    },
                },
            ]
        }


def command_events(directory):
    events = read_jsonl(directory / "events.jsonl")[0]
    return [
        {k: v for k, v in e.items() if k not in ("decision_id", "parent_decision_id")}
        for e in events
        if e["type"] == "command"
    ]


class TestM7Competition(unittest.TestCase):
    def setUp(self):
        output = os.environ.get("ZERO_AD_TEST_OUTPUT")
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="m7-", dir=output))
        print(f"\nEvidence: {self.directory}", flush=True)

    def run_match(self, controllers, name, *, turn_limit, seed=101, deadline=20, **options):
        scenario = Scenario.load(MATCH).with_seeds(seed, seed + 1000)
        scenario.turn_limit = turn_limit
        engine = EngineProcess(self.directory / name, mods=scenario.mods, process_deadline_s=600)
        self.addCleanup(engine.close)
        engine.ready()
        episode = Episode(
            scenario,
            controllers,
            engine,
            self.directory / "runs",
            RunOptions(decision_deadline_s=deadline, turn_limit_override=turn_limit, **options),
        )
        return episode, episode.run()

    def test_plan_schedules_mirror_first_with_swapped_seats(self):
        matchups = competition.parse_matchups("eco:idle,eco:eco", PARTICIPANTS)
        plan, scenarios = competition.build_match_plan(
            COMPETITION, "development", PARTICIPANTS, matchups
        )
        self.assertEqual(plan["match_count"], 9)
        ids = [m["match_id"] for m in plan["matches"]]
        self.assertEqual(
            ids[:3], [f"match_conquest_v1/{s}/eco-vs-eco/1/0" for s in (101, 102, 103)]
        )
        self.assertEqual(
            ids[3:5],
            ["match_conquest_v1/101/eco-vs-idle/1/0", "match_conquest_v1/101/eco-vs-idle/2/0"],
        )
        first, second = plan["matches"][3], plan["matches"][4]
        self.assertEqual(first["participants"], {"1": "eco", "2": "idle"})
        self.assertEqual(second["participants"], {"1": "idle", "2": "eco"})
        self.assertEqual(first["seed_pair"], second["seed_pair"])
        self.assertTrue(plan["matches"][0]["mirror"])
        self.assertEqual(plan["versions"]["scaffold"], "4")
        self.assertEqual(plan["timeout_policy"]["consecutive_failures_for_forfeit"], 3)
        scenario = scenarios["match_conquest_v1"]
        self.assertEqual(scenario.external_seats(), [1, 2])
        self.assertEqual(scenario.objective["evaluator"], "match_v1")
        players = scenario.resolve()["settings"]["PlayerData"]
        self.assertEqual([p["AI"] for p in players], ["", ""])
        self.assertIn("scripts/Conquest.js", scenario.resolve()["settings"]["TriggerScripts"])
        with self.assertRaises(ValueError):
            competition.parse_participant("muse=model")
        with self.assertRaises(ValueError):
            competition.parse_matchups("eco:nobody", PARTICIPANTS)
        identifier, spec = competition.parse_participant(
            "muse=model:benchmark/experiments/openrouter_muse_match_v1.json"
        )
        self.assertEqual((identifier, spec["controller"]), ("muse", "model"))
        self.assertEqual(len(competition.participant_summary(spec)["config_sha256"]), 64)

    def test_arrival_order_does_not_change_when_commands_enter_the_engine(self):
        runs = {}
        for name, lags in (("slow-seat-1", (1.5, 0.0)), ("slow-seat-2", (0.0, 1.5))):
            controllers = {
                1: Lagged(make_controller("economy"), lags[0]),
                2: Lagged(make_controller("economy"), lags[1]),
            }
            episode, result = self.run_match(controllers, name, turn_limit=150)
            self.assertEqual(result["status"], "completed", result)
            directory = episode.artifacts.directory
            decisions = read_jsonl(directory / "decisions.jsonl")[0]
            observations = read_jsonl(directory / "observations.jsonl")[0]
            runs[name] = {
                "ranks": {(d["decision_id"], d["seat"]): d["arrival_rank"] for d in decisions},
                "hashes": [h["hash"] for h in read_jsonl(directory / "hashes.jsonl")[0]],
                "results": sorted(
                    (a["seat"], a["action_id"], a["execution_turn"], a["stage"])
                    for a in read_jsonl(directory / "actions.jsonl")[0]
                    if a["kind"] == "result"
                ),
                "commands": command_events(directory),
                "observations": observations,
            }
        slow_one, slow_two = runs["slow-seat-1"], runs["slow-seat-2"]
        for decision in range(3):
            self.assertEqual(slow_one["ranks"][(decision, 2)], 1)
            self.assertEqual(slow_one["ranks"][(decision, 1)], 2)
            self.assertEqual(slow_two["ranks"][(decision, 1)], 1)
            self.assertEqual(slow_two["ranks"][(decision, 2)], 2)
        self.assertTrue(slow_one["results"])
        self.assertEqual(slow_one["results"], slow_two["results"])
        self.assertEqual(slow_one["commands"], slow_two["commands"])
        self.assertEqual(slow_one["hashes"], slow_two["hashes"])
        # Both seats observe the same completed turn, each through its own frozen view.
        by_decision = {}
        for row in slow_one["observations"]:
            by_decision.setdefault(row["decision_id"], []).append(row)
        for decision, rows in by_decision.items():
            self.assertEqual({r["turn"] for r in rows}, {rows[0]["turn"]}, decision)
            self.assertEqual(len({r["observation"]["observation_id"] for r in rows}), 2)

    def test_model_contexts_are_isolated_between_seats(self):
        policies = {1: SeatPolicy(1), 2: SeatPolicy(2)}
        controllers = {
            seat: ModelController(MockProvider(policy), MOCK_CONFIG)
            for seat, policy in policies.items()
        }
        episode, result = self.run_match(controllers, "isolation", turn_limit=150)
        self.assertEqual(result["status"], "completed", result)
        self.assertFalse(any(result["administrative"].values()), result["administrative"])
        directory = episode.artifacts.directory
        calls = read_jsonl(directory / "model-calls.jsonl")[0]
        observations = read_jsonl(directory / "observations.jsonl")[0]
        ids = {
            seat: {r["observation"]["observation_id"] for r in observations if r["seat"] == seat}
            for seat in (1, 2)
        }
        for seat, other in ((1, 2), (2, 1)):
            mine = [c for c in calls if c["seat"] == seat]
            self.assertTrue(mine)
            text = json.dumps(mine)
            self.assertNotIn(policies[other].secret, text)
            self.assertNotIn(f"s{other}-d", text)
            for observation_id in ids[other]:
                self.assertNotIn(observation_id, text)
            later = json.dumps([c for c in mine if c["decision_id"] >= 1])
            self.assertIn(policies[seat].secret, later, "own notes must return to the model")
            # A seat's own-entity list is exactly what the privileged snapshot owns for it.
            first = next(r["observation"] for r in observations if r["seat"] == seat)
            opening = next(
                s for s in read_jsonl(directory / "snapshots.jsonl")[0] if s["decision_id"] == 0
            )
            self.assertEqual(
                len(first["own_entities"]), opening["players"][str(seat)]["entities"]["total"]
            )
        results = [a for a in read_jsonl(directory / "actions.jsonl")[0] if a["kind"] == "result"]
        self.assertEqual({a["seat"] for a in results}, {1, 2})
        self.assertTrue(all(a["stage"] == "applied" for a in results), results)

    def test_match_rules_and_replay_with_no_builtin_ai(self):
        episode, drawn = self.run_match(
            {1: make_controller("economy"), 2: make_controller("noop")}, "draw", turn_limit=100
        )
        self.assertEqual((drawn["status"], drawn["result"]), ("completed", "draw"), drawn)
        self.assertEqual(drawn["objective"]["sides"], {"1": "draw", "2": "draw"})
        self.assertEqual(drawn["terminal_reason"], "turn_limit")
        resolved = json.loads((episode.artifacts.directory / "resolved-scenario.json").read_text())
        self.assertEqual(
            [p["AI"] for p in resolved["attributes"]["settings"]["PlayerData"]], ["", ""]
        )
        relative = os.path.relpath(episode.artifacts.directory, Path.cwd())
        replayed = report.verify(relative, replay=True)
        self.assertTrue(replayed["ok"], replayed)
        self.assertTrue(replayed["replay"]["final_hash_matches"], replayed)
        # The graphical client's loading screen needs a display name the bridge never sent.
        viewable = report.viewable_replay(episode.artifacts.directory)
        recorded = (episode.artifacts.directory / "replay/commands.txt").read_text().split("\n")
        copied = viewable.read_text().split("\n")
        self.assertEqual(recorded[1:], copied[1:])
        view_settings = json.loads(copied[0][6:])["settings"]
        self.assertEqual(view_settings["mapName"], "Mainland")
        self.assertEqual(view_settings["PopulationCapType"], "player")
        self.assertEqual(resolved["attributes"]["settings"]["mapName"], "Mainland")
        self.assertEqual(resolved["attributes"]["settings"]["PopulationCapType"], "player")

        _, resigned = self.run_match(
            {1: make_controller("economy"), 2: Resigner(50)}, "resign", turn_limit=400
        )
        self.assertEqual(resigned["result"], "win", resigned)
        self.assertEqual(resigned["objective"]["sides"], {"1": "win", "2": "loss"})
        self.assertEqual(resigned["terminal_reason"], "game_end")
        self.assertEqual(resigned["player_states"], {"1": "won", "2": "defeated"})
        self.assertFalse(any(resigned["administrative"].values()))
        self.assertLess(resigned["final_turn"], 400)

        _, forfeited = self.run_match(
            {1: make_controller("economy"), 2: Broken()}, "forfeit", turn_limit=400
        )
        self.assertEqual(forfeited["objective"]["sides"], {"1": "win", "2": "loss"})
        self.assertEqual(forfeited["administrative"]["2"]["kind"], "forfeit")
        self.assertEqual(forfeited["administrative"]["2"]["turn"], 100)
        self.assertIsNone(forfeited["administrative"]["1"])
        self.assertEqual(forfeited["objective"]["administrative_outcome"], {"2": "forfeit"})
        self.assertEqual(forfeited["terminal_reason"], "game_end")

        _, double = self.run_match(
            {1: SleepingController(3.0), 2: SleepingController(3.0)},
            "double-forfeit",
            turn_limit=400,
            deadline=0.5,
        )
        self.assertEqual(double["objective"]["sides"], {"1": "draw", "2": "draw"}, double)
        self.assertEqual(double["result"], "draw")
        self.assertEqual(
            {s: v["kind"] for s, v in double["administrative"].items()},
            {"1": "forfeit", "2": "forfeit"},
        )
        self.assertEqual(
            double["administrative"]["1"]["turn"], double["administrative"]["2"]["turn"]
        )

    def test_invalid_models_forfeit_but_explicit_empty_submissions_do_not(self):
        invalid = ModelController(MockProvider(lambda *_: {"text": "no submission"}), {})
        valid = ModelController(
            MockProvider(
                lambda *_: {
                    "tool_calls": [
                        {"name": "submit_actions", "input": {"actions": [], "plan": None}}
                    ]
                }
            ),
            {},
        )
        episode, result = self.run_match({1: valid, 2: invalid}, "invalid-model", turn_limit=200)
        self.assertEqual(result["status"], "completed", result)
        self.assertIsNone(result["administrative"]["1"])
        self.assertEqual(result["administrative"]["2"]["kind"], "forfeit")
        self.assertEqual(result["administrative"]["2"]["turn"], 100)
        decisions, _ = read_jsonl(episode.artifacts.directory / "decisions.jsonl")
        self.assertEqual(
            [d["outcome"] for d in decisions if d["seat"] == 2],
            ["malformed", "malformed", "forfeit"],
        )
        self.assertTrue(all(d["outcome"] == "submitted" for d in decisions if d["seat"] == 1))

    def test_rejected_batches_and_reports_preserve_the_correct_seat(self):
        def policy(gateway):
            actions = (
                []
                if gateway.seat == 2
                else [
                    {
                        "action_id": "seat-one-rejected",
                        "type": "move",
                        "units": ["missing-handle"],
                        "position": {"x": 100, "z": 100},
                        "queued": False,
                    }
                ]
            )
            return DecisionResult(actions, {"plan": f"seat-{gateway.seat}-plan"})

        episode, result = self.run_match(
            {1: ScriptedController(policy), 2: ScriptedController(policy)},
            "seat-reports",
            turn_limit=1,
        )
        self.assertEqual(result["status"], "completed", result)
        directory = episode.artifacts.directory
        actions, _ = read_jsonl(directory / "actions.jsonl")
        submission = next(a for a in actions if a["kind"] == "submission")
        self.assertEqual(submission["batches"][0]["actions"][0]["units"], ["missing-handle"])
        self.assertEqual(submission["batches"][1]["actions"], [])
        rows = [
            line.split("|")
            for line in (directory / "report.md").read_text().splitlines()
            if line.startswith("| 0 | 0 |")
        ]
        self.assertEqual({int(row[3]): int(row[7]) for row in rows}, {1: 1, 2: 0})
        for seat in (1, 2):
            text = investigate.investigate_episode(directory, seat=seat)
            self.assertIn(f"seat-{seat}-plan", text)
            self.assertNotIn(f"seat-{3 - seat}-plan", text)
            if seat == 2:
                self.assertNotIn("seat-one-rejected", text)

    def test_competition_accounts_by_matchup_and_side(self):
        suite_dir = self.directory / "competition"
        suite_dir.mkdir()
        manifest = json.loads(COMPETITION.read_text())
        manifest["scenarios"] = ["match_conquest_v1", "broken_match_v1"]
        manifest["seed_splits"]["development"] = [101]
        (suite_dir / "competition_tiny.json").write_text(json.dumps(manifest))
        (suite_dir / "match_conquest_v1.json").write_text(MATCH.read_text())
        broken = json.loads(MATCH.read_text())
        broken["id"] = "broken_match_v1"
        broken["map"]["path"] = "maps/random/does_not_exist"
        (suite_dir / "broken_match_v1.json").write_text(json.dumps(broken))
        matchups = competition.parse_matchups("eco:idle,eco:eco", PARTICIPANTS)
        plan, scenarios = competition.build_match_plan(
            suite_dir / "competition_tiny.json",
            "development",
            PARTICIPANTS,
            matchups,
            options={
                "turn_limit_override": 100,
                "decision_deadline_s": 20,
                "process_deadline_s": 600,
                "max_attempts": 1,
            },
        )
        self.assertEqual(plan["match_count"], 6)
        output = self.directory / "run"
        rows, summary = competition.run_match_plan(
            plan,
            scenarios,
            PARTICIPANTS,
            output,
            decision_deadline_s=20,
            process_deadline_s=600,
            turn_limit_override=100,
        )
        self.assertEqual(len(rows), 6)
        broken_rows = [r for r in rows if r["scenario"] == "broken_match_v1"]
        self.assertTrue(all(r["status"] == "failed" and r["failure"] for r in broken_rows))
        self.assertEqual(summary["accounting"]["attempted"], 6)
        self.assertEqual(summary["accounting"]["valid"], 3)
        self.assertEqual(summary["accounting"]["excluded"], 3)
        cross = summary["matchups"]["eco-vs-idle"]
        self.assertEqual((cross["attempted"], cross["valid"], cross["invalid"]), (4, 2, 2))
        self.assertEqual(cross["seed_pairs"], 1)
        eco = cross["by_participant"]["eco"]
        self.assertEqual((eco["win"], eco["draw"], eco["loss"]), (0, 2, 0))
        self.assertEqual(eco["match_score"], 0.5)
        self.assertEqual(eco["ci95"]["clusters"], 1)
        self.assertEqual(eco["by_seat"]["1"]["valid"], 1)
        self.assertEqual(eco["by_seat"]["2"]["valid"], 1)
        self.assertEqual(cross["by_seat"]["1"]["draw"], 2)
        mirror = summary["matchups"]["eco-vs-eco"]
        self.assertTrue(mirror["mirror"])
        self.assertEqual((mirror["attempted"], mirror["valid"]), (2, 1))
        self.assertIsNone(summary["rating"])
        text = (output / "competition-report.md").read_text()
        self.assertIn("## Outcomes by side", text)
        self.assertIn("broken_match_v1/101/eco-vs-eco/1/0", text)
        self.assertNotIn("Elo", text)
        episodes = len(list((output / "episodes").iterdir()))
        rows_again, _ = competition.run_match_plan(
            plan, scenarios, PARTICIPANTS, output, decision_deadline_s=20, process_deadline_s=600
        )
        self.assertEqual(len(rows_again), 6)
        self.assertEqual(len(list((output / "episodes").iterdir())), episodes)
        with self.assertRaises(ValueError):
            other, _ = competition.build_match_plan(
                suite_dir / "competition_tiny.json", "development", PARTICIPANTS, [("eco", "eco")]
            )
            competition.run_match_plan(other, scenarios, PARTICIPANTS, output)


if __name__ == "__main__":
    unittest.main()
