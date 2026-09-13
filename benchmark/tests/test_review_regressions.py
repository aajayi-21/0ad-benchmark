"""Regression cases for the M0-M7 review, including bounded worker lifecycle failures."""


# These regressions deliberately exercise the scheduler and accounting boundaries.
# ruff: noqa: SLF001

import copy
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from zero_ad_bench import analysis, experiment, planning, provenance, report
from zero_ad_bench.agents import AgentStop, _known
from zero_ad_bench.engine import EngineError
from zero_ad_bench.environment import Episode, RunOptions
from zero_ad_bench.model_agent import ACTION_FIELDS, TOOLS, ModelController, validate_actions
from zero_ad_bench.providers import MockProvider, OpenAICompatibleProvider, usage_record
from zero_ad_bench.scenario import Scenario
from zero_ad_bench.workers import DecisionExpired


class SlowController:
    name = "slow"

    def __init__(self, path):
        self.path = path

    def decide(self, gateway):
        with self.path.open("a") as handle:
            handle.write(f"{os.getpid()}\n")
        time.sleep(5)
        gateway.record("late", {"should_not_exist": True})
        return []


class CountingController:
    name = "counter"

    def __init__(self):
        self.count = 0

    def decide(self, gateway):
        gateway.observation()
        self.count += 1
        return [{"type": "wait", "action_id": str(self.count)}]


class AbortingController:
    name = "aborting"

    def decide(self, gateway):  # noqa: ARG002
        os._exit(2)


class ChildController:
    name = "child-process"

    def __init__(self, path):
        self.path = path

    def decide(self, gateway):  # noqa: ARG002
        script = (
            "import os, signal, sys, time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "Path(sys.argv[1]).write_text(str(os.getppid()) + ' ' + str(os.getpid())); "
            "time.sleep(20)"
        )
        with subprocess.Popen([sys.executable, "-c", script, str(self.path)]) as process:
            process.wait(timeout=25)
        return []


class MemoryArtifacts:
    def __init__(self):
        self.records = []

    def append(self, stream, record):
        self.records.append((stream, record))


class TestReviewRegressions(unittest.TestCase):
    def episode(self, controller, deadline=0.1):
        episode = Episode.__new__(Episode)
        episode.seats = [1]
        episode.controllers = {1: controller}
        episode.workers = {}
        episode.scenario = SimpleNamespace(limits={"reads_per_decision": 8}, decision_turns=50)
        episode.options = RunOptions(decision_deadline_s=deadline)
        episode.views = {1: {"observation_id": "review", "sim_time_ms": 0}}
        episode.artifacts = MemoryArtifacts()
        episode.interrupt_requested = False
        self.addCleanup(lambda: [worker.stop() for worker in episode.workers.values()])
        return episode

    def test_timeout_kills_work_before_next_decision_and_revokes_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pids"
            episode = self.episode(SlowController(path))
            for decision in range(2):
                result = episode._collect_all(decision, decision * 50)[1]
                self.assertEqual(result[1], "timeout")
                with self.assertRaises(DecisionExpired):
                    result[-1].observation()
                for pid in path.read_text().splitlines():
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(pid), 0)
            self.assertEqual(len(path.read_text().splitlines()), 2)
            self.assertEqual(episode.artifacts.records, [])

    def test_successful_private_state_survives_worker_restart(self):
        episode = self.episode(CountingController(), deadline=2)
        for decision in range(3):
            result = episode._collect_all(decision, decision * 50)[1]
            self.assertEqual(result[1], "submitted")
            self.assertEqual(result[0][0]["action_id"], str(decision + 1))
            episode.workers[1].stop()

    def assert_stopped(self, pids):
        for _ in range(100):
            live = []
            for pid in pids:
                stat = Path(f"/proc/{pid}/stat")
                with suppress(FileNotFoundError):
                    if stat.read_text().split()[2] != "Z":
                        live.append(pid)
            if not live:
                return
            time.sleep(0.01)
        self.fail(f"Controller processes still running: {live}")

    def test_timeout_and_runner_death_stop_command_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pids"
            episode = self.episode(ChildController(path), deadline=0.5)
            self.assertEqual(episode._collect_all(0, 0)[1][1], "timeout")
            self.assert_stopped(path.read_text().split())
            path.unlink()
            script = (
                "import sys; from pathlib import Path; "
                "from benchmark.tests.test_review_regressions import "
                "ChildController, TestReviewRegressions; "
                "t=TestReviewRegressions(); "
                "e=t.episode(ChildController(Path(sys.argv[1])), deadline=10); "
                "e._collect_all(0,0)"
            )
            with subprocess.Popen([sys.executable, "-c", script, str(path)]) as runner:
                try:
                    for _ in range(200):
                        if path.exists():
                            break
                        time.sleep(0.01)
                    self.assertTrue(path.exists(), "Child did not start within two seconds")
                    pids = path.read_text().split()
                    runner.kill()
                    runner.wait(timeout=2)
                    self.assert_stopped(pids)
                finally:
                    if runner.poll() is None:
                        runner.kill()
                    if path.exists():
                        with suppress(ProcessLookupError):
                            os.killpg(int(path.read_text().split()[0]), signal.SIGKILL)

    def test_unexpected_worker_exit_is_infrastructure_failure(self):
        episode = self.episode(AbortingController(), deadline=2)
        with self.assertRaises(EngineError) as caught:
            episode._collect_all(0, 0)
        self.assertEqual(caught.exception.kind, "worker")
        self.assertIsNone(episode.workers[1].process)

    def test_controller_constructor_failure_is_an_accounted_trial(self):
        scenario = Scenario.load("benchmark/scenarios/expansion_v1.json")
        trial = {
            "trial_id": "constructor",
            "controller": "model",
            "scenario": scenario.id,
            "seed": 101,
            "ai_seed": 1101,
            "trial": 0,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(experiment, "EngineProcess") as engine,
        ):
            row = experiment.run_trial(
                trial,
                scenario,
                Path(directory),
                {"provider": {"kind": "invalid"}},
                "unused",
                2,
                30,
                {},
            )
        self.assertEqual((row["status"], row["result"]), ("failed", "invalid"))
        self.assertEqual(row["failure"]["kind"], "runner_error")
        engine.assert_not_called()

    def test_strict_action_schema_closes_every_object_and_covers_all_actions(self):
        provider = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
        tools = provider.wire_tools(SimpleNamespace(tools=TOOLS))

        def check(value):
            if isinstance(value, dict):
                if value.get("type") == "object":
                    self.assertIs(value.get("additionalProperties"), False)
                    self.assertEqual(set(value["properties"]), set(value["required"]))
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(tools)
        submit = next(t["function"] for t in tools if t["function"]["name"] == "submit_actions")
        branches = submit["parameters"]["properties"]["actions"]["items"]["anyOf"]
        self.assertEqual(
            {b["properties"]["type"]["enum"][0] for b in branches}, set(ACTION_FIELDS)
        )

    def test_malformed_hashable_fields_are_repairable(self):
        for value in ([], {}, None, 1, True):
            with self.subTest(value=value):
                self.assertTrue(validate_actions([{"action_id": value, "type": "wait"}])[1])
                self.assertTrue(validate_actions([{"action_id": "bad", "type": value}])[1])

    def test_missing_cost_and_provider_cache_semantics(self):
        controller = ModelController(
            MockProvider(lambda *_: {}),
            {
                "pricing_usd_per_million": {"input": 10, "output": 20, "cache_read": 1},
            },
        )
        self.assertIsNone(controller.cost(usage_record()))
        self.assertIsNone(controller.cost(usage_record(input_tokens=100)))
        controller._account(usage_record())
        self.assertTrue(controller.totals["cost_unavailable"])
        self.assertEqual(controller.cost(usage_record(0, 0)), 0)
        provider = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
        response = provider.normalize(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 0,
                    "prompt_tokens_details": {"cached_tokens": 80},
                },
            },
            {},
        )
        self.assertEqual(controller.cost(response.usage), 0.00028)
        self.assertEqual(controller.cost(usage_record(100, 0, cache_read=80)), 0.00108)
        self.assertEqual(controller.cost(usage_record(provider_cost_usd=0.5)), 0.5)

    def test_survival_median_includes_censored_episodes(self):
        successes = [
            {"status": "completed", "result": "success", "achieved_turn": t}
            for t in (100, 200, 300)
        ]
        failure = {"status": "completed", "result": "failure"}
        self.assertEqual(
            analysis.time_to_success(successes + [failure] * 2, 1000)["median_turn"], 300
        )
        self.assertEqual(
            analysis.time_to_success([*successes[:1], failure], 1000)["median_turn"], 100
        )
        self.assertIsNone(analysis.time_to_success([failure] * 2, 1000)["median_turn"])
        self.assertIsNone(analysis.time_to_success([], 1000)["median_turn"])
        self.assertEqual(analysis.time_to_success(successes, 1000)["median_turn"], 200)

    def test_provider_timeout_is_accounted_as_infrastructure_with_unknown_cost(self):
        provider = MockProvider(lambda *_: time.sleep(5))
        controller = ModelController(provider, {"budgets": {"episode_tokens": 2_000_000}})
        episode = self.episode(controller)
        episode.episode_id = "review"
        episode.engine = SimpleNamespace(
            call=lambda *_args, **_kwargs: (
                200,
                {"ok": True, "data": {"text": "review", "remaining_count": 0}},
            )
        )
        result = episode._collect_all(0, 0)[1]
        self.assertEqual(result[1], "provider_failure")
        records = [row for stream, row in episode.artifacts.records if stream == "model-calls"]
        usage = report.model_usage(records, [])
        self.assertEqual(usage["attempts"], 1)
        self.assertTrue(usage["cost_unavailable"])
        self.assertGreater(usage["budget_tokens"], 0)
        self.assertIsNone(episode.workers[1].process)

    def test_budget_stops_between_requests_and_reserves_before_spending(self):
        calls = []
        gateway = SimpleNamespace(
            decision_id=0,
            turn=0,
            decision_turns=50,
            read_budget=8,
            reads=0,
            deadline_s=2,
            observation=lambda: {"sim_time_ms": 0},
            briefing=lambda: {"text": "review", "remaining_count": 0},
            record=lambda *_: None,
        )

        def policy(request, _index):
            calls.append(request.max_output_tokens)
            return {
                "tool_calls": [{"name": "write_notes", "input": {"text": "continue"}}],
                "usage": usage_record(100, 1, provider_cost_usd=1),
            }

        controller = ModelController(
            MockProvider(policy),
            {
                "budgets": {"episode_tokens": 100_000, "episode_cost_usd": 1},
                "pricing_usd_per_million": {"input": 1, "output": 1},
            },
        )
        with self.assertRaises(AgentStop):
            controller.decide(gateway)
        self.assertEqual(len(calls), 1)
        controller = ModelController(MockProvider(policy), {"budgets": {"episode_tokens": 100}})
        with self.assertRaises(AgentStop):
            controller.decide(gateway)
        self.assertEqual(len(calls), 1, "The tiny ceiling must stop before dispatch")

    def test_paid_model_requires_finite_positive_ceilings(self):
        provider = SimpleNamespace(name="openai_compatible")
        for budgets in (
            {},
            {"episode_tokens": 100},
            {"episode_tokens": 100, "episode_cost_usd": float("inf")},
            {"episode_tokens": True, "episode_cost_usd": 1},
        ):
            with self.subTest(budgets=budgets), self.assertRaises(ValueError):
                ModelController(provider, {"budgets": budgets})

    def test_resume_refuses_every_changed_preregistration_field(self):
        plan, scenarios = experiment.build_plan(
            "benchmark/scenarios/suite_v1.json",
            "development",
            ["noop"],
            scenario_ids=["expansion_v1"],
        )
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "preregistration.json").write_text(json.dumps(plan))
            for field in ("options", "experiment_config", "scenarios", "versions", "inputs"):
                changed = copy.deepcopy(plan)
                changed[field] = {"changed": True}
                with (
                    self.subTest(field=field),
                    self.assertRaisesRegex(ValueError, "preregistration differs"),
                ):
                    planning.open_plan(directory, changed, scenarios)
            with self.assertRaisesRegex(ValueError, "Runtime decision_deadline_s"):
                experiment.run_plan(plan, scenarios, directory, decision_deadline_s=1)

    def test_default_mod_roots_resolve_assets_and_archives_preserve_exact_content(self):
        scenario = Scenario.load("benchmark/scenarios/expansion_v1.json")
        self.assertTrue(all(scenario.content_hashes({}).values()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = {}
            for name in ("mod", "public", "fixture"):
                source = root / name
                (source / "simulation").mkdir(parents=True)
                (source / "mod.json").write_text("{}")
                sources[name] = source
            source = sources["fixture"] / "simulation/rules.js"
            source.write_text("const review = 1;\n")
            captured = provenance.capture(["fixture"], sources)
            provenance.archive(root, captured, ["fixture"], sources)
            with zipfile.ZipFile(root / "inputs.zip") as bundle:
                self.assertEqual(
                    bundle.read("mods/fixture/simulation/rules.js"), source.read_bytes()
                )
                self.assertIn("runner/environment.py", bundle.namelist())
            provenance.archive(root, captured, ["fixture"], sources)
            with zipfile.ZipFile(root / "inputs.zip", "a") as bundle:
                bundle.writestr("unexpected", b"tampered")
            with self.assertRaisesRegex(ValueError, "Archived gameplay input"):
                provenance.archive(root, captured, ["fixture"], sources)
            source.write_text("const review = 2;\n")
            self.assertNotEqual(captured, provenance.capture(["fixture"], sources))

    def test_private_seed_file_verifies_commitment_count_and_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.json"
            path.write_text("[7, 11]\n")
            suite = {
                "seed_splits": {
                    "private_evaluation": {
                        "count": 2,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                }
            }
            self.assertEqual(planning.seeds_for_split(suite, "private_evaluation", path), [7, 11])
            path.write_text("[7, 12]\n")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                planning.seeds_for_split(suite, "private_evaluation", path)

    def test_attempt_recovery_accounts_for_interruption_and_retains_budget_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = planning.AttemptJournal(directory, "experiment", "trial_id")
            started, _path = journal.begin({"trial_id": "review"}, {"tokens": 10, "cost_usd": 1})
            recovered = planning.AttemptJournal(directory, "experiment", "trial_id")
            self.assertEqual(recovered.rows[0]["attempt_id"], started["attempt_id"])
            self.assertEqual(recovered.rows[0]["status"], "interrupted")
            self.assertFalse(
                recovered.can_reserve(
                    {"tokens": 10, "cost_usd": 1}, {"tokens": 15, "cost_usd": 1.5}
                )
            )
            self.assertEqual(
                len((Path(directory) / "experiment.jsonl").read_text().splitlines()), 1
            )

    def test_runner_failure_is_recorded_and_retried_with_a_distinct_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "inputs.zip").write_bytes(b"fixture")
            calls = []

            def run(trial):
                calls.append(trial["attempt_id"])
                if len(calls) == 1:
                    raise RuntimeError("injected runner defect")
                return {
                    "status": "completed",
                    "result": "success",
                    "budget_consumed": {"tokens": 0, "cost_usd": 0},
                }

            plan = {
                "schema_version": "1.1",
                "options": {"max_attempts": 2, "experiment_budget": {"tokens": 20, "cost_usd": 2}},
            }
            with (
                patch.object(planning, "open_plan", return_value="fixture"),
                patch.object(planning, "validate_inputs"),
            ):
                rows, _ = planning.execute_schedule(
                    plan,
                    {},
                    directory,
                    stream="experiment",
                    key="trial_id",
                    schedule=[{"trial_id": "review"}],
                    run_one=run,
                    reserve=lambda _: {"tokens": 10, "cost_usd": 1},
                )
            self.assertEqual([r["status"] for r in rows], ["failed", "completed"])
            self.assertEqual(len(set(calls)), 2)
            self.assertEqual(analysis.accounting(rows)["attempted"], 2)

    def test_component_queries_placement_and_production_transitions(self):
        subprocess.run(["node", "benchmark/tests/review_components.js"], check=True, timeout=20)

    def test_enemy_filter_uses_seat_and_diplomacy(self):
        for seat in (1, 2):
            enemy = 3 - seat
            view = {
                "seat": seat,
                "self": {"diplomacy": [0, 1 if seat == 1 else -1, 1 if seat == 2 else -1, 1]},
                "visible_entities": [
                    {"owner": owner, "position": {"x": 0, "z": 0}} for owner in range(4)
                ],
                "last_seen": [],
            }
            self.assertEqual([e["owner"] for e in _known(view, enemy=True)], [enemy])

    def test_unfinished_provider_request_retains_unknown_cost_and_reservation(self):
        started = {
            "kind": "model_request_started",
            "seat": 1,
            "decision_id": 0,
            "request_index": 1,
            "attempt": 1,
            "reservation": {"tokens": 100, "cost_usd": 0.2},
        }
        usage = report.model_usage([started], [])
        self.assertEqual(usage["attempts"], 1)
        self.assertEqual(usage["budget_tokens"], 100)
        self.assertEqual(usage["budget_cost_usd"], 0.2)
        self.assertTrue(usage["cost_unavailable"])
        completed = {**started, "kind": "model", "usage": usage_record(10, 2), "cost_usd": 0.01}
        usage = report.model_usage([started, completed], [])
        self.assertEqual(usage["attempts"], 1)
        self.assertEqual(usage["budget_tokens"], 12)

    def test_schedule_ceiling_stops_before_launching_next_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "inputs.zip").write_bytes(b"fixture")
            plan = {
                "schema_version": "1.1",
                "options": {
                    "max_attempts": 2,
                    "experiment_budget": {"tokens": 15, "cost_usd": 1.5},
                },
            }

            def run(_trial):
                return {
                    "status": "completed",
                    "result": "success",
                    "budget_consumed": {"tokens": 10, "cost_usd": 1},
                }

            with (
                patch.object(planning, "open_plan", return_value="fixture"),
                patch.object(planning, "validate_inputs"),
            ):
                rows, _ = planning.execute_schedule(
                    plan,
                    {},
                    directory,
                    stream="experiment",
                    key="trial_id",
                    schedule=[{"trial_id": "one"}, {"trial_id": "two"}],
                    run_one=run,
                    reserve=lambda _: {"tokens": 10, "cost_usd": 1},
                )
            self.assertEqual([row["trial_id"] for row in rows], ["one"])
            manifest = json.loads((Path(directory) / "experiment-manifest.json").read_text())
            self.assertEqual(manifest["status"], "budget_stop")

    def test_runner_excludes_simultaneous_resume_and_records_report_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (root / ".runner.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "Another runner"):
                    planning.execute_schedule({}, {}, root)
            (root / "inputs.zip").write_bytes(b"fixture")
            with (
                patch.object(planning, "open_plan", return_value="fixture"),
                self.assertRaisesRegex(RuntimeError, "report error"),
            ):
                planning.execute_schedule(
                    {"schema_version": "1.1"},
                    {},
                    root,
                    stream="experiment",
                    key="trial_id",
                    schedule=[],
                    run_one=lambda _: None,
                    reserve=lambda _: None,
                    finish=lambda *_: (_ for _ in ()).throw(RuntimeError("report error")),
                )
            manifest = json.loads((root / "experiment-manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertIsNotNone(manifest["finished_utc"])
