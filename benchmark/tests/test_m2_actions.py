"""Bounded live action, timing, authority, and replay checks for M2."""

import concurrent.futures
import json
import os
import tempfile
import unittest
from pathlib import Path

from benchmark.tests.replay_helpers import verify_replay
from benchmark.tests.test_m1_interface import CONFIG, EngineProcess


FIXTURES = Path(__file__).parent / "fixtures/m2"


class TestM2Actions(unittest.TestCase):
    def setUp(self):
        output = os.environ.get("ZERO_AD_TEST_OUTPUT")
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="m2-", dir=output))
        print(f"\nEvidence: {self.directory}", flush=True)
        self.engine = EngineProcess(self.directory / "engine", fixtures=FIXTURES)
        self.addCleanup(self.engine.close)
        self.engine.ready()
        self.config = json.loads(CONFIG.read_text())
        self.config.update({"mapType": "random", "map": "maps/random/m2_actions"})
        self.config["settings"].update(
            {
                "Size": 128,
                "StartingResources": 2000,
                "CheatsEnabled": False,
                "RevealMap": False,
                "ExploreMap": False,
                "TriggerScripts": ["scripts/m2_setup.js"],
                "VictoryConditions": [],
            }
        )
        for player in self.config["settings"]["PlayerData"]:
            player.update({"Civ": "athen", "AI": "", "AIBehavior": "balanced"})
        self.response = None
        self.trace = []
        self.addCleanup(self.save_trace)

    def save_trace(self):
        (self.directory / "trace.json").write_text(json.dumps(self.trace, indent=2) + "\n")

    def ok(self, operation, data=None, **kwargs):
        status, response = self.engine.call(operation, data, **kwargs)
        self.assertEqual(status, 200, response)
        self.trace.append({"operation": operation, "request": data, "response": response})
        return response

    def reset(self, limit=12000, seats=(1,)):
        self.response = self.ok(
            "reset",
            {
                "attributes": self.config,
                "seats": list(seats),
                "save_replay": True,
                "turn_limit": limit,
            },
        )
        return self.response

    def advance(self, turns=1, actions=(), **kwargs):
        previous = self.response
        body = {
            "episode_id": previous["episode_id"],
            "expected_turn": previous["turn"],
            "decision_id": previous["data"]["next_decision_id"],
            "turns": turns,
            "batches": [{"seat": 1, "actions": list(actions)}],
        }
        self.response = self.ok("advance", body, **kwargs)
        elapsed = self.response["turn"] - previous["turn"]
        self.assertEqual(self.response["sim_time_ms"] - previous["sim_time_ms"], elapsed * 200)
        if self.response["state"] != "terminal":
            self.assertEqual(elapsed, turns)
        return self.response

    def own(self, suffix):
        return [
            entity
            for entity in self.response["data"]["players"]["1"]["own_entities"]
            if entity["template"].endswith(suffix)
        ]

    def applied(self, response, action_id):
        result = next(x for x in response["data"]["action_results"] if x["action_id"] == action_id)
        self.assertEqual(result["stage"], "applied", result)
        self.assertEqual(result["execution_turn"], result["submission_turn"] + 1)
        return result

    def replay(self):
        final = self.ok("finalize", {"episode_id": self.response["episode_id"]})
        verify_replay(self, self.engine, final, self.directory, FIXTURES)

    def test_scripted_action_gate_and_replay(self):
        self.reset()
        worker = self.own("support_civilian")[0]
        workers = [item["handle"] for item in self.own("support_civilian")]
        cc = self.own("civil_centre")[0]["handle"]
        store = self.own("storehouse")[0]["handle"]
        response = self.advance(
            1,
            [
                {
                    "action_id": "move",
                    "type": "move",
                    "units": [workers[0]],
                    "position": {"x": 160, "z": 156},
                    "queued": False,
                }
            ],
        )
        self.applied(response, "move")
        self.advance(25)
        self.assertNotEqual(self.own("support_civilian")[0]["position"], worker["position"])
        view = self.response["data"]["players"]["1"]
        tree = next(item for item in view["visible_entities"] if item["resource"])
        response = self.advance(
            50,
            [
                {
                    "action_id": "gather",
                    "type": "gather",
                    "units": [workers[0]],
                    "target": tree["handle"],
                    "queued": False,
                }
            ],
        )
        self.applied(response, "gather")
        self.assertTrue(self.own("support_civilian")[0]["carrying"])
        response = self.advance(
            1,
            [
                {
                    "action_id": "build",
                    "type": "build",
                    "units": workers[1:],
                    "template": "structures/athen/house",
                    "position": {"x": 132, "z": 180},
                    "angle": 0,
                    "queued": False,
                    "autorepair": True,
                    "autocontinue": False,
                },
                {
                    "action_id": "train",
                    "type": "train",
                    "building": cc,
                    "template": "units/athen/support_civilian",
                    "count": 1,
                },
                {
                    "action_id": "research",
                    "type": "research",
                    "building": store,
                    "technology": "gather_capacity_basket",
                },
            ],
            request_id="purchases",
        )
        for action in ("build", "train", "research"):
            self.applied(response, action)
        foundation = self.applied(response, "build")["foundation_handle"]
        duplicate = self.ok("advance", self.trace[-1]["request"], request_id="purchases")
        self.assertEqual(duplicate, response)
        for _ in range(6):
            self.advance(50)
        self.assertEqual(len(self.own("support_civilian")), 9)
        self.assertIn(
            "gather_capacity_basket", self.response["data"]["players"]["1"]["self"]["researched"]
        )
        self.assertTrue(any(item["handle"] == foundation for item in self.own("house")))
        self.assertFalse(
            any(item["template"].startswith("foundation|") for item in self.own("house"))
        )
        before = self.own("storehouse")[0]["health"]["current"]
        response = self.advance(
            50,
            [
                {
                    "action_id": "repair",
                    "type": "repair",
                    "units": workers[1:],
                    "target": store,
                    "queued": False,
                    "autocontinue": False,
                }
            ],
        )
        self.applied(response, "repair")
        self.assertGreater(self.own("storehouse")[0]["health"]["current"], before)
        soldiers = [item["handle"] for item in self.own("infantry_spearman_b")]
        self.advance(
            50,
            [
                {
                    "action_id": "approach",
                    "type": "move",
                    "units": soldiers,
                    "position": {"x": 236, "z": 128},
                    "queued": False,
                }
            ],
        )
        target = next(
            item
            for item in self.response["data"]["players"]["1"]["visible_entities"]
            if item["owner"] == 2 and item["template"].endswith("storehouse")
        )
        response = self.advance(
            50,
            [
                {
                    "action_id": "attack",
                    "type": "attack",
                    "units": soldiers,
                    "target": target["handle"],
                    "allow_capture": False,
                    "queued": False,
                }
            ],
        )
        self.applied(response, "attack")
        after = next(
            item
            for item in response["data"]["players"]["1"]["visible_entities"]
            if item["handle"] == target["handle"]
        )
        self.assertLess(after["health"]["current"], target["health"]["current"])
        turn = response["turn"]
        response = self.advance(50, [{"action_id": "resign", "type": "resign"}])
        self.applied(response, "resign")
        self.assertEqual(response["turn"], turn + 1)
        self.assertTrue(response["data"]["terminated"])
        self.replay()

    def test_limits_invalid_batches_and_stale_requests(self):
        self.reset(limit=30)
        response = self.advance(
            1,
            [
                {
                    "action_id": "bad",
                    "type": "research",
                    "building": "unknown",
                    "technology": "phase_town_athen",
                }
            ],
        )
        self.assertEqual(response["data"]["action_results"][0]["reason"], "unavailable_entity")
        stale = {"episode_id": response["episode_id"], "expected_turn": 0, "turns": 25}
        status, error = self.engine.call("advance", stale)
        self.assertEqual((status, error["error"]["code"]), (409, "stale_turn"))
        response = self.advance(25, [{"action_id": str(i), "type": "wait"} for i in range(21)])
        self.assertEqual(response["data"]["action_results"][0]["reason"], "action_budget_exceeded")
        response = self.advance(50)
        self.assertEqual(response["turn"], 30)
        self.assertTrue(response["data"]["truncated"])
        self.assertFalse(response["data"]["terminated"])
        self.assertEqual(self.ok("health")["turn"], 30)

    def test_schema_authority_and_failed_placement(self):
        self.reset()
        worker = self.own("support_civilian")[0]["handle"]
        store = self.own("storehouse")[0]["handle"]
        cc = self.own("civil_centre")[0]
        actions = [
            {
                "action_id": "hidden",
                "type": "attack",
                "units": [worker],
                "target": "seen-999999",
                "allow_capture": False,
                "queued": False,
            },
            {
                "action_id": "nested",
                "type": "set_rally_point",
                "building": cc["handle"],
                "position": {"x": 150, "z": 150},
                "queued": False,
                "data": {"command": "cheat"},
            },
            {
                "action_id": "outside",
                "type": "move",
                "units": [worker],
                "position": {"x": -1, "z": 150},
                "queued": False,
            },
            {
                "action_id": "blocked",
                "type": "build",
                "units": [worker],
                "template": "structures/athen/house",
                "position": cc["position"],
                "angle": 0,
                "queued": False,
                "autorepair": True,
                "autocontinue": False,
            },
            {
                "action_id": "valid",
                "type": "research",
                "building": store,
                "technology": "gather_capacity_basket",
            },
        ]
        response = self.advance(1, actions)
        results = {item["action_id"]: item for item in response["data"]["action_results"]}
        for action in ("hidden", "nested", "outside"):
            self.assertEqual(results[action]["stage"], "rejected", results[action])
        self.assertEqual(results["blocked"]["stage"], "failed")
        self.applied(response, "valid")
        self.assertFalse(
            any(item["template"].startswith("foundation|") for item in self.own("house"))
        )
        for batches, reason in [
            ("bad", "invalid_batch"),
            (None, "invalid_batch"),
            ([{"seat": 2, "actions": []}], "invalid_batch"),
            (
                [{"seat": 1, "actions": [{"action_id": "same", "type": "wait"}] * 2}],
                "duplicate_action_id",
            ),
        ]:
            body = {
                "episode_id": response["episode_id"],
                "expected_turn": response["turn"],
                "decision_id": response["data"]["next_decision_id"],
                "turns": 1,
                "batches": batches,
            }
            response = self.ok("advance", body)
            self.assertEqual(response["data"]["action_results"][0]["reason"], reason)
            self.assertEqual(response["turn"], body["expected_turn"] + 1)
        self.response = response
        self.replay()

    def test_rally_stance_garrison_and_unload(self):
        self.reset()
        worker = self.own("support_civilian")[0]["handle"]
        cc = self.own("civil_centre")[0]["handle"]
        response = self.advance(
            50,
            [
                {
                    "action_id": "rally",
                    "type": "set_rally_point",
                    "building": cc,
                    "position": {"x": 160, "z": 160},
                    "queued": False,
                },
                {"action_id": "stance", "type": "stance", "units": [worker], "stance": "passive"},
                {
                    "action_id": "garrison",
                    "type": "garrison",
                    "units": [worker],
                    "holder": cc,
                    "queued": False,
                },
            ],
        )
        for action in ("rally", "stance", "garrison"):
            self.applied(response, action)
        self.assertIn(worker, self.own("civil_centre")[0]["garrisoned"])
        response = self.advance(
            1, [{"action_id": "unload", "type": "unload", "units": [worker], "holder": cc}]
        )
        self.applied(response, "unload")
        self.assertNotIn(worker, self.own("civil_centre")[0]["garrisoned"])
        self.replay()

    def test_queue_full_and_population_block_are_distinct(self):
        self.config["settings"]["StartingResources"] = 10000
        self.reset()
        cc = self.own("civil_centre")[0]["handle"]
        command = {
            "type": "train",
            "building": cc,
            "template": "units/athen/support_civilian",
            "count": 5,
        }
        response = self.advance(1, [{"action_id": str(i), **command} for i in range(17)])
        results = response["data"]["action_results"]
        self.assertEqual([item["stage"] for item in results], ["applied"] * 16 + ["failed"])
        self.assertEqual(len(self.own("civil_centre")[0]["queue"]), 16)
        for _ in range(12):
            self.advance(50)
        queue = self.own("civil_centre")[0]["queue"]
        self.assertTrue(queue)
        self.assertGreater(queue[0]["needed_population"], 0)
        self.replay()

    def test_concurrent_retries_and_stale_decisions(self):
        self.reset()
        cc = self.own("civil_centre")[0]["handle"]
        body = {
            "episode_id": self.response["episode_id"],
            "expected_turn": 0,
            "decision_id": 0,
            "turns": 1,
            "batches": [
                {
                    "seat": 1,
                    "actions": [
                        {
                            "action_id": "once",
                            "type": "train",
                            "building": cc,
                            "template": "units/athen/support_civilian",
                            "count": 1,
                        }
                    ],
                }
            ],
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(
                pool.map(
                    lambda _: self.engine.call("advance", body, request_id="same-request"),
                    range(3),
                )
            )
        self.assertTrue(all(status == 200 for status, _ in responses), responses)
        self.assertTrue(all(response == responses[0][1] for _, response in responses))
        self.response = responses[0][1]
        self.trace.append(
            {"operation": "concurrent_advance", "request": body, "response": self.response}
        )
        self.assertEqual(self.response["turn"], 1)
        self.assertEqual(len(self.own("civil_centre")[0]["queue"]), 1)
        for changes, reason in [({}, "stale_turn"), ({"expected_turn": 1}, "stale_decision")]:
            status, error = self.engine.call("advance", {**body, **changes})
            self.assertEqual((status, error["error"]["code"]), (409, reason))
        self.assertEqual(self.ok("health")["turn"], 1)
        self.replay()

    def test_execution_time_authority_and_partial_group(self):
        self.reset()
        workers = [item["handle"] for item in self.own("support_civilian")[:2]]
        cc = self.own("civil_centre")[0]["handle"]
        self.advance(
            50,
            [
                {
                    "action_id": "garrison",
                    "type": "garrison",
                    "units": workers,
                    "holder": cc,
                    "queued": False,
                }
            ],
        )
        self.assertTrue(set(workers).issubset(self.own("civil_centre")[0]["garrisoned"]))
        response = self.advance(
            1,
            [
                {"action_id": "first", "type": "unload", "units": workers[:1], "holder": cc},
                {"action_id": "partial", "type": "unload", "units": workers, "holder": cc},
            ],
        )
        result = self.applied(response, "partial")
        self.assertTrue(result["partial"])
        self.assertEqual([item["stage"] for item in result["entities"]], ["failed", "applied"])
        response = self.advance(
            50,
            [
                {"action_id": "resign", "type": "resign"},
                {
                    "action_id": "too-late",
                    "type": "train",
                    "building": cc,
                    "template": "units/athen/support_civilian",
                    "count": 1,
                },
            ],
        )
        failed = next(
            item for item in response["data"]["action_results"] if item["action_id"] == "too-late"
        )
        self.assertEqual((failed["stage"], failed["reason"]), ("failed", "inactive_seat"))
        self.assertEqual(response["turn"], 52)
        self.replay()

    def test_petra_replay_and_remaining_order_types(self):
        self.config["settings"]["PlayerData"][1]["AI"] = "petra"
        self.reset()
        worker = self.own("support_civilian")[0]["handle"]
        store = self.own("storehouse")[0]["handle"]
        tree = next(
            item["handle"]
            for item in self.response["data"]["players"]["1"]["visible_entities"]
            if item["resource"]
        )
        self.advance(
            50,
            [
                {
                    "action_id": "gather",
                    "type": "gather",
                    "units": [worker],
                    "target": tree,
                    "queued": False,
                }
            ],
        )
        response = self.advance(
            25,
            [
                {
                    "action_id": "return",
                    "type": "return_resources",
                    "units": [worker],
                    "target": store,
                    "queued": False,
                }
            ],
        )
        self.applied(response, "return")
        response = self.advance(
            25,
            [
                {
                    "action_id": "attack-move",
                    "type": "attack_move",
                    "units": [worker],
                    "position": {"x": 160, "z": 160},
                    "allow_capture": False,
                    "queued": False,
                }
            ],
        )
        self.applied(response, "attack-move")
        response = self.advance(
            1, [{"action_id": "stop", "type": "stop", "units": [worker], "queued": False}]
        )
        self.applied(response, "stop")
        for _ in range(4):
            self.advance(50)
        commands = [
            item
            for entry in self.trace
            if entry["operation"] == "advance"
            for item in entry["response"]["data"]["command_trace"]
        ]
        self.assertTrue(any(item["source"] == "builtin_ai" for item in commands))
        self.replay()

    def test_seat_order_and_builtin_ai_authority(self):
        hashes = []
        for order in [(1, 2), (2, 1)]:
            self.reset(seats=(1, 2))
            batches = []
            for seat in order:
                cc = next(
                    item["handle"]
                    for item in self.response["data"]["players"][str(seat)]["own_entities"]
                    if item["template"].endswith("civil_centre")
                )
                batches.append(
                    {
                        "seat": seat,
                        "actions": [
                            {
                                "action_id": "train",
                                "type": "train",
                                "building": cc,
                                "template": "units/athen/support_civilian",
                                "count": 1,
                            }
                        ],
                    }
                )
            response = self.ok(
                "advance",
                {
                    "episode_id": self.response["episode_id"],
                    "expected_turn": 0,
                    "decision_id": 0,
                    "turns": 25,
                    "batches": batches,
                },
            )
            self.assertEqual([item["seat"] for item in response["data"]["command_trace"]], [1, 2])
            self.assertTrue(
                all(item["stage"] == "applied" for item in response["data"]["action_results"])
            )
            hashes.append(response["data"]["state_hash"])
        self.assertEqual(*hashes)
        self.config["settings"]["PlayerData"][1]["AI"] = "petra"
        self.reset(seats=(2,))
        response = self.ok(
            "advance",
            {
                "episode_id": self.response["episode_id"],
                "expected_turn": 0,
                "decision_id": 0,
                "turns": 1,
                "batches": [{"seat": 2, "actions": [{"action_id": "bad", "type": "resign"}]}],
            },
        )
        self.assertEqual(response["data"]["action_results"][0]["reason"], "builtin_ai_seat")
        self.assertEqual(response["data"]["players"]["2"]["self"]["state"], "active")

    def test_execution_rejection_and_cancellation(self):
        self.config["settings"]["StartingResources"] = 50
        self.reset()
        cc = self.own("civil_centre")[0]["handle"]
        command = {
            "type": "train",
            "building": cc,
            "template": "units/athen/support_civilian",
            "count": 1,
        }
        response = self.advance(
            1, [{"action_id": "first", **command}, {"action_id": "second", **command}]
        )
        accepted = self.applied(response, "first")
        failed = next(
            item for item in response["data"]["action_results"] if item["action_id"] == "second"
        )
        self.assertEqual(failed["stage"], "failed")
        self.assertEqual(failed["reason"], "execution_rejected")
        response = self.advance(
            1,
            [
                {
                    "action_id": "cancel",
                    "type": "cancel_production",
                    "building": cc,
                    "queue": accepted["queue_handle"],
                }
            ],
        )
        self.applied(response, "cancel")
        self.assertEqual(self.own("civil_centre")[0]["queue"], [])
        self.replay()
