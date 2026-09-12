"""Model scaffold gates for M5: mock provider episodes, budgets, retries, adapters, logging."""

import http.server
import json
import math
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "benchmark/src"
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))

from zero_ad_bench import report  # noqa: E402
from zero_ad_bench.engine import EngineProcess  # noqa: E402
from zero_ad_bench.environment import Episode, RunOptions  # noqa: E402
from zero_ad_bench.model_agent import (  # noqa: E402
    SYSTEM_PROMPT,
    TOOLS,
    ModelController,
    validate_actions,
)
from zero_ad_bench.providers import (  # noqa: E402
    COMMAND_PRESETS,
    AnthropicProvider,
    CommandProvider,
    MockProvider,
    OpenAICompatibleProvider,
    OpenRouterProvider,
    ProviderError,
    ProviderRequest,
    tool_schema_hash,
)
from zero_ad_bench.scenario import Scenario  # noqa: E402
from zero_ad_bench.telemetry import read_jsonl  # noqa: E402


FIXTURES = ROOT / "benchmark/tests/fixtures/m4"
SCENARIO = FIXTURES / "scenarios/raid_recovery_v1.json"
MOD_SOURCES = {"m4_fixture": FIXTURES}
EXPERIMENT = json.loads((ROOT / "benchmark/experiments/mock_pilot_v1.json").read_text())


def rows(text, prefix):
    return [
        json.loads(line.removeprefix(prefix))
        for line in text.splitlines()
        if line.startswith(prefix)
    ]


def briefing_of(messages):
    """Return the latest briefing text and its decision turn from the neutral messages."""
    for message in reversed(messages):
        if message["role"] == "user" and "Observation briefing:" in message["content"]:
            header, _, text = message["content"].partition("Observation briefing:\n")
            turn = int(header.split("at completed turn ")[1].split(" ")[0])
            return text, turn
    raise AssertionError("no briefing in the conversation")


def plan_actions(text, turn, researchable=None):
    """Compute the raid/recovery baseline from the compact briefing rows only."""
    own = rows(text, "Own entity: ")
    visible = rows(text, "Visible entity: ")
    civilians = [e for e in own if e["template"].endswith("support_civilian")]
    centres = [e for e in own if e["template"].endswith("civil_centre")]
    storehouses = [e for e in own if e["template"].endswith("storehouse")]
    wood = [e for e in visible if e["template"].startswith("gaia/tree")]
    idle = [e for e in civilians if e["idle"] and e["position"]]
    actions = []
    if turn == 0 and centres:
        base = centres[0]["position"]
        actions.append(
            {
                "action_id": "house",
                "type": "build",
                "units": [e["handle"] for e in idle[:2]],
                "template": "structures/athen/house",
                "position": {"x": base["x"] + 4, "z": base["z"] + 44},
                "angle": 0,
                "queued": False,
                "autorepair": True,
                "autocontinue": False,
            }
        )
        idle = idle[2:]
        if storehouses and researchable and "gather_capacity_basket" in researchable:
            actions.append(
                {
                    "action_id": "basket",
                    "type": "research",
                    "building": storehouses[0]["handle"],
                    "technology": "gather_capacity_basket",
                }
            )
    foundations = [e for e in own if e["foundation_progress"] is not None]
    for index, foundation in enumerate(foundations):
        builders = [e["handle"] for e in idle[:2]]
        if not builders:
            break
        idle = idle[2:]
        actions.append(
            {
                "action_id": f"resume-{turn}-{index}",
                "type": "repair",
                "units": builders,
                "target": foundation["handle"],
                "queued": False,
                "autocontinue": False,
            }
        )
    for index, worker in enumerate(idle):
        if not wood:
            break
        target = min(
            wood,
            key=lambda e: math.hypot(
                e["position"]["x"] - worker["position"]["x"],
                e["position"]["z"] - worker["position"]["z"],
            ),
        )
        actions.append(
            {
                "action_id": f"gather-{turn}-{index}",
                "type": "gather",
                "units": [worker["handle"]],
                "target": target["handle"],
                "queued": False,
            }
        )
    if centres and centres[0]["queued_items"] == 0 and len(civilians) < 10:
        actions.append(
            {
                "action_id": f"train-{turn}",
                "type": "train",
                "building": centres[0]["handle"],
                "template": "units/athen/support_civilian",
                "count": min(2, 10 - len(civilians)),
            }
        )
    return actions[:20]


class HeuristicPolicy:
    """A deterministic stand-in for a model: reads the conversation, calls tools, submits."""

    def __init__(self, json_mode=False, overrides=None):
        self.json_mode = json_mode
        self.overrides = overrides or {}

    def call(self, name, arguments):
        if self.json_mode:
            return {
                "text": json.dumps({"tool": name, "input": arguments}),
                "tool_calls": [],
                "stop_reason": "end_turn",
            }
        return {"tool_calls": [{"name": name, "input": arguments}]}

    def calls(self, pairs):
        if self.json_mode:
            return self.call(*pairs[-1])
        return {"tool_calls": [{"name": n, "input": a} for n, a in pairs]}

    def __call__(self, req, _call_index):
        text, turn = briefing_of(req.messages)
        decision = req.messages[0]["content"].split("Decision ")[1].split(" ")[0]
        override = self.overrides.get(int(decision))
        if override is not None:
            outcome = override(req, self)
            if outcome is not None:
                return outcome
        last = req.messages[-1]
        if turn == 0 and last["role"] == "user":
            own = rows(text, "Own entity: ")
            handles = [
                e["handle"] for e in own if e["template"].endswith(("storehouse", "civil_centre"))
            ]
            return self.call("inspect_entities", {"handles": handles})
        if turn == 0:
            records = self._last_tool_result(last)
            researchable = [t for r in records for t in (r.get("researchable") or [])]
            notes = "raid expected; keep civilians working; rebuild to ten. " * 200
            return self.calls(
                [
                    ("write_notes", {"text": notes}),
                    (
                        "submit_actions",
                        {
                            "actions": plan_actions(text, 0, researchable),
                            "plan": "build a house, research baskets, keep gathering",
                        },
                    ),
                ]
            )
        if last["role"] == "user":
            return self.calls(
                [
                    ("write_notes", {"text": f"turn {turn}: recovering"}),
                    (
                        "submit_actions",
                        {
                            "actions": plan_actions(text, turn),
                            "plan": "retrain civilians and keep gathering",
                        },
                    ),
                ]
            )
        return self.call("submit_actions", {"actions": [], "plan": None})

    @staticmethod
    def _last_tool_result(message):
        if message["role"] == "tool":
            payload = json.loads(message["results"][0]["content"])
        else:
            payload = json.loads(message["content"].split("Tool result:", 1)[-1])
        return payload.get("records", [])


class FakeServer(http.server.ThreadingHTTPServer):
    """Local provider stand-in: records wire bodies and replays scripted responses."""

    def __init__(self, script):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.script = list(script)
        self.requests = []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    def close(self):
        self.shutdown()
        self.server_close()


class FakeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        headers = {key.lower(): value for key, value in self.headers.items()}
        self.server.requests.append({"path": self.path, "headers": headers, "body": body})
        status, headers, payload = (
            self.server.script.pop(0)
            if self.server.script
            else (500, {}, {"error": "script exhausted"})
        )
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)


def anthropic_message(tool_calls=(), text="", stop="tool_use", model="claude-test"):
    content = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": f"toolu_{i}", "name": n, "input": a}
        for i, (n, a) in enumerate(tool_calls)
    ]
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 0,
        },
    }


def openai_completion(tool_calls=(), text=None, finish="tool_calls", model="gpt-test"):
    message = {
        "role": "assistant",
        "content": text,
        "tool_calls": [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": n, "arguments": json.dumps(a)},
            }
            for i, (n, a) in enumerate(tool_calls)
        ],
    }
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 100},
            "completion_tokens_details": {"reasoning_tokens": 7},
        },
    }


class TestM5Model(unittest.TestCase):
    def setUp(self):
        output = os.environ.get("ZERO_AD_TEST_OUTPUT")
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="m5-", dir=output))
        print(f"\nEvidence: {self.directory}", flush=True)
        self.scenario = Scenario.load(SCENARIO)

    def run_episode(self, controller, name, **options):
        engine = EngineProcess(
            self.directory / name,
            mods=self.scenario.mods,
            mod_sources=MOD_SOURCES,
            process_deadline_s=300,
        )
        self.addCleanup(engine.close)
        engine.ready()
        episode = Episode(
            self.scenario,
            {1: controller},
            engine,
            self.directory / "runs",
            RunOptions(**{"decision_deadline_s": 20, **options}),
            MOD_SOURCES,
        )
        result = episode.run()
        return episode, result, engine

    def streams(self, episode):
        directory = episode.artifacts.directory
        return {
            name: read_jsonl(directory / f"{name}.jsonl")[0]
            for name in ("decisions", "model-calls", "actions", "hashes")
        }

    def test_mock_model_completes_episode_with_exact_logging(self):
        controller = ModelController(MockProvider(HeuristicPolicy()), EXPERIMENT)
        episode, result, engine = self.run_episode(controller, "mock")
        self.assertEqual((result["status"], result["result"]), ("completed", "success"), result)
        streams = self.streams(episode)
        decisions = streams["decisions"]
        self.assertTrue(all(d["outcome"] == "submitted" for d in decisions), decisions)
        self.assertTrue(all(d["metadata"]["reason"] == "submitted" for d in decisions))
        first = decisions[0]["metadata"]
        self.assertTrue(first["notes_compacted"])
        self.assertEqual(len(first["notes"]), EXPERIMENT["notes"]["max_chars"])
        self.assertEqual(first["plan"], "build a house, research baskets, keep gathering")
        self.assertEqual(first["model_requests"], 2)
        self.assertEqual(decisions[1]["metadata"]["model_requests"], 1)
        self.assertEqual(first["system_prompt_hash"], controller.system_prompt_hash)
        self.assertEqual(first["tool_schema_hash"], tool_schema_hash(TOOLS))
        calls = streams["model-calls"]
        model_calls = [c for c in calls if c["kind"] == "model"]
        tools = [c for c in calls if c["kind"] == "tool"]
        self.assertEqual(len(model_calls), len(decisions) + 1)
        self.assertTrue(all(c["attempt"] == 1 and c["error"] is None for c in model_calls))
        self.assertTrue(all(c["request"]["system"] == SYSTEM_PROMPT for c in model_calls))
        self.assertTrue(
            all(c["cost_usd"] is not None and c["latency_s"] >= 0 for c in model_calls)
        )
        self.assertEqual(
            [c["request"]["messages"][0]["role"] for c in model_calls[:2]], ["user", "user"]
        )
        self.assertEqual(model_calls[1]["request"]["messages"][-1]["role"], "tool")
        self.assertIn("Your private notes", model_calls[2]["request"]["messages"][0]["content"])
        automatic = [c for c in tools if c["charged"] is False]
        charged = [c for c in tools if c["charged"]]
        self.assertEqual(len(automatic), len(decisions))
        # The whole briefing fits the automatic page for this fixture; nothing is omitted.
        self.assertTrue(
            all(c["response"]["omitted_count"] == 0 for c in automatic),
            automatic[0]["response"]["omitted_count"],
        )
        self.assertEqual(automatic[0]["request"]["limit"], 64)
        self.assertEqual([c["request"]["kind"] for c in charged], ["entities"])
        # Nothing privileged reaches the model: scan every request that was sent.
        forbidden = [
            engine.token,
            "state_hash",
            '"evaluator"',
            "AISeed",
            '"Seed"',
            "replay_directory",
            "command_trace",
            "interval_metrics",
            '"seat": 2',
        ]
        for call in model_calls:
            payload = json.dumps(call["request"])
            for needle in forbidden:
                self.assertNotIn(needle, payload, needle)
        usage = result["model_usage"]
        self.assertEqual(usage["attempts"], len(model_calls))
        self.assertEqual(usage["retries"], 0)
        self.assertGreater(usage["input_tokens"], 0)
        self.assertFalse(usage["cost_unavailable"])
        text = (episode.artifacts.directory / "report.md").read_text()
        self.assertIn("## Model usage", text)
        self.assertIn("Usability versus strategy", text)
        self.assertEqual(report.build_result(episode.artifacts.directory), result)
        manifest = json.loads((episode.artifacts.directory / "manifest.json").read_text())
        self.assertEqual(manifest["controllers"], {"1": "model"})
        self.hashes = [h["hash"] for h in streams["hashes"]]

    def test_json_fallback_matches_native_tool_path(self):
        native = ModelController(MockProvider(HeuristicPolicy()), EXPERIMENT)
        fallback = ModelController(
            MockProvider(HeuristicPolicy(json_mode=True), supports_tools=False), EXPERIMENT
        )
        self.assertNotEqual(native.system_prompt_hash, fallback.system_prompt_hash)
        results = {}
        for name, controller in (("native", native), ("json", fallback)):
            episode, result, _ = self.run_episode(controller, name)
            self.assertEqual(result["status"], "completed", result)
            streams = self.streams(episode)
            results[name] = {
                "hashes": [h["hash"] for h in streams["hashes"]],
                "actions": [
                    (a["decision_id"], a["action_id"], a["stage"])
                    for a in streams["actions"]
                    if a["kind"] == "result"
                ],
                "result": result["result"],
            }
            self.assertTrue(all(d["outcome"] == "submitted" for d in streams["decisions"]))
        self.assertEqual(results["native"], results["json"])
        calls = [c for c in self.streams(episode)["model-calls"] if c["kind"] == "model"]
        self.assertTrue(all(c["request"]["tools"] == [] for c in calls))
        self.assertTrue(all(c["response"]["tool_calls"] == [] for c in calls))

    def test_retries_failures_budgets_and_malformed_outputs(self):
        state = {"rate_limited": False}

        def rate_limit_once(req, policy):  # noqa: ARG001
            if not state["rate_limited"]:
                state["rate_limited"] = True
                return ProviderError(
                    "rate_limit", "slow down", retryable=True, status=429, retry_after_s=0.01
                )
            return None

        def text_only(req, policy):  # noqa: ARG001
            return {
                "text": "I am not sure what to do.",
                "tool_calls": [],
                "stop_reason": "end_turn",
            }

        def refusal(req, policy):  # noqa: ARG001
            return {"text": "", "tool_calls": [], "stop_reason": "refusal"}

        def bad_then_good(req, policy):
            if req.messages[-1]["role"] == "user":
                return policy.call("submit_actions", {"actions": "not a list", "plan": None})
            return policy.call("submit_actions", {"actions": [], "plan": "wait"})

        def persistent_client_error(req, policy):  # noqa: ARG001
            return ProviderError("client", "bad request", retryable=False, status=400)

        def read_forever(_req, policy):
            return policy.call("read_briefing", {"cursor": None, "max_chars": 2048})

        def overspend_reads(req, policy):
            if req.messages[-1]["role"] == "user":
                return policy.calls(
                    [("inspect_section", {"section": "own_entities", "cursor": None, "limit": 8})]
                    * 9
                )
            return policy.call("submit_actions", {"actions": [], "plan": "done"})

        overrides = {
            0: rate_limit_once,
            1: text_only,
            2: refusal,
            3: bad_then_good,
            4: persistent_client_error,
            5: read_forever,
            6: overspend_reads,
        }
        controller = ModelController(
            MockProvider(HeuristicPolicy(overrides=overrides)), EXPERIMENT
        )
        episode, result, _ = self.run_episode(controller, "faults")
        self.assertEqual(result["status"], "invalid", result)
        self.assertEqual(result["invalid_reasons"][0]["reason"], "provider_failure")
        streams = self.streams(episode)
        by_decision = {d["decision_id"]: d for d in streams["decisions"]}
        calls = [c for c in streams["model-calls"] if c["kind"] == "model"]
        first = [c for c in calls if c["decision_id"] == 0]
        self.assertEqual([(c["request_index"], c["attempt"]) for c in first[:2]], [(0, 1), (0, 2)])
        self.assertEqual(first[0]["error"]["kind"], "rate_limit")
        self.assertIsNone(first[1]["error"])
        submitted = [
            a["action_id"]
            for a in streams["actions"]
            if a["kind"] == "result" and a["decision_id"] == 0
        ]
        self.assertEqual(len(submitted), len(set(submitted)))
        self.assertGreater(len(submitted), 0)
        self.assertEqual(by_decision[0]["outcome"], "submitted")
        self.assertEqual(by_decision[1]["metadata"]["reason"], "no_submission")
        self.assertEqual(by_decision[2]["metadata"]["reason"], "refusal")
        self.assertEqual(by_decision[3]["metadata"]["reason"], "submitted")
        self.assertEqual(by_decision[3]["metadata"]["model_requests"], 2)
        self.assertEqual(by_decision[4]["outcome"], "provider_failure")
        self.assertEqual(by_decision[4]["consecutive_failures"], 0)
        self.assertEqual(by_decision[5]["metadata"]["reason"], "request_budget_exhausted")
        self.assertEqual(
            by_decision[5]["metadata"]["model_requests"],
            EXPERIMENT["budgets"]["model_requests_per_decision"],
        )
        overspent = next(c for c in calls if c["decision_id"] == 6)
        self.assertEqual(by_decision[6]["reads_used"], 8)
        second = [c for c in calls if c["decision_id"] == 6][1]
        results = second["request"]["messages"][-1]["results"]
        self.assertEqual([r["is_error"] for r in results], [False] * 8 + [True])
        self.assertIn("Read budget", results[-1]["content"])
        self.assertIsNone(overspent["error"])
        self.assertEqual(result["model_usage"]["retries"], 1)
        self.assertEqual(result["model_usage"]["error_kinds"], {"rate_limit": 1, "client": 1})
        self.assertIsNone(result["administrative"]["1"])

    def test_token_and_cost_ceilings_stop_the_agent(self):
        for ceiling in ({"episode_tokens": 3000}, {"episode_cost_usd": 0.001}):
            with self.subTest(ceiling=ceiling):
                config = json.loads(json.dumps(EXPERIMENT))
                config["budgets"].update(
                    {"episode_tokens": None, "episode_cost_usd": None, **ceiling}
                )
                controller = ModelController(MockProvider(HeuristicPolicy()), config)
                episode, result, _ = self.run_episode(controller, "ceiling-" + next(iter(ceiling)))
                self.assertEqual((result["status"], result["result"]), ("completed", "failure"))
                self.assertEqual(result["administrative"]["1"]["kind"], "budget_stop")
                self.assertEqual(result["terminal_reason"], "game_end")
                decisions = self.streams(episode)["decisions"]
                self.assertEqual(decisions[0]["outcome"], "submitted")
                self.assertEqual(decisions[1]["outcome"], "agent_stop")
                self.assertIn("ceiling", decisions[1]["error"])
                self.assertEqual(len(decisions), 2)
                self.assertEqual(result["player_states"]["1"], "defeated")

    def test_anthropic_and_openai_adapters_share_tool_semantics(self):
        submit = (
            "submit_actions",
            {"actions": [{"action_id": "w", "type": "wait"}], "plan": "hold"},
        )
        anthropic_server = FakeServer(
            [
                (
                    429,
                    {"retry-after": "1"},
                    {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
                ),
                (200, {}, anthropic_message([submit], text="Holding.")),
            ]
        )
        openai_server = FakeServer(
            [
                (500, {}, {"error": {"message": "boom"}}),
                (200, {}, openai_completion([submit], text="Holding.")),
            ]
        )
        self.addCleanup(anthropic_server.close)
        self.addCleanup(openai_server.close)
        anthropic = AnthropicProvider(
            "claude-test", api_key="test-key", base_url=anthropic_server.url, timeout_s=10
        )
        openai = OpenAICompatibleProvider(
            "gpt-test", api_key="test-key", base_url=openai_server.url + "/v1", timeout_s=10
        )
        messages = [
            {
                "role": "user",
                "content": "Decision 0 at completed turn 0. Observation briefing:\nx",
            },
            {
                "role": "assistant",
                "content": "Let me look.",
                "tool_calls": [
                    {"id": "c1", "name": "inspect_entities", "input": {"handles": ["own-1"]}}
                ],
            },
            {
                "role": "tool",
                "results": [
                    {
                        "id": "c1",
                        "name": "inspect_entities",
                        "content": '{"records": []}',
                        "is_error": False,
                    }
                ],
            },
        ]
        request = ProviderRequest(
            model="x",
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=TOOLS,
            max_output_tokens=4000,
            temperature=0.0,
        )
        outcomes = {}
        for name, provider, server in (
            ("anthropic", anthropic, anthropic_server),
            ("openai", openai, openai_server),
        ):
            with self.assertRaises(ProviderError) as caught:
                provider.complete(request)
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(len(server.requests), 1, "adapters must not retry on their own")
            response = provider.complete(request)
            self.assertEqual(len(server.requests), 2)
            outcomes[name] = (
                response.tool_calls[0]["name"],
                response.tool_calls[0]["input"],
                response.stop_reason,
                response.text,
                response.usage["input_tokens"],
                response.usage["output_tokens"],
                response.usage["cache_read_input_tokens"],
            )
        self.assertEqual(outcomes["anthropic"], outcomes["openai"])
        self.assertEqual(outcomes["anthropic"][2], "tool_use")
        wire = anthropic_server.requests[1]
        self.assertEqual(wire["path"], "/v1/messages")
        self.assertEqual(wire["headers"].get("x-api-key"), "test-key")
        self.assertIn("anthropic-version", wire["headers"])
        body = wire["body"]
        self.assertEqual(body["tool_choice"], {"type": "auto"})
        self.assertEqual([t["name"] for t in body["tools"]], [t.name for t in TOOLS])
        self.assertTrue(all(t["strict"] and "input_schema" in t for t in body["tools"]))
        self.assertEqual(body["max_tokens"], 4000)
        self.assertEqual(body["messages"][1]["content"][1]["type"], "tool_use")
        self.assertEqual(body["messages"][2]["content"][0]["type"], "tool_result")
        self.assertEqual(body["messages"][2]["content"][0]["tool_use_id"], "c1")
        self.assertEqual(body["system"], SYSTEM_PROMPT)
        self.assertNotIn("thinking", body)
        wire = openai_server.requests[1]
        self.assertEqual(wire["path"], "/v1/chat/completions")
        self.assertEqual(wire["headers"].get("authorization"), "Bearer test-key")
        body = wire["body"]
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual([t["function"]["name"] for t in body["tools"]], [t.name for t in TOOLS])
        self.assertEqual(body["messages"][0], {"role": "system", "content": SYSTEM_PROMPT})
        self.assertEqual(
            body["messages"][2]["tool_calls"][0]["function"]["name"], "inspect_entities"
        )
        self.assertEqual(
            body["messages"][3],
            {"role": "tool", "tool_call_id": "c1", "content": '{"records": []}'},
        )
        self.assertEqual(body["max_completion_tokens"], 4000)
        self.assertEqual(tool_schema_hash(TOOLS), tool_schema_hash(list(TOOLS)))

    def test_sdk_adapter_drives_a_full_episode_through_the_controller(self):
        wait = ("submit_actions", {"actions": [], "plan": "observe"})
        server = FakeServer([(200, {}, anthropic_message([wait], model="claude-fake"))] * 40)
        self.addCleanup(server.close)
        provider = AnthropicProvider(
            "claude-fake", api_key="test-key", base_url=server.url, timeout_s=10
        )
        config = json.loads(json.dumps(EXPERIMENT))
        config["provider"] = {"kind": "anthropic", "model": "claude-fake"}
        controller = ModelController(provider, config)
        episode, result, _ = self.run_episode(controller, "sdk")
        self.assertEqual((result["status"], result["result"]), ("completed", "failure"), result)
        decisions = self.streams(episode)["decisions"]
        self.assertEqual(len(decisions), 20)
        self.assertTrue(
            all(d["outcome"] == "submitted" and d["action_count"] == 0 for d in decisions)
        )
        self.assertEqual(len(server.requests), 20)
        usage = result["model_usage"]
        self.assertEqual((usage["attempts"], usage["responses"], usage["errors"]), (20, 20, 0))
        self.assertEqual(usage["models"], {"claude-fake": 20})
        self.assertEqual(usage["cache_read_input_tokens"], 2000)
        self.assertEqual(usage["input_tokens"], 2400)
        self.assertGreater(usage["cost_usd"], 0)
        self.assertEqual(server.requests[5]["body"]["messages"][0]["role"], "user")
        self.assertIn(
            "Decision 5 at completed turn 125",
            server.requests[5]["body"]["messages"][0]["content"],
        )

    def test_openrouter_adapter_uses_provider_cost_and_reasoning(self):
        submit = ("submit_actions", {"actions": [], "plan": "hold"})
        payload = openai_completion([submit], text="Holding.", model="anthropic/claude-test")
        payload["provider"] = "Anthropic"
        payload["usage"].update(
            {"cost": 0.0123, "cost_details": {"upstream_inference_cost": 0.01}}
        )
        server = FakeServer([(200, {"x-request-id": "gen-1"}, payload)])
        self.addCleanup(server.close)
        provider = OpenRouterProvider(
            "anthropic/claude-test",
            api_key="or-key",
            base_url=server.url + "/api/v1",
            reasoning_effort="high",
            app_title="bench",
            app_url="https://example.invalid",
            provider_routing={"order": ["Anthropic"]},
        )
        request = ProviderRequest(
            model="x",
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "Decision 0"}],
            tools=TOOLS,
            max_output_tokens=4000,
            temperature=None,
        )
        response = provider.complete(request)
        wire = server.requests[0]
        self.assertEqual(wire["path"], "/api/v1/chat/completions")
        self.assertEqual(wire["headers"]["authorization"], "Bearer or-key")
        self.assertEqual(wire["headers"]["x-title"], "bench")
        self.assertEqual(wire["headers"]["http-referer"], "https://example.invalid")
        body = wire["body"]
        self.assertEqual(body["max_tokens"], 4000)
        self.assertNotIn("max_completion_tokens", body)
        self.assertNotIn("temperature", body)
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(body["provider"], {"order": ["Anthropic"]})
        self.assertNotIn("models", body, "no fallback model list may be sent")
        self.assertEqual(response.usage["provider_cost_usd"], 0.0123)
        self.assertEqual(response.usage["reasoning_tokens"], 7)
        self.assertEqual(response.served_by, "Anthropic")
        self.assertEqual(response.request_id, "gen-1")
        self.assertEqual(response.model, "anthropic/claude-test")
        controller = ModelController(provider, EXPERIMENT)
        self.assertEqual(controller.cost(response.usage), 0.0123)
        self.assertEqual(
            controller.cost({**response.usage, "provider_cost_usd": None}),
            round(120 * 1.0 / 1e6 + 30 * 5.0 / 1e6 + 100 * 0.1 / 1e6, 8),
        )

    def fake_command(self, name, script):
        path = self.directory / "bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("#!/usr/bin/env python3\n" + script)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_local_command_provider_drives_a_full_episode(self):
        record = self.directory / "claude-calls.jsonl"
        fake_claude = self.fake_command(
            "claude",
            f"""
import json, sys
prompt = sys.stdin.read()
argv = sys.argv[1:]
with open({str(record)!r}, "a") as handle:
    handle.write(json.dumps({{"argv": argv, "prompt": prompt}}) + "\\n")
call = {{"tool": "submit_actions", "input": {{"actions": [], "plan": "cli wait"}}}}
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
    "result": json.dumps(call), "session_id": "s", "total_cost_usd": 0.0123,
    "usage": {{"input_tokens": 500, "output_tokens": 20, "cache_read_input_tokens": 400,
               "cache_creation_input_tokens": 0}},
    "modelUsage": {{"claude-fake": {{"inputTokens": 500}}}}}}))
""",
        )
        preset = COMMAND_PRESETS["claude_code"]
        argv = [str(fake_claude) if arg == "claude" else arg for arg in preset["argv"]]
        provider = CommandProvider(
            "claude-fake",
            argv=argv,
            parser=preset["parser"],
            prompt_via=preset["prompt_via"],
            timeout_s=30,
            workdir=self.directory / "cli-work",
        )
        config = json.loads(json.dumps(EXPERIMENT))
        config["provider"] = {"kind": "command", "preset": "claude_code", "model": "claude-fake"}
        controller = ModelController(provider, config)
        self.assertFalse(controller.supports_tools)
        episode, result, _ = self.run_episode(controller, "cli")
        self.assertEqual((result["status"], result["result"]), ("completed", "failure"), result)
        decisions = self.streams(episode)["decisions"]
        self.assertEqual(len(decisions), 20)
        self.assertTrue(all(d["outcome"] == "submitted" for d in decisions))
        calls, _ = read_jsonl(record)
        self.assertEqual(len(calls), 20)
        first = calls[0]
        self.assertIn("--bare", first["argv"])
        self.assertEqual(first["argv"][first["argv"].index("--tools") + 1], "")
        self.assertEqual(
            first["argv"][first["argv"].index("--system-prompt") + 1], controller.system
        )
        self.assertEqual(first["argv"][first["argv"].index("--model") + 1], "claude-fake")
        self.assertIn("Observation briefing:", first["prompt"])
        self.assertTrue(
            first["prompt"].endswith("Reply with exactly one JSON object for your next tool call.")
        )
        self.assertNotIn(controller.system, first["prompt"])
        usage = result["model_usage"]
        self.assertEqual(usage["attempts"], 20)
        self.assertAlmostEqual(usage["cost_usd"], 0.0123 * 20, places=6)
        self.assertEqual(usage["cache_read_input_tokens"], 8000)
        self.assertEqual(usage["models"], {"claude-fake": 20})
        model_calls = [c for c in self.streams(episode)["model-calls"] if c["kind"] == "model"]
        self.assertTrue(all(c["cost_source"] == "provider" for c in model_calls))
        self.assertTrue(all(c["response"]["served_by"] == "local_command" for c in model_calls))
        self.assertTrue(all(c["request"]["tools"] == [] for c in model_calls))

    def test_last_message_file_parser_and_command_failures(self):
        fake_codex = self.fake_command(
            "codex",
            """
import sys
args = sys.argv[1:]
target = args[args.index("--output-last-message") + 1]
prompt = args[-1]
assert "### System" in prompt and "### User" in prompt, prompt[:200]
open(target, "w").write('{"tool": "submit_actions", "input": {"actions": [], "plan": "codex"}}')
print("codex jsonl noise")
""",
        )
        preset = COMMAND_PRESETS["codex"]
        argv = [str(fake_codex) if arg == "codex" else arg for arg in preset["argv"]]
        provider = CommandProvider(
            "codex-fake",
            argv=argv,
            parser=preset["parser"],
            prompt_via=preset["prompt_via"],
            system_in_prompt=preset["system_in_prompt"],
            timeout_s=30,
            workdir=self.directory / "codex-work",
        )
        request = ProviderRequest(
            model="x",
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "Decision 0"}],
            tools=[],
            max_output_tokens=100,
        )
        response = provider.complete(request)
        self.assertEqual(json.loads(response.text)["tool"], "submit_actions")
        self.assertIsNone(response.usage["input_tokens"])
        self.assertIsNone(response.usage["provider_cost_usd"])
        failing = self.fake_command("failing", "import sys; sys.stderr.write('boom'); sys.exit(3)")
        broken = CommandProvider(
            "x",
            argv=[str(failing)],
            parser="raw",
            timeout_s=30,
            workdir=self.directory / "fail-work",
        )
        with self.assertRaises(ProviderError) as caught:
            broken.complete(request)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.status, 3)
        self.assertIn("boom", caught.exception.message)

    def test_action_validation_reports_specific_errors_and_fills_defaults(self):
        good, errors = validate_actions(
            [
                {"action_id": "g1", "type": "gather", "units": ["own-4"], "target": "seen-16"},
                {
                    "action_id": "b1",
                    "type": "build",
                    "units": ["own-5"],
                    "template": "structures/athen/house",
                    "position": {"x": 1, "z": 2},
                },
            ]
        )
        self.assertEqual(errors, [])
        self.assertEqual(good[0]["queued"], False)
        self.assertEqual(
            (good[1]["angle"], good[1]["autorepair"], good[1]["autocontinue"]), (0, True, False)
        )
        _, errors = validate_actions(
            [
                {
                    "action_id": "a1",
                    "type": "gather",
                    "units": ["own-4"],
                    "target": "seen-1",
                    "type_note": None,
                },
                {"action_id": "a1", "type": "train", "building": "own-1", "template": "x"},
                {"type": "dance"},
                "not an object",
            ]
        )
        self.assertEqual(len(errors), 6, errors)
        self.assertIn("unknown field(s) ['type_note']", errors[0])
        self.assertIn("duplicate action_id", errors[1])
        self.assertIn("missing required field(s) ['count']", errors[2])
        self.assertIn("action_id must be a non-empty string", errors[3])
        self.assertIn("unknown type 'dance'", errors[4])
        self.assertIn("must be an object", errors[5])
        self.assertEqual(validate_actions("nope"), ([], ["actions must be a list of objects"]))
        self.assertEqual(validate_actions([{}] * 21)[1][0][:11], "at most 20 ")

        def sloppy_then_fixed(req, policy):
            last = req.messages[-1]
            if last["role"] == "user":
                return policy.call(
                    "submit_actions",
                    {
                        "actions": [
                            {
                                "action_id": "m1",
                                "type": "move",
                                "units": ["own-12"],
                                "position": {"x": 180, "z": 150},
                                "extra": 1,
                            }
                        ],
                        "plan": "first try",
                    },
                )
            if last["role"] == "tool" and last["results"][-1]["is_error"]:
                self.assertIn("unknown field(s) ['extra']", last["results"][-1]["content"])
                return policy.call(
                    "submit_actions",
                    {
                        "actions": [
                            {
                                "action_id": "m1",
                                "type": "move",
                                "units": ["own-12"],
                                "position": {"x": 180, "z": 150},
                            }
                        ],
                        "plan": "fixed",
                    },
                )
            return None

        def never_fixed(req, policy):  # noqa: ARG001
            return policy.call(
                "submit_actions", {"actions": [{"action_id": "x", "type": "dance"}], "plan": None}
            )

        controller = ModelController(
            MockProvider(HeuristicPolicy(overrides={1: sloppy_then_fixed, 2: never_fixed})),
            EXPERIMENT,
        )
        episode, result, _ = self.run_episode(controller, "validation")
        self.assertEqual(result["status"], "completed", result)
        decisions = self.streams(episode)["decisions"]
        self.assertEqual(decisions[1]["metadata"]["reason"], "submitted")
        self.assertEqual(decisions[1]["metadata"]["submission_errors"], 1)
        self.assertEqual(decisions[1]["metadata"]["model_requests"], 2)
        self.assertEqual(decisions[1]["metadata"]["plan"], "fixed")
        actions = [
            a
            for a in self.streams(episode)["actions"]
            if a["kind"] == "result" and a["decision_id"] == 1
        ]
        self.assertEqual([(a["action_id"], a["stage"]) for a in actions], [("m1", "applied")])
        self.assertEqual(decisions[2]["metadata"]["reason"], "invalid_submission")
        self.assertEqual(
            decisions[2]["metadata"]["submission_errors"],
            EXPERIMENT["budgets"]["model_requests_per_decision"],
        )
        self.assertEqual(decisions[2]["action_count"], 0)
        self.assertEqual(decisions[2]["outcome"], "submitted")


if __name__ == "__main__":
    unittest.main()
