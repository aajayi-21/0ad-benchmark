"""Live visibility, frozen-query, text-only scouting, and replay gates for M3."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from benchmark.tests.replay_helpers import verify_replay
from benchmark.tests.test_m1_interface import CONFIG, EngineProcess


FIXTURES = Path(__file__).parent / "fixtures/m3"


def normalized(value, episode):
    """Compare public facts across episodes after replacing their random identifier."""
    return json.dumps(value, sort_keys=True).replace(episode, "<episode>")


class TestM3Observations(unittest.TestCase):
    def setUp(self):
        output = os.environ.get("ZERO_AD_TEST_OUTPUT")
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="m3-", dir=output))
        print(f"\nEvidence: {self.directory}", flush=True)
        self.engine = EngineProcess(self.directory / "engine", fixtures=FIXTURES)
        self.addCleanup(self.engine.close)
        self.engine.ready()
        self.config = json.loads(CONFIG.read_text())
        self.config.update({"mapType": "random", "map": "maps/random/m3_visibility"})
        self.config["settings"].update(
            {
                "Size": 128,
                "StartingResources": 2000,
                "CheatsEnabled": False,
                "RevealMap": False,
                "ExploreMap": False,
                "TriggerScripts": ["scripts/m3_setup.js"],
                "VictoryConditions": [],
            }
        )
        for player in self.config["settings"]["PlayerData"]:
            player.update({"Civ": "athen", "AI": "", "AIBehavior": "balanced"})
        self.trace = []
        self.addCleanup(self.save_trace)
        self.response = None

    def save_trace(self):
        (self.directory / "trace.json").write_text(json.dumps(self.trace, indent=2) + "\n")

    def ok(self, operation, data=None, **kwargs):
        status, response = self.engine.call(operation, data, **kwargs)
        self.assertEqual(status, 200, response)
        self.trace.append({"operation": operation, "request": data, "response": response})
        return response

    def reset(self, mode="memory", variant=0, **kwargs):
        self.config["settings"].update({"M3Mode": mode, "M3Variant": variant})
        self.response = self.ok(
            "reset",
            {
                "attributes": self.config,
                "seats": [1],
                "save_replay": True,
                "turn_limit": 1200,
                **kwargs,
            },
        )
        return self.observe()

    def observe(self):
        self.view = self.ok(
            "observe",
            {"episode_id": self.response["episode_id"], "audience": "player", "seat": 1},
        )["data"]
        return self.view

    def advance(self, turns=1, actions=()):
        previous = self.response
        self.response = self.ok(
            "advance",
            {
                "episode_id": previous["episode_id"],
                "expected_turn": previous["turn"],
                "decision_id": previous["data"]["next_decision_id"],
                "turns": turns,
                "batches": [{"seat": 1, "actions": list(actions)}],
            },
        )
        self.assertEqual(self.response["turn"], previous["turn"] + turns)
        self.assertEqual(self.response["sim_time_ms"], self.response["turn"] * 200)
        return self.observe()

    def query_body(self, kind, **kwargs):
        return {
            "episode_id": self.response["episode_id"],
            "seat": 1,
            "observation_id": self.view["observation_id"],
            "kind": kind,
            **kwargs,
        }

    def inspect(self, kind, **kwargs):
        return self.ok("inspect", self.query_body(kind, **kwargs))["data"]

    def pages(self, kind, **kwargs):
        pages = []
        cursor = None
        # Every request is bounded independently; cap page traversal as well.
        for _ in range(200):
            page = self.inspect(kind, cursor=cursor, **kwargs)
            pages.append(page)
            cursor = page["next_cursor"]
            if cursor is None:
                return pages
        raise AssertionError("Query exceeded 200 pages")

    def test_hidden_pairs_transient_sightings_and_revisit(self):
        comparisons = []
        for variant in range(4):
            with self.subTest(variant=variant):
                initial = self.reset(variant=variant)
                enemy = next(
                    item
                    for item in initial["visible_entities"]
                    if item["template"].endswith("infantry_spearman_b")
                )
                scout = next(
                    item for item in initial["own_entities"] if "Cavalry" in item["classes"]
                )
                self.assertFalse(
                    any(
                        item["template"].endswith("civil_centre")
                        for item in initial["visible_entities"]
                    )
                )
                view = self.advance(8)
                memory = next(
                    item for item in view["last_seen"] if item["handle"] == enemy["handle"]
                )
                self.assertEqual(memory["status"], "last_seen")
                self.assertEqual(memory["health"], enemy["health"])
                self.assertEqual(memory["position"], enemy["position"])
                self.assertLess(memory["last_seen_turn"], 3)
                transient = next(
                    item
                    for item in view["last_seen"]
                    if item["template"].endswith("infantry_javelineer_a")
                )
                self.assertEqual(transient["last_seen_turn"], 4)
                self.assertTrue(
                    any(
                        event["type"] == "sighting"
                        and event["entity"]["handle"] == transient["handle"]
                        and event["turn"] == 4
                        for event in view["events"]
                    )
                )
                entity_query = self.inspect("entities", handles=[enemy["handle"], "seen-999999"])
                region_query = self.pages(
                    "region", bounds={"min_x": 380, "min_z": 380, "max_x": 448, "max_z": 448}
                )
                briefing = self.pages("briefing", max_chars=1024, limit=8)
                catalog = self.ok(
                    "catalog",
                    {
                        "episode_id": self.response["episode_id"],
                        "seat": 1,
                        "templates": [
                            "structures/athen/house",
                            "__proto__",
                            "structures/spart/house",
                        ],
                        "technologies": ["phase_town", "constructor"],
                    },
                )["data"]
                failed = self.advance(
                    actions=[
                        {
                            "action_id": "hidden-target",
                            "type": "attack",
                            "units": [scout["handle"]],
                            "target": enemy["handle"],
                            "queued": False,
                            "allow_capture": False,
                        }
                    ]
                )
                self.assertEqual(failed["action_results"][0]["reason"], "unavailable_entity")
                comparisons.append(
                    normalized(
                        [initial, view, entity_query, region_query, briefing, catalog, failed],
                        self.response["episode_id"],
                    )
                )
                revisited = self.advance()
                absent = next(
                    item for item in revisited["last_seen"] if item["handle"] == enemy["handle"]
                )
                self.assertEqual(absent["status"], "not_present_at_last_position")
                self.assertFalse(
                    any(event["type"] == "destroyed" for event in revisited["events"])
                )
                returned = self.advance(2)
                if variant != 2:
                    visible_again = next(
                        item
                        for item in returned["visible_entities"]
                        if "infantry_spearman" in item["template"]
                    )
                    if variant == 3:
                        self.assertNotEqual(visible_again["handle"], enemy["handle"])
                    else:
                        self.assertEqual(visible_again["handle"], enemy["handle"])
        self.assertTrue(all(result == comparisons[0] for result in comparisons[1:]))

    def test_visible_enemy_private_fields_and_queue_history(self):
        for mode in ("privacy", "queue_history"):
            comparisons = []
            for variant in (0, 1):
                initial = self.reset(mode=mode, variant=variant)
                enemy = next(
                    item
                    for item in initial["visible_entities"]
                    if item["template"].endswith("civil_centre")
                )
                view = self.advance(1 if mode == "privacy" else 2)
                if mode == "queue_history":
                    self.assertTrue(
                        any(item["handle"] == enemy["handle"] for item in view["own_entities"])
                    )
                    view = self.advance(
                        actions=[
                            {
                                "action_id": "captured-train",
                                "type": "train",
                                "building": enemy["handle"],
                                "template": "units/athen/support_civilian",
                                "count": 1,
                            }
                        ]
                    )
                    self.assertEqual(view["action_results"][0]["queue_handle"], "queue-1")
                query = self.inspect("entities", handles=[enemy["handle"]])
                comparisons.append(normalized([initial, view, query], self.response["episode_id"]))
            self.assertEqual(*comparisons, mode)

    def test_visible_rename_capture_garrison_and_destruction(self):
        initial = self.reset(mode="lifecycle")
        scout = next(item for item in initial["own_entities"] if "Cavalry" in item["classes"])
        enemy = next(
            item
            for item in initial["visible_entities"]
            if item["template"].endswith("infantry_spearman_b")
        )
        house = next(
            item for item in initial["visible_entities"] if item["template"].endswith("house")
        )
        renamed = self.advance(2)
        self.assertEqual(
            next(item for item in renamed["own_entities"] if item["handle"] == scout["handle"])[
                "template"
            ],
            "units/athen/cavalry_swordsman_a",
        )
        self.assertEqual(
            next(
                item for item in renamed["visible_entities"] if item["handle"] == enemy["handle"]
            )["template"],
            "units/athen/infantry_spearman_a",
        )
        captured = self.advance()
        self.assertTrue(
            any(item["handle"] == house["handle"] for item in captured["own_entities"])
        )
        garrisoned = next(item for item in captured["own_entities"] if item["holder"])
        self.assertIsNone(garrisoned["position"])
        holder = next(
            item for item in captured["own_entities"] if item["handle"] == garrisoned["holder"]
        )
        self.assertIn(garrisoned["handle"], holder["garrisoned"])
        dead = self.advance()
        self.assertTrue(
            any(
                event["type"] == "destroyed" and event["entity"]["handle"] == enemy["handle"]
                for event in dead["events"]
            )
        )
        self.assertEqual(
            next(item for item in dead["last_seen"] if item["handle"] == enemy["handle"])[
                "status"
            ],
            "destroyed",
        )
        for entity in dead["visible_entities"] + dead["last_seen"]:
            self.assertFalse({"orders", "queue", "carrying", "garrisoned", "id"} & entity.keys())

    def test_readonly_paging_validation_and_population(self):
        view = self.reset(mode="scouting", seats=[1, 2])
        before = copy.deepcopy(self.response["data"])
        full = self.pages("section", section="map_cells", limit=11)
        self.assertEqual([row for page in full for row in page["records"]], view["map"]["cells"])
        self.assertTrue(
            all(
                page["omitted_count"] == len(view["map"]["cells"]) - len(page["records"])
                for page in full
            )
        )
        for cell in view["map"]["cells"]:
            if cell["visibility"] == "unknown":
                self.assertIsNone(cell["terrain"])
                self.assertIsNone(cell["territory"])
        self.assertTrue(any(cell["visibility"] == "unknown" for cell in view["map"]["cells"]))
        self.assertTrue(view["map"]["resources"])
        query = self.query_body(
            "entities", handles=[view["own_entities"][0]["handle"], "own-999999"]
        )
        first = self.ok("inspect", query)["data"]
        self.assertEqual(first, self.ok("inspect", query)["data"])
        self.assertEqual(first["records"][-1]["error"], "unavailable_entity")
        for changes, code in [
            ({"observation_id": "old"}, "stale_observation"),
            ({"limit": 0}, "invalid_query"),
            ({"limit": None}, "invalid_query"),
            ({"kind": {"toString": None}}, "invalid_query"),
            ({"kind": ["entities"]}, "invalid_query"),
            ({"handles": ["__proto__"], "cursor": "fake:9999"}, "invalid_cursor"),
            ({"handles": [1]}, "invalid_query"),
            ({"handles": ["same", "same"]}, "invalid_query"),
            ({"kind": "region", "handles": []}, "invalid_query"),
            ({"seat": 2}, "stale_observation"),
        ]:
            status, error = self.engine.call("inspect", {**query, **changes})
            self.assertIn(status, (400, 409), error)
            self.assertEqual(error["error"]["code"], code)
        self.assertEqual(self.observe(), view)
        self.assertEqual(self.response["data"], before)
        cc = next(
            item for item in view["own_entities"] if item["template"].endswith("civil_centre")
        )
        # Use one bound seat for actions; reset also verifies that old observation cursors expire.
        self.reset(mode="scouting")
        status, error = self.engine.call("inspect", query)
        self.assertEqual((status, error["error"]["code"]), (409, "stale_episode"))
        trained = self.advance(
            turns=5,
            actions=[
                {
                    "action_id": "train",
                    "type": "train",
                    "building": cc["handle"],
                    "template": "units/athen/support_civilian",
                    "count": 3,
                }
            ],
        )
        self.assertEqual(trained["action_results"][0]["stage"], "applied")
        self.assertEqual(
            trained["self"]["population"]["actual_units"],
            view["self"]["population"]["actual_units"],
        )
        self.assertEqual(trained["self"]["population"]["reserved_population"], 3)
        self.assertEqual(self.ok("health")["turn"], 5)

    def test_text_only_scouting_construction_and_replay(self):
        self.reset(mode="scouting", objective="Scout the opposing settlement and build one house.")
        text = "\n".join(page["text"] for page in self.pages("briefing"))
        (self.directory / "initial-briefing.txt").write_text(text + "\n")
        own = [
            json.loads(line.removeprefix("Own entity: "))
            for line in text.splitlines()
            if line.startswith("Own entity: ")
        ]
        scout = next(item for item in own if "cavalry" in item["template"])
        workers = [item["handle"] for item in own if item["template"].endswith("support_civilian")]
        worker = self.inspect("entities", handles=workers[:1])["records"][0]
        self.assertIn("structures/athen/house", worker["buildable"])
        self.advance(
            300,
            [
                {
                    "action_id": "scout",
                    "type": "move",
                    "units": [scout["handle"]],
                    "position": {"x": 332, "z": 332},
                    "queued": False,
                },
                {
                    "action_id": "house",
                    "type": "build",
                    "units": workers,
                    "template": "structures/athen/house",
                    "position": {"x": 132, "z": 180},
                    "angle": 0,
                    "queued": False,
                    "autorepair": True,
                    "autocontinue": False,
                },
            ],
        )
        text = "\n".join(page["text"] for page in self.pages("briefing"))
        (self.directory / "completed-briefing.txt").write_text(text + "\n")
        sightings = [
            json.loads(line.removeprefix("Memory: "))
            for line in text.splitlines()
            if line.startswith("Memory: ")
        ]
        settlement = next(
            item
            for item in sightings
            if item["owner"] == 2 and item["template"].endswith("civil_centre")
        )
        self.assertGreater(settlement["last_seen_turn"], 0)
        self.assertLess(settlement["last_seen_turn"], 300)
        events = [
            json.loads(line.removeprefix("Event: "))
            for line in text.splitlines()
            if line.startswith("Event: ")
        ]
        sighting = next(
            item
            for item in events
            if item["type"] == "sighting" and item["entity"]["handle"] == settlement["handle"]
        )
        self.assertEqual(sighting["entity"]["last_seen_turn"], sighting["turn"])
        actions = self.inspect("section", section="action_results")["records"]
        house = next(item for item in actions if item["action_id"] == "house")
        self.assertEqual(house["stage"], "applied")
        completed = self.inspect("entities", handles=[house["foundation_handle"]])["records"][0]
        self.assertEqual(completed["template"], "structures/athen/house")
        self.assertIsNone(completed["foundation_progress"])
        self.verify_replay()

    def verify_replay(self):
        final = self.ok("finalize", {"episode_id": self.response["episode_id"]})
        verify_replay(self, self.engine, final, self.directory, FIXTURES)

    def test_partial_petra_readonly_and_full_diagnostic(self):
        self.config["settings"]["PlayerData"][1]["AI"] = "petra"
        sequences = []
        for stress in (False, True):
            view = self.reset(mode="scouting")
            sequence = []
            for _ in range(3):
                if stress:
                    self.inspect("briefing", max_chars=1024)
                    self.inspect(
                        "region", bounds={"min_x": 320, "min_z": 320, "max_x": 448, "max_z": 448}
                    )
                    self.inspect(
                        "entities", handles=[view["own_entities"][0]["handle"], "seen-999999"]
                    )
                view = self.advance(40)
                self.assertEqual(view["track"], "partial")
                self.assertFalse(
                    any(
                        item["owner"] == 2 and item["template"].endswith("civil_centre")
                        for item in view["visible_entities"]
                    )
                )
                sequence.append(
                    [
                        view,
                        self.response["data"]["state_hash"],
                        self.response["data"]["command_trace"],
                    ]
                )
            self.assertTrue(any(row[2] for row in sequence))
            sequences.append(normalized(sequence, self.response["episode_id"]))
        self.assertEqual(*sequences)
        full = self.reset(mode="scouting", information_mode="full")
        self.assertEqual(full["track"], "full_diagnostic")
        self.assertTrue(
            any(
                item["owner"] == 2 and item["template"].endswith("civil_centre")
                for item in full["visible_entities"]
            )
        )
        self.assertTrue(all("queue" not in item for item in full["visible_entities"]))

    def test_primary_track_rejects_reveal_and_alliances(self):
        for setting in ({"RevealMap": True}, {"AllyView": True}, {"LockTeams": False}):
            config = copy.deepcopy(self.config)
            config["settings"].update(setting)
            status, error = self.engine.call(
                "reset", {"attributes": config, "seats": [1], "save_replay": False}
            )
            self.assertEqual((status, error["error"]["code"]), (400, "unsupported_visibility"))
        self.config["settings"]["PlayerData"][1]["Team"] = 1
        status, error = self.engine.call(
            "reset", {"attributes": self.config, "seats": [1], "save_replay": False}
        )
        self.assertEqual((status, error["error"]["code"]), (400, "unsupported_visibility"))


if __name__ == "__main__":
    unittest.main()
