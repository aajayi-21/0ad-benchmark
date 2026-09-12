"""Controllers that act only through a seat's player gateway (no evaluator access)."""

import math
import time


class DecisionResult:
    """Actions plus controller metadata (plan, notes, usage) recorded with the decision."""

    def __init__(self, actions, metadata=None):
        self.actions = actions
        self.metadata = metadata or {}


class AgentStop(Exception):  # noqa: N818
    """The controller ends its participation (for example a budget stop).

    The runner records it administratively and submits a resign action.
    """

    def __init__(self, kind, reason):
        super().__init__(f"{kind}: {reason}")
        self.kind = kind
        self.reason = reason


class ProviderFailure(Exception):  # noqa: N818
    """A persistent provider outage: an infrastructure failure, not a strategic no-op."""

    def __init__(self, detail):
        super().__init__(str(detail))
        self.detail = detail


class NoOpController:
    name = "noop"

    def decide(self, gateway):  # noqa: ARG002
        return []


class SleepingController:
    """Miss every decision deadline; used to exercise timeout and forfeit accounting."""

    name = "sleep"

    def __init__(self, seconds):
        self.seconds = seconds

    def decide(self, gateway):  # noqa: ARG002
        time.sleep(self.seconds)
        return []


class ScriptedController:
    name = "scripted"

    def __init__(self, plan):
        self.plan = plan

    def decide(self, gateway):
        return self.plan(gateway)


def _distance(a, b):
    return math.hypot(a["x"] - b["x"], a["z"] - b["z"])


class RaidRecoveryController:
    """Deterministic economic baseline for the raid/recovery fixture.

    Every decision reads the frozen observation once, sends idle civilians back to unfinished
    foundations, keeps the rest gathering the nearest known wood, retrains civilians until the
    target count is reached, and on the first decision starts one house and one storehouse
    technology.
    """

    name = "raid_recovery"

    def __init__(self, target_workers=10):
        self.target_workers = target_workers

    def decide(self, gateway):
        view = gateway.observation()
        own = view["own_entities"]
        actions = []
        civilians = [e for e in own if e["template"].endswith("support_civilian")]
        centres = [e for e in own if e["template"].endswith("civil_centre")]
        storehouses = [e for e in own if e["template"].endswith("storehouse")]
        wood = [
            e
            for e in view["visible_entities"] + view["last_seen"]
            if e.get("resource")
            and e["resource"]["type"]["generic"] == "wood"
            and e.get("status", "visible") in ("visible", "last_seen")
        ]
        idle = [e for e in civilians if e["idle"] and e["position"]]
        if view["turn"] == 0:
            builders = [e["handle"] for e in idle[:2]]
            if builders and centres and centres[0]["position"]:
                base = centres[0]["position"]
                actions.append(
                    {
                        "action_id": "house",
                        "type": "build",
                        "units": builders,
                        "template": "structures/athen/house",
                        "position": {"x": base["x"] + 4, "z": base["z"] + 44},
                        "angle": 0,
                        "queued": False,
                        "autorepair": True,
                        "autocontinue": False,
                    }
                )
                idle = idle[2:]
            if storehouses and "gather_capacity_basket" in storehouses[0]["researchable"]:
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
                    "action_id": f"resume-{view['turn']}-{index}",
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
            target = min(wood, key=lambda e: _distance(e["position"], worker["position"]))
            actions.append(
                {
                    "action_id": f"gather-{view['turn']}-{index}",
                    "type": "gather",
                    "units": [worker["handle"]],
                    "target": target["handle"],
                    "queued": False,
                }
            )
        queued = sum(len(e["queue"]) for e in centres)
        if centres and not queued and len(civilians) < self.target_workers:
            actions.append(
                {
                    "action_id": f"train-{view['turn']}",
                    "type": "train",
                    "building": centres[0]["handle"],
                    "template": "units/athen/support_civilian",
                    "count": min(2, self.target_workers - len(civilians)),
                }
            )
        return actions[:20]


CONTROLLERS = {
    "noop": NoOpController,
    "raid_recovery": RaidRecoveryController,
}


def make_controller(spec):
    """Build a controller from a CLI spec such as `noop`, `raid_recovery`, or `sleep:2.5`."""
    name, _, argument = spec.partition(":")
    if name == "sleep":
        return SleepingController(float(argument or 5))
    if name not in CONTROLLERS:
        raise ValueError(f"Unknown controller {spec}")
    return CONTROLLERS[name]()
