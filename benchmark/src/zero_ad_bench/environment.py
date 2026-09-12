"""Episode lifecycle: reset, synchronized decisions, exact advances, artifacts, finalize."""

import json
import secrets
import shutil
import threading
import time
import traceback
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from zero_ad_bench import PROTOCOL_VERSION, evaluation, report
from zero_ad_bench.agents import AgentStop, DecisionResult, NoOpController, ProviderFailure
from zero_ad_bench.engine import EngineError
from zero_ad_bench.scenario import INFORMATION_TRACKS, PHASE_ORDER
from zero_ad_bench.telemetry import EpisodeArtifacts


class Interrupted(Exception):  # noqa: N818
    """The operator asked the runner to stop; the partial trace is kept."""


class BudgetExceeded(Exception):  # noqa: N818
    """The controller exhausted its per-decision read budget."""


@dataclass
class RunOptions:
    decision_deadline_s: float = 30.0
    telemetry: bool = True
    save_replay: bool = True
    max_consecutive_failures: int = 3
    experiment_id: str = "local"
    label: str | None = None
    # A shortened horizon for smoke runs; scored runs must leave this unset.
    turn_limit_override: int | None = None


def phase_of(researched):
    for phase in reversed(PHASE_ORDER):
        if any(name.startswith("phase_" + phase) for name in researched):
            return phase
    return PHASE_ORDER[0]


class PlayerGateway:
    """The only surface a controller sees: one seat's frozen view plus read-only tools."""

    def __init__(self, episode, seat, decision_id, turn):
        self.episode = episode
        self.seat = seat
        self.decision_id = decision_id
        self.turn = turn
        self.reads = 0
        self.read_budget = episode.scenario.limits["reads_per_decision"]
        self.decision_turns = episode.scenario.decision_turns
        self.deadline_s = episode.options.decision_deadline_s
        self.view = episode.views[seat]

    def observation(self):
        return self.view

    def record(self, kind, payload):
        """Append a controller record (model call, tool call) to model-calls.jsonl."""
        self.episode.artifacts.append(
            "model-calls",
            {
                "kind": kind,
                "seat": self.seat,
                "decision_id": self.decision_id,
                "turn": self.turn,
                **payload,
            },
        )

    def _tool(self, operation, body, charged=True):
        if charged and self.reads >= self.read_budget:
            raise BudgetExceeded(f"Read budget of {self.read_budget} exhausted")
        if charged:
            self.reads += 1
        started = time.monotonic()
        status, envelope = self.episode.engine.call(operation, body)
        response = envelope.get("data") if envelope.get("ok") else {"error": envelope.get("error")}
        self.record(
            "tool",
            {
                "operation": operation,
                "charged": charged,
                "request": {k: v for k, v in body.items() if k != "episode_id"},
                "http_status": status,
                "response": response,
                "elapsed_s": round(time.monotonic() - started, 6),
            },
        )
        return response

    def _inspect_body(self, kind, fields):
        return {
            "episode_id": self.episode.episode_id,
            "seat": self.seat,
            "observation_id": self.view["observation_id"],
            "kind": kind,
            **fields,
        }

    def inspect(self, kind, **fields):
        return self._tool("inspect", self._inspect_body(kind, fields))

    def briefing(self, max_chars=32768, limit=64):
        """Return the initial text observation; it is supplied automatically, not charged.

        The largest page the engine allows is requested so the model rarely needs a second
        request to see the whole briefing; any remaining rows are stated in the header.
        """
        return self._tool(
            "inspect",
            self._inspect_body("briefing", {"max_chars": max_chars, "limit": limit}),
            charged=False,
        )

    def catalog(self, templates=(), technologies=()):
        return self._tool(
            "catalog",
            {
                "episode_id": self.episode.episode_id,
                "seat": self.seat,
                "templates": list(templates),
                "technologies": list(technologies),
            },
        )


class Episode:
    """Run one scenario with bound controllers and write the complete artifact set."""

    def __init__(self, scenario, controllers, engine, output_root, options=None, mod_sources=None):
        self.scenario = scenario
        self.controllers = controllers
        self.engine = engine
        self.output_root = Path(output_root)
        self.options = options or RunOptions()
        self.mod_sources = {name: str(path) for name, path in (mod_sources or {}).items()}
        self.seats = scenario.external_seats()
        if not self.seats:
            # A built-in-AI-only game (for example Petra versus Petra as a simulator sanity
            # check) still needs one bound seat to observe and advance; it submits nothing.
            self.seats = [min(scenario.controllers)]
            controllers = {
                **controllers,
                self.seats[0]: controllers.get(self.seats[0], NoOpController()),
            }
            self.controllers = controllers
        missing = [seat for seat in self.seats if seat not in controllers]
        if missing:
            raise ValueError(f"No controller bound for external seats {missing}")
        self.artifacts = None
        self.episode_id = None
        self.data = None
        self.views = {}
        self.turn = 0
        self.sim_time_ms = 0
        self.state = "ready"
        self.snapshots = []
        self.failures = dict.fromkeys(self.seats, 0)
        self.administrative = dict.fromkeys(self.seats)
        self.stopped_on_success = False
        self.interrupt_requested = False
        self.result = None
        self.engine_info = None

    def request_interrupt(self):
        self.interrupt_requested = True

    def _check_interrupt(self):
        if self.interrupt_requested:
            raise Interrupted

    def run(self):
        status = "completed"
        failure = None
        unexpected = None
        try:
            self._reset()
            self._boundary(0)
            while not self._stop_reason():
                self._check_interrupt()
                self._decide_and_advance()
            self._finalize()
        except EngineError as exc:
            status = "failed"
            failure = {**exc.record(), "stage": self.state, "turn": self.turn}
        except (Interrupted, KeyboardInterrupt):
            status = "interrupted"
        except Exception as exc:  # noqa: BLE001
            # A runner defect is an infrastructure failure: record it, keep the trace, re-raise.
            status = "failed"
            failure = {
                "kind": "runner_error",
                "code": type(exc).__name__,
                "message": str(exc)[:500],
                "traceback": traceback.format_exc()[-4000:],
                "stage": self.state,
                "turn": self.turn,
            }
            unexpected = exc
        finally:
            self._close(status, failure)
        if unexpected is not None:
            raise unexpected
        return self.result

    def _absorb(self, envelope):
        self.data = envelope["data"]
        self.turn = envelope["turn"]
        self.sim_time_ms = envelope["sim_time_ms"]
        self.state = envelope["state"]
        self.views = {seat: self.data["players"][str(seat)] for seat in self.seats}

    def _reset(self):
        scenario = self.scenario
        self.engine_info = self.engine.request("health")["data"]
        body = {
            "attributes": scenario.resolve(),
            "seats": list(self.seats),
            "save_replay": self.options.save_replay,
            "turn_limit": scenario.turn_limit,
            "information_mode": INFORMATION_TRACKS[scenario.information],
            "objective": scenario.public_objective(),
            "telemetry": self.options.telemetry,
        }
        try:
            envelope = self.engine.mutate("reset", body, f"reset-{secrets.token_hex(6)}")
        except EngineError:
            self.episode_id = "failed-" + secrets.token_hex(6)
            self._open_artifacts()
            raise
        self.episode_id = envelope["episode_id"]
        self._open_artifacts()
        self._absorb(envelope)
        self.artifacts.write_json(
            "resolved-scenario.json",
            {
                "scenario": scenario.describe(),
                "attributes": body["attributes"],
                "reset_request": {k: v for k, v in body.items() if k != "attributes"},
                "content_hashes": scenario.content_hashes(self.mod_sources),
                "replay_directory": self.data["replay_directory"],
                "engine": self.engine_info,
            },
        )

    def _open_artifacts(self):
        self.artifacts = EpisodeArtifacts(self.output_root / f"episode_{self.episode_id}")
        self.artifacts.start(
            {
                "experiment_id": self.options.experiment_id,
                "episode_id": self.episode_id,
                "scenario_id": self.scenario.id,
                "protocol_version": PROTOCOL_VERSION,
                "engine": self.engine_info,
                "engine_process": {"pid": self.engine.process.pid, "port": self.engine.port},
                "options": asdict(self.options),
                "seats": list(self.seats),
                "controllers": {str(s): self.controllers[s].name for s in self.seats},
                "mod_sources": self.mod_sources,
                "label": self.options.label,
            }
        )

    def _outcome(self):
        stop = self.data["stop_reason"] if self.data else ""
        states = self.data["evaluator"]["player_states"] if self.data else []
        return {
            "terminal_reason": stop or None,
            "terminated": bool(self.data and self.data["terminated"]),
            "truncated": bool(self.data and self.data["truncated"]),
            "turn": self.turn,
            "sim_time_ms": self.sim_time_ms,
            "player_states": {str(i + 1): s for i, s in enumerate(states)},
            "stopped_on_success": self.stopped_on_success,
        }

    def _stop_reason(self):
        if self.state == "terminal":
            return self.data["stop_reason"]
        objective = self.scenario.objective
        verdict = evaluation.evaluate(objective, self.snapshots, self._outcome())
        if verdict["success"] and objective["stop_on_success"]:
            self.stopped_on_success = True
            return "objective"
        return None

    def _boundary(self, decision_id):
        """Record the frozen views, evaluator snapshot, and hash of the current boundary."""
        for seat in self.seats:
            self.artifacts.append(
                "observations",
                {
                    "decision_id": decision_id,
                    "seat": seat,
                    "turn": self.turn,
                    "observation": self.views[seat],
                },
            )
        snapshot = self._snapshot(decision_id)
        self.snapshots.append(snapshot)
        self.artifacts.append("snapshots", snapshot)
        self.artifacts.append(
            "hashes",
            {
                "decision_id": decision_id,
                "turn": self.turn,
                "sim_time_ms": self.sim_time_ms,
                "hash": self.data["state_hash"],
                "scope": "full engine state at the decision boundary; benchmark caches excluded",
            },
        )

    def _snapshot(self, decision_id):
        evaluator = self.data["evaluator"]
        players = {}
        for player in evaluator["players"][1:]:
            seat = player["seat"]
            owned = [e for e in evaluator["entities"] if e["owner"] == seat]
            classes = Counter(c for e in owned for c in set(e["classes"]))
            workers = [e for e in owned if "Worker" in e["classes"] and not e["foundation"]]
            players[str(seat)] = {
                "state": player["state"],
                "civ": player["civ"],
                "resources": player["resources"],
                "population": player["population"],
                "phase": phase_of(player["researched"]),
                "researched": player["researched"],
                "research_queued": player["research_queued"],
                "statistics": player["statistics"],
                "entities": {
                    "total": len(owned),
                    "units": classes.get("Unit", 0),
                    "structures": classes.get("Structure", 0),
                    "foundations": sum(1 for e in owned if e["foundation"]),
                    "workers": len(workers),
                    "idle_workers": sum(1 for e in workers if e["idle"]),
                },
                "templates": dict(Counter(e["template"] for e in owned)),
                "classes": dict(classes),
                # Structure positions support region evaluators; units stay aggregated.
                "structures": [
                    {
                        "template": e["template"],
                        "x": e["position"]["x"],
                        "z": e["position"]["z"],
                        "foundation": bool(e["foundation"]),
                    }
                    for e in owned
                    if "Structure" in e["classes"] and e["position"]
                ],
            }
        return {
            "decision_id": decision_id,
            "turn": self.turn,
            "sim_time_ms": self.sim_time_ms,
            "state": self.state,
            "stop_reason": self.data["stop_reason"],
            "terminated": self.data["terminated"],
            "truncated": self.data["truncated"],
            "player_states": {str(i + 1): s for i, s in enumerate(evaluator["player_states"])},
            "players": players,
            "interval_metrics": self.data.get("interval_metrics", {}),
            "telemetry": self.data.get("telemetry"),
        }

    def _collect(self, controller, gateway):
        """Run one controller under the wall-clock deadline; simulated time stays paused."""
        holder = {}

        def work():
            try:
                holder["actions"] = controller.decide(gateway)
            except BaseException as exc:  # noqa: BLE001
                holder["error"] = exc

        started = time.monotonic()
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(self.options.decision_deadline_s)
        elapsed = round(time.monotonic() - started, 6)
        if thread.is_alive():
            return [], "timeout", elapsed, None, {}
        if "error" in holder:
            failure = holder["error"]
            if isinstance(failure, EngineError):
                raise failure
            if isinstance(failure, AgentStop):
                return [], "agent_stop", elapsed, failure.reason, {"stop_kind": failure.kind}
            if isinstance(failure, ProviderFailure):
                return [], "provider_failure", elapsed, json.dumps(failure.detail)[:500], {}
            return [], "controller_error", elapsed, repr(failure)[:500], {}
        actions = holder.get("actions")
        metadata = {}
        if isinstance(actions, DecisionResult):
            metadata = actions.metadata
            actions = actions.actions
        if not isinstance(actions, list) or not all(isinstance(a, dict) for a in actions):
            return [], "malformed", elapsed, None, metadata
        return actions, "submitted", elapsed, None, metadata

    def _decide_and_advance(self):
        decision_id = self.data["next_decision_id"]
        turn = self.turn
        batches = []
        for seat in self.seats:
            gateway = PlayerGateway(self, seat, decision_id, turn)
            actions, outcome, elapsed, error_text, metadata = self._collect(
                self.controllers[seat], gateway
            )
            # Provider outages are infrastructure: they neither count toward a forfeit nor
            # reset the streak. Budget stops end participation immediately.
            if outcome not in ("provider_failure", "agent_stop"):
                self.failures[seat] = 0 if outcome == "submitted" else self.failures[seat] + 1
            if self.administrative[seat]:
                actions = []
            elif outcome == "agent_stop":
                self.administrative[seat] = metadata.get("stop_kind", "agent_stop")
                actions = [{"action_id": "agent-stop", "type": "resign"}]
            elif self.failures[seat] >= self.options.max_consecutive_failures:
                self.administrative[seat] = "forfeit"
                outcome = "forfeit"
                actions = [{"action_id": "forfeit", "type": "resign"}]
            self.artifacts.append(
                "decisions",
                {
                    "decision_id": decision_id,
                    "seat": seat,
                    "turn": turn,
                    "observation_id": self.views[seat]["observation_id"],
                    "deadline_s": self.options.decision_deadline_s,
                    "elapsed_s": elapsed,
                    "outcome": outcome,
                    "error": error_text,
                    "action_count": len(actions),
                    "reads_used": gateway.reads,
                    "read_budget": gateway.read_budget,
                    "consecutive_failures": self.failures[seat],
                    "administrative": self.administrative[seat],
                    "metadata": metadata,
                },
            )
            batches.append({"seat": seat, "actions": actions})
        self._check_interrupt()
        body = {
            "episode_id": self.episode_id,
            "expected_turn": turn,
            "decision_id": decision_id,
            "turns": self.scenario.decision_turns,
            "batches": batches,
        }
        envelope = self.engine.mutate("advance", body, f"advance-{decision_id}")
        self._absorb(envelope)
        self._record_interval(decision_id)
        self._boundary(decision_id + 1)

    def _record_interval(self, decision_id):
        data = self.data
        for result in data["action_results"]:
            self.artifacts.append("actions", {"kind": "result", **result})
        for item in data["action_lifecycle"]:
            self.artifacts.append(
                "actions", {"kind": "lifecycle", "interval_decision_id": decision_id, **item}
            )
        ledger = data["ledger"]
        for event in ledger["events"]:
            self.artifacts.append("events", {"decision_id": decision_id, **event})
        if ledger["overflow"]:
            self.artifacts.append(
                "events",
                {
                    "decision_id": decision_id,
                    "seq": None,
                    "type": "ledger_overflow",
                    "turn": self.turn,
                    "sim_time_ms": self.sim_time_ms,
                    "dropped": ledger["overflow"],
                    "last_seq": ledger["last_seq"],
                },
            )
        for index, command in enumerate(data["command_trace"]):
            # A built-in AI or scripted command carries no agent decision; the interval's
            # decision ID is the join key, and the parent decision stays separately visible.
            self.artifacts.append(
                "events",
                {
                    **command,
                    "decision_id": decision_id,
                    "parent_decision_id": command.get("decision_id"),
                    "seq": None,
                    "command_index": index,
                    "type": "command",
                },
            )

    def _finalize(self):
        envelope = self.engine.mutate(
            "finalize", {"episode_id": self.episode_id}, f"finalize-{self.episode_id}"
        )
        replay = Path(envelope["data"]["replay_directory"])
        for name in ("commands.txt", "metadata.json"):
            if (replay / name).is_file():
                self.artifacts.copy_file(replay / name, f"replay/{name}")

    def _close(self, status, failure):
        if self.artifacts is None:
            return
        try:
            if self.engine.log_path.is_file():
                target = self.artifacts.directory / "engine-logs/engine.log"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.engine.log_path, target)
            override = {
                "status": status,
                "failure": failure,
                "stopped_on_success": self.stopped_on_success,
            }
            self.result = report.build_result(self.artifacts.directory, override)
            self.artifacts.write_json("result.json", self.result)
            text = report.build_report(self.artifacts.directory, self.result)
            self.artifacts.write_text("report.md", text)
        finally:
            self.artifacts.finish(
                status,
                failure=failure,
                stopped_on_success=self.stopped_on_success,
                result=self.result["result"] if self.result else None,
            )
