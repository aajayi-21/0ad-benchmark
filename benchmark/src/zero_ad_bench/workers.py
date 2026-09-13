"""Killable Linux controller workers, with runner-owned, seat-bound gateway RPC.

A persistent worker owns one controller's private state. Only the runner services engine reads
and writes artifacts. A deadline kills the worker's process group, including command-provider
children; a subsequent decision restores the last checkpoint accepted by the runner.
"""

import copy
import ctypes
import multiprocessing
import os
import pickle
import signal
import time

from zero_ad_bench.agents import AgentStop, ProviderFailure
from zero_ad_bench.engine import EngineError


class DecisionExpired(TimeoutError):  # noqa: N818 - matches Interrupted/BudgetExceeded outcomes
    """The decision's gateway has expired or was revoked."""


def exception_record(exc):
    if isinstance(exc, EngineError):
        return {"kind": "engine", "detail": exc.record()}
    if isinstance(exc, ProviderFailure):
        return {"kind": "provider", "detail": exc.detail}
    if isinstance(exc, AgentStop):
        return {"kind": "agent_stop", "stop_kind": exc.kind, "message": exc.reason}
    return {"kind": "controller", "message": repr(exc)[:1000]}


def restore_exception(record):
    kind = record["kind"]
    if kind == "engine":
        detail = record["detail"]
        return EngineError(
            detail["kind"],
            detail["code"],
            detail["message"],
            detail.get("http_status"),
            detail.get("detail"),
        )
    if kind == "provider":
        return ProviderFailure(record["detail"])
    if kind == "agent_stop":
        return AgentStop(record["stop_kind"], record["message"])
    return RuntimeError(record["message"])


def checkpoint(controller):
    if hasattr(controller, "checkpoint"):
        return ("explicit", copy.deepcopy(controller.checkpoint()))
    try:
        # These are trusted Python controllers, never data deserialized from a model response.
        return ("pickle", pickle.dumps(controller))
    except (TypeError, AttributeError, pickle.PicklingError):
        return None


class WorkerGateway:
    """A controller receives only a frozen view and the allowlisted RPC methods."""

    def __init__(self, connection, context, controller):
        self.connection = connection
        self.controller = controller
        for name, value in context.items():
            setattr(self, name, value)

    def remaining_s(self):
        remaining = self.deadline_at - time.monotonic()
        if remaining <= 0:
            raise DecisionExpired("Decision deadline expired")
        return remaining

    def observation(self):
        self.remaining_s()
        return copy.deepcopy(self.view)

    def _rpc(self, operation, *args, **kwargs):
        self.remaining_s()
        state = checkpoint(self.controller) if operation == "record" else None
        self.connection.send(
            {"operation": operation, "args": args, "kwargs": kwargs, "checkpoint": state}
        )
        if not self.connection.poll(self.remaining_s()):
            raise DecisionExpired("Runner gateway deadline expired")
        reply = self.connection.recv()
        self.reads = reply["reads"]
        if "error" in reply:
            if reply["error"]["kind"] == "read_budget":
                # Import here to keep the transport independent of Episode's construction.
                from zero_ad_bench.environment import BudgetExceeded  # noqa: PLC0415

                raise BudgetExceeded(reply["error"]["message"])
            raise restore_exception(reply["error"])
        return reply["result"]

    def record(self, kind, payload):
        return self._rpc("record", kind, payload)

    def inspect(self, kind, **fields):
        return self._rpc("inspect", kind, **fields)

    def briefing(self, **fields):
        return self._rpc("briefing", **fields)

    def catalog(self, templates=(), technologies=()):
        return self._rpc("catalog", templates, technologies)


def _run(controller, connection, parent_pid):
    os.setsid()
    # Isolated groups must still die if the runner is killed without executing its finally.
    # PR_SET_PDEATHSIG is Linux-specific, like this worker transport. On TERM, kill the whole
    # owned group so command-provider descendants cannot outlive the runner either.
    signal.signal(signal.SIGTERM, lambda *_: os.killpg(os.getpgrp(), signal.SIGKILL))
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Could not bind worker lifetime to runner")
    if os.getppid() != parent_pid:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    try:
        while True:
            context = connection.recv()
            gateway = WorkerGateway(connection, context, controller)
            try:
                actions = controller.decide(gateway)
                result = {"actions": actions}
            except BaseException as exc:  # noqa: BLE001 - report, then let the runner classify
                result = {"error": exception_record(exc)}
            connection.send(
                {
                    "operation": "finished",
                    "finished": time.monotonic(),
                    "checkpoint": checkpoint(controller),
                    **result,
                }
            )
    except (EOFError, BrokenPipeError):
        pass  # The owning runner closed the channel; there is no work left to keep alive.
    finally:
        connection.close()


class ControllerWorker:
    """Persistent controller process. The parent controller holds the last accepted state."""

    def __init__(self, controller):
        self.controller = controller
        self.process = None
        self.connection = None
        self.restartable = True
        self.pending_request = None

    def begin(self, gateway):
        if self.process is None:
            if not self.restartable:
                return False
            # The benchmark's supported runtime is Linux. Fork also supports existing scripted
            # controllers containing local functions without a new serialization dependency.
            context = multiprocessing.get_context("fork")
            self.connection, child = context.Pipe()
            self.process = context.Process(
                target=_run, args=(self.controller, child, os.getpid()), daemon=True
            )
            self.process.start()
            child.close()
        self.pending_request = None
        self.connection.send(
            {
                name: getattr(gateway, name)
                for name in (
                    "seat",
                    "decision_id",
                    "turn",
                    "reads",
                    "read_budget",
                    "decision_turns",
                    "deadline_s",
                    "deadline_at",
                    "view",
                )
            }
        )
        return True

    def accept_checkpoint(self, state):
        if state is None:
            self.restartable = False
        elif state[0] == "explicit":
            self.controller.restore_checkpoint(state[1])
        else:
            self.controller = pickle.loads(state[1])  # noqa: S301 - our trusted worker only

    def stop(self):
        if self.process is None:
            return
        pid = self.process.pid
        try:
            # The worker creates this group before executing controller code. It may have
            # exited while its descendants remain, so signal the group even after its exit.
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            if self.process.is_alive():
                self.process.terminate()  # It has not reached setsid yet.
        self.process.join(0.2)
        try:
            # A descendant can ignore TERM even when the worker itself exited promptly.
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            if self.process.is_alive():
                self.process.kill()
        self.process.join(1)
        if self.process.is_alive():
            raise RuntimeError(f"Controller worker {pid} did not exit")
        self.process.close()
        self.connection.close()
        self.process = None
        self.connection = None
