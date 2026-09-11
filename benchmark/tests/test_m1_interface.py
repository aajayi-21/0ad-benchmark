"""Live regression checks for the private M1 engine bridge (no model calls)."""

import concurrent.futures
import copy
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from urllib import error, request


ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "binaries/system/pyrogenesis"
CONFIG = ROOT / "source/tools/rlclient/python/samples/arcadia.json"
FIXTURES = Path(__file__).parent / "fixtures/m1"


class EngineProcess:
    """Own one bounded engine process and an isolated writable game profile."""

    def __init__(self, directory, port=None, extra_args=(), benchmark_mod=True, fixtures=FIXTURES):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_hex(24)
        self.opener = request.build_opener(request.ProxyHandler({}))
        if port is None:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.env = os.environ.copy()
        for key in ("DISPLAY", "WAYLAND_DISPLAY"):
            self.env.pop(key, None)
        for key in ("DATA", "CONFIG", "CACHE", "STATE"):
            self.env[f"XDG_{key}_HOME"] = str(self.directory / key.lower())
        self.env["ZERO_AD_BENCHMARK_TOKEN"] = self.token
        fixture_name = json.loads((fixtures / "mod.json").read_text())["name"]
        fixture_target = self.directory / "data/0ad/mods" / fixture_name
        shutil.copytree(fixtures, fixture_target)
        args = [
            str(ENGINE),
            "--autostart-nonvisual",
            f"--benchmark-interface={self.url[7:]}",
            "--mod=public",
        ]
        if benchmark_mod:
            args += ["--mod=agent_benchmark", f"--mod={fixture_name}"]
        args += list(extra_args)
        self.log_path = self.directory / "engine.log"
        self.log = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            ["timeout", "--kill-after=5s", "120s", *args],
            cwd=self.directory,
            env=self.env,
            stdout=self.log,
            stderr=self.log,
        )

    def call(
        self, operation, data=None, request_id=None, authenticated=True, raw=None, method=None
    ):
        headers = {
            "Content-Type": "application/json",
            "X-Request-ID": request_id or secrets.token_hex(8),
        }
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        body = raw if raw is not None else json.dumps(data or {}).encode()
        method = method or ("GET" if operation == "health" else "POST")
        if method == "GET":
            body = None
        # This URL is always constructed from a numeric loopback address above.
        req = request.Request(  # noqa: S310
            self.url + "/benchmark/v1/" + operation, data=body, headers=headers, method=method
        )
        try:
            response = self.opener.open(req, timeout=40)
        except error.HTTPError as exc:
            response = exc
        with response:
            status = response.code
            content = response.read().decode()
            if response.headers.get_content_type() != "application/json":
                raise AssertionError(f"Non-JSON HTTP response: {status}, {content[:200]}")
        return status, json.loads(content)

    def ready(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(self.log_path.read_text())
            try:
                status, response = self.call("health")
                if status == 200:
                    return response
            except (OSError, error.URLError):
                time.sleep(0.1)
        raise TimeoutError("Engine did not start within 15 seconds")

    def close(self):
        if self.process.poll() is None:
            try:
                self.call("shutdown")
                self.process.wait(timeout=10)
            except (
                OSError,
                error.URLError,
                subprocess.TimeoutExpired,
                ValueError,
                AssertionError,
            ):
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        self.log.close()


class TestM1Interface(unittest.TestCase):
    def setUp(self):
        output = os.environ.get("ZERO_AD_TEST_OUTPUT")
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="m1-", dir=output))
        print(f"\nEvidence: {self.directory}", flush=True)
        self.engine = EngineProcess(self.directory / "primary")
        self.addCleanup(self.engine.close)
        self.engine.ready()
        self.config = json.loads(CONFIG.read_text())

    def ok(self, operation, data=None, **kwargs):
        status, response = self.engine.call(operation, data, **kwargs)
        self.assertEqual(status, 200, response)
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["protocol_version"], "1.1")
        return response

    def reset(self, seats=(1,), config=None):
        return self.ok(
            "reset",
            {"attributes": config or self.config, "seats": list(seats), "save_replay": True},
        )

    def record(self, name, value):
        (self.directory / name).write_text(json.dumps(value, indent=2) + "\n")

    def owned_handles(self, response, seat):
        data = response["data"]
        owned = [entity for entity in data["evaluator"]["entities"] if entity["owner"] == seat]
        view = data["players"][str(seat)]["own_entities"]
        self.assertEqual(len(owned), len(view))
        self.assertGreater(len(owned), 0)
        self.assertTrue(all(entity["template"] for entity in owned))
        self.assertEqual(
            [entity["template"] for entity in owned], [entity["template"] for entity in view]
        )
        names = sorted({entity["template"] for entity in owned})
        catalog = self.ok(
            "catalog", {"episode_id": response["episode_id"], "seat": seat, "templates": names}
        )["data"]["templates"]
        self.assertEqual(set(names), set(catalog))
        self.assertTrue(
            all("static" in entry and "effective" in entry for entry in catalog.values()), catalog
        )
        return {
            entity["id"]: observed["handle"] for entity, observed in zip(owned, view, strict=True)
        }

    def test_snapshots_seat_catalog_and_lifecycle(self):
        reset = self.reset(seats=(2,))
        self.record("reset-seat-2.json", reset)
        handles = self.owned_handles(reset, 2)
        episode = reset["episode_id"]
        self.assertEqual(reset["turn"], 0)
        self.assertEqual(reset["sim_time_ms"], 0)
        self.assertEqual(set(reset["data"]["players"]), {"2"})
        entities = reset["data"]["evaluator"]["entities"]
        self.assertGreater(len(entities), 1000)
        self.assertTrue(all(isinstance(entity, dict) for entity in entities))
        self.assertTrue(any(entity["owner"] == 2 and entity["health"] for entity in entities))
        observation = {"episode_id": episode, "audience": "player", "seat": 2}
        first = self.ok("observe", observation)
        self.record("player-seat-2.json", first)
        time.sleep(0.3)
        second = self.ok("observe", observation)
        self.assertEqual(first["data"], second["data"])
        self.assertEqual(second["sim_time_ms"], 0)
        self.assertNotIn("evaluator", second["data"])
        self.assertNotIn("statistics", second["data"]["self"])
        self.assertIsNone(second["data"]["self"]["population"]["max"])
        self.assertTrue(second["data"]["self"]["population"]["unlimited"])
        self.assertGreater(len(second["data"]["own_entities"]), 0)
        self.assertTrue(
            all(
                "id" not in entity and "orders" not in entity
                for entity in second["data"]["own_entities"]
            )
        )
        status, denied = self.engine.call("observe", {**observation, "seat": 1})
        self.assertEqual((status, denied["error"]["code"]), (403, "unbound_seat"))
        templates = [
            "structures/spart/house",
            "structures/spart/m1_missing",
            "../secret",
            "units/athen/support_civilian",
            "__proto__",
        ]
        catalog = self.ok(
            "catalog",
            {
                "episode_id": episode,
                "seat": 2,
                "templates": templates,
                "technologies": ["phase_town", "m1_missing", "__proto__"],
            },
        )["data"]
        self.assertEqual(set(catalog["templates"]), set(templates))
        self.record("catalog-seat-2.json", catalog)
        self.assertIn("cost", catalog["templates"][templates[0]]["static"])
        self.assertIn("health", catalog["templates"][templates[0]]["effective"])
        self.assertEqual(catalog["templates"][templates[1]]["error"], "not_found")
        self.assertEqual(catalog["templates"][templates[2]]["error"], "invalid_name")
        self.assertEqual(catalog["templates"][templates[3]]["error"], "not_permitted")
        self.assertIn("static", catalog["technologies"]["phase_town"])
        self.assertEqual(catalog["technologies"]["m1_missing"]["error"], "not_found")
        self.assertEqual(self.ok("observe", observation)["data"], first["data"])
        advance = {"episode_id": episode, "expected_turn": 0, "turns": 25}
        result = self.ok("advance", advance, request_id="advance-once")
        self.assertEqual((result["turn"], result["sim_time_ms"]), (25, 5000))
        self.assertEqual(self.owned_handles(result, 2), handles)
        self.assertEqual(self.ok("advance", advance, request_id="advance-once"), result)
        self.assertEqual(self.engine.call("advance", advance)[0], 409)
        self.assertEqual(
            self.engine.call("advance", {**advance, "turns": 2}, request_id="advance-once")[0], 409
        )
        final = self.ok("finalize", {"episode_id": episode})
        self.assertEqual(final["data"], self.ok("finalize", {"episode_id": episode})["data"])
        replay = Path(final["data"]["replay_directory"])
        self.assertTrue((replay / "metadata.json").is_file())
        self.assertEqual(
            len(
                [
                    line
                    for line in (replay / "commands.txt").read_text().splitlines()
                    if line.startswith("turn ")
                ]
            ),
            25,
        )
        self.assertEqual(self.engine.call("observe", observation)[0], 409)
        fresh = self.reset()
        self.assertNotEqual(episode, fresh["episode_id"])
        self.assertEqual(fresh["sim_time_ms"], 0)
        self.assertEqual(self.engine.call("observe", observation)[0], 409)
        self.ok("shutdown")
        self.assertEqual(self.engine.process.wait(timeout=10), 0)

    def test_errors_are_bounded_and_do_not_mutate(self):
        self.assertEqual(self.engine.call("health", authenticated=False)[0], 401)
        self.assertEqual(self.engine.call("reset", raw=b"{broken")[0], 400)
        self.assertEqual(self.engine.call("reset", raw=b"[]")[0], 400)
        self.assertEqual(self.engine.call("reset", raw=b"x" * (1024 * 1024 + 1))[0], 413)
        reset = self.reset()
        episode = reset["episode_id"]
        invalid = copy.deepcopy(self.config)
        invalid["map"] = "maps/scenarios/m1_does_not_exist"
        status, response = self.engine.call(
            "reset", {"attributes": invalid, "seats": [1], "save_replay": False}
        )
        self.assertEqual((status, response["error"]["code"]), (400, "map_not_found"))
        self.assertEqual(self.ok("health")["episode_id"], episode)
        for invalid_seats in [[], [0], [3], [1, 1], [1.5], [True], "1"]:
            with self.subTest(seats=invalid_seats):
                self.assertEqual(
                    self.engine.call(
                        "reset",
                        {"attributes": self.config, "seats": invalid_seats, "save_replay": False},
                    )[0],
                    400,
                )
        for invalid_players in [[None], [[1]], [{"Civ": "unknown", "AI": ""}]]:
            invalid = copy.deepcopy(self.config)
            invalid["settings"]["PlayerData"] = invalid_players
            self.assertEqual(
                self.engine.call(
                    "reset", {"attributes": invalid, "seats": [1], "save_replay": False}
                )[0],
                400,
            )
        self.assertEqual(self.ok("health")["episode_id"], episode)
        self.assertEqual(
            self.engine.call(
                "advance", {"episode_id": episode, "expected_turn": 0, "turns": 1, "commands": []}
            )[0],
            400,
        )
        self.assertEqual(self.ok("health")["turn"], 0)
        for endpoint in ["evaluate", "step", "templates"]:
            req = request.Request(self.engine.url + "/" + endpoint, data=b"0")  # noqa: S310
            with self.assertRaises(error.HTTPError) as caught:
                self.engine.opener.open(req, timeout=5)
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()
        observation = {"episode_id": episode, "audience": "player", "seat": 1}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(
                pool.map(lambda _: self.engine.call("observe", observation), range(12))
            )
        self.assertTrue(all(status == 200 for status, _ in responses))
        self.assertTrue(
            all(response["data"] == responses[0][1]["data"] for _, response in responses)
        )

    def test_startup_conflicts_fail_and_existing_process_survives(self):
        for name, kwargs in [
            ("port-conflict", {"port": self.engine.port}),
            ("missing-mod", {"benchmark_mod": False}),
            ("legacy-conflict", {"extra_args": ("--rl-interface",)}),
            ("network-conflict", {"extra_args": ("--autostart-host",)}),
        ]:
            with self.subTest(name=name):
                process = EngineProcess(self.directory / name, **kwargs)
                self.addCleanup(process.close)
                self.assertNotEqual(process.process.wait(timeout=10), 0)
        self.ok("health")

    def test_failed_load_retires_process(self):
        config = copy.deepcopy(self.config)
        config.update({"mapType": "random", "map": "maps/random/m1_failed_load"})
        status, failure = self.engine.call(
            "reset", {"attributes": config, "seats": [1], "save_replay": False}
        )
        self.assertEqual(status, 500, failure)
        self.assertEqual(self.ok("health")["state"], "failed")
        self.assertEqual(
            self.engine.call(
                "reset", {"attributes": self.config, "seats": [1], "save_replay": False}
            )[0],
            503,
        )
        self.ok("shutdown")
        self.assertEqual(self.engine.process.wait(timeout=10), 0)

    def test_petra_reads_preserve_events_and_future_behavior(self):
        base = copy.deepcopy(self.config)
        base["settings"]["TriggerScripts"].append("scripts/m1_command_trace.js")
        for player in base["settings"]["PlayerData"]:
            player.update({"AI": "petra", "AIDiff": 3, "AIBehavior": "balanced"})
        sequences = []
        traces = []
        for stress in [False, True]:
            config = copy.deepcopy(base)
            if stress:
                config["settings"]["TriggerScripts"].append("scripts/m1_snapshot_stress.js")
            log_start = len(self.engine.log_path.read_text())
            result = self.reset(seats=(1, 2), config=config)
            initial = result["data"]["evaluator"]
            handles = {seat: self.owned_handles(result, seat) for seat in (1, 2)}
            episode = result["episode_id"]
            sequence = []
            for _ in range(4):
                result = self.ok(
                    "advance",
                    {"episode_id": episode, "expected_turn": result["turn"], "turns": 100},
                )
                sequence.append(result["data"]["evaluator"])
                self.assertTrue(result["data"]["players"]["1"]["own_entities"])
                for seat in (1, 2):
                    updated = self.owned_handles(result, seat)
                    for entity in handles[seat].keys() & updated.keys():
                        self.assertEqual(handles[seat][entity], updated[entity])
                    handles[seat] = updated
                if stress:
                    for _ in range(5):
                        self.ok(
                            "observe", {"episode_id": episode, "audience": "player", "seat": 1}
                        )
            sequences.append(sequence)
            self.ok("finalize", {"episode_id": episode})
            trace_log = self.engine.log_path.read_text()[log_start:]
            traces.append(
                [
                    json.loads(line.removeprefix("M1_COMMAND "))
                    for line in trace_log.splitlines()
                    if line.startswith("M1_COMMAND ")
                ]
            )
            self.assertGreater(len(traces[-1]), 10, "Petra must actually issue commands")
            self.assertNotEqual(initial, sequence[-1], "Petra must change the world")
            if stress:
                self.assertEqual(trace_log.count("M1_STRESS verified"), 4)
                self.assertIn("M1_PENDING_EVENTS", trace_log)
                self.assertIn("M1_PENDING_ENTITIES", trace_log)
            self.record(f"petra-{'stress' if stress else 'baseline'}-trace.json", traces[-1])
            self.record(f"petra-{'stress' if stress else 'baseline'}-states.json", sequence)
        self.assertEqual(sequences[0], sequences[1])
        self.assertEqual(traces[0], traces[1])
        self.assertNotIn("ERROR:", self.engine.log_path.read_text())

    def test_load_deadline_and_queued_requests_are_bounded(self):
        config = copy.deepcopy(self.config)
        config.update({"mapType": "random", "map": "maps/random/m1_slow_load"})
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            loading = pool.submit(
                self.engine.call,
                "reset",
                {"attributes": config, "seats": [1], "save_replay": False},
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.ok("health")["state"] == "loading":
                    break
                time.sleep(0.05)
            else:
                self.fail("Reset did not start")
            queued = pool.submit(
                self.engine.call,
                "observe",
                {"episode_id": "no-episode", "audience": "player", "seat": 1},
            )
            status, failure = loading.result(timeout=40)
            self.assertEqual((status, failure["error"]["code"]), (504, "request_timeout"))
            self.assertIn(queued.result(timeout=10)[0], (503, 504))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.ok("health")["state"] == "failed":
                break
            time.sleep(0.05)
        else:
            self.fail("Timed-out loader did not retire the process")
        self.ok("shutdown")
        self.assertEqual(self.engine.process.wait(timeout=10), 0)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 34, "The loader must cancel before the fixture fallback at 35s")
        self.assertNotIn("reached its fallback deadline", self.engine.log_path.read_text())
        self.record("deadline.json", {"elapsed_seconds": elapsed, "response": failure})

    def test_logged_script_load_error_retires_process(self):
        config = copy.deepcopy(self.config)
        config["settings"]["TriggerScripts"].append("scripts/m1_script_error.js")
        status, failure = self.engine.call(
            "reset", {"attributes": config, "seats": [1], "save_replay": False}
        )
        self.assertEqual((status, failure["error"]["code"]), (500, "load_failed"))
        self.assertEqual(self.ok("health")["state"], "failed")

    def test_logged_turn_error_retires_process(self):
        config = copy.deepcopy(self.config)
        config["settings"]["TriggerScripts"].append("scripts/m1_turn_error.js")
        reset = self.reset(config=config)
        status, failure = self.engine.call(
            "advance", {"episode_id": reset["episode_id"], "expected_turn": 0, "turns": 2}
        )
        self.assertEqual((status, failure["error"]["code"]), (500, "advance_failed"))
        self.assertEqual(self.ok("health")["state"], "failed")

    def test_receipt_limit_preserves_retries_and_finalization(self):
        reset = self.reset()
        episode = reset["episode_id"]
        first = {"episode_id": episode, "expected_turn": 0, "turns": 1}
        saved = self.ok("advance", first, request_id="receipt-first")
        turn = saved["turn"]
        for _ in range(10):
            result = self.ok("advance", {"episode_id": episode, "expected_turn": turn, "turns": 1})
            turn += 1
            self.assertEqual(result["turn"], turn)
        status, expired = self.engine.call("advance", first, request_id="receipt-first")
        self.assertEqual((status, expired["error"]["code"]), (410, "receipt_expired"))
        self.assertEqual(self.ok("health")["turn"], turn)
        self.ok("finalize", {"episode_id": episode})
        fresh = self.reset()
        self.assertNotEqual(fresh["episode_id"], episode)
        self.ok("advance", {"episode_id": fresh["episode_id"], "expected_turn": 0, "turns": 1})

    def test_random_and_skirmish_maps_load(self):
        for map_type, map_path in [
            ("random", "maps/random/mainland"),
            ("skirmish", "maps/skirmishes/bactria_2p"),
        ]:
            with self.subTest(map_type=map_type):
                config = copy.deepcopy(self.config)
                config.update({"mapType": map_type, "map": map_path})
                config["settings"].update(
                    {"Biome": "generic/temperate", "Size": 128, "PlayerPlacement": "circle"}
                )
                result = self.reset(seats=(2,), config=config)
                self.assertEqual(result["state"], "running")
                self.owned_handles(result, 2)
                result = self.ok(
                    "advance", {"episode_id": result["episode_id"], "expected_turn": 0, "turns": 1}
                )
                self.assertEqual((result["turn"], result["sim_time_ms"]), (1, 200))
                self.ok("finalize", {"episode_id": result["episode_id"]})
        self.assertNotIn("ERROR:", self.engine.log_path.read_text())


if __name__ == "__main__":
    unittest.main()
