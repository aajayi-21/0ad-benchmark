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


def _own(view, suffix=None, cls=None):
    return [
        e
        for e in view["own_entities"]
        if (suffix is None or e["template"].endswith(suffix))
        and (cls is None or cls in e["classes"])
    ]


def _known(view, owner=None, cls=None, suffix=None, resource=None, visible_only=False):
    """Visible and remembered entities filtered by owner, class, template, or resource type."""
    pool = list(view["visible_entities"]) + ([] if visible_only else list(view["last_seen"]))
    result = []
    for e in pool:
        if e.get("status") in ("destroyed", "not_present_at_last_position"):
            continue
        if owner is not None and e.get("owner") != owner:
            continue
        if cls is not None and cls not in e.get("classes", []):
            continue
        if suffix is not None and not e["template"].endswith(suffix):
            continue
        if resource is not None and (
            not e.get("resource") or e["resource"]["type"]["generic"] != resource
        ):
            continue
        if e.get("position") is None:
            continue
        result.append(e)
    return result


def _nearest(candidates, point):
    return min(candidates, key=lambda e: _distance(e["position"], point)) if candidates else None


def _gather_orders(view, workers, wood_share=0.75, prefix="g"):
    """Send idle workers to the nearest known wood or food; returns the actions."""
    wood = _known(view, resource="wood")
    food = _known(view, resource="food")
    actions = []
    for index, worker in enumerate(workers):
        want_wood = wood and (not food or index < round(len(workers) * wood_share))
        target = _nearest(wood if want_wood else food, worker["position"])
        if target is None:
            continue
        actions.append(
            {
                "action_id": f"{prefix}-{view['turn']}-{index}",
                "type": "gather",
                "units": [worker["handle"]],
                "target": target["handle"],
                "queued": False,
            }
        )
    return actions


def _resume_foundations(view, idle, prefix="r"):
    actions = []
    for index, foundation in enumerate(
        e for e in view["own_entities"] if e["foundation_progress"] is not None
    ):
        builders = [e["handle"] for e in idle[:2]]
        if not builders:
            break
        del idle[:2]
        actions.append(
            {
                "action_id": f"{prefix}-{view['turn']}-{index}",
                "type": "repair",
                "units": builders,
                "target": foundation["handle"],
                "queued": False,
                "autocontinue": False,
            }
        )
    return actions


class EconomyController:
    """Scripted economic baseline: gather, train civilians, build Village structures, phase up."""

    name = "economy"
    slots = 8

    def __init__(self, target_workers=14):
        self.target_workers = target_workers
        self.slot = 0

    def decide(self, gateway):
        view = gateway.observation()
        actions = []
        civilians = _own(view, "support_civilian")
        centres = _own(view, "civil_centre")
        idle = [e for e in civilians if e["idle"] and e["position"]]
        actions += _resume_foundations(view, idle)
        resources = view["self"]["resources"]
        villages = [e for e in view["own_entities"] if "Village" in e["classes"]]
        completed = [e for e in villages if e["foundation_progress"] is None]
        in_progress = [e for e in villages if e["foundation_progress"] is not None]
        centre = centres[0] if centres else None
        if (
            centre
            and centre["position"]
            and len(completed) + len(in_progress) < 5
            and resources["wood"] >= 80
            and not in_progress
            and idle
        ):
            angle = 2 * math.pi * self.slot / self.slots
            self.slot = (self.slot + 1) % self.slots
            position = {
                "x": centre["position"]["x"] + 36 * math.cos(angle),
                "z": centre["position"]["z"] + 36 * math.sin(angle),
            }
            builders = [e["handle"] for e in idle[:2]]
            del idle[:2]
            actions.append(
                {
                    "action_id": f"house-{view['turn']}",
                    "type": "build",
                    "units": builders,
                    "template": f"structures/{view['self']['civ']}/house",
                    "position": position,
                    "angle": 0,
                    "queued": False,
                    "autorepair": True,
                    "autocontinue": False,
                }
            )
        # Food first while civilians are few, wood afterwards.
        share = 0.35 if resources["food"] < 300 else 0.7
        actions += _gather_orders(view, idle, wood_share=share)
        phase = next(
            (t for t in (centre["researchable"] if centre else []) if t.startswith("phase_town")),
            None,
        )
        if (
            centre
            and phase
            and len(completed) >= 5
            and resources["food"] >= 500
            and resources["wood"] >= 500
            and not centre["queue"]
        ):
            actions.append(
                {
                    "action_id": f"phase-{view['turn']}",
                    "type": "research",
                    "building": centre["handle"],
                    "technology": phase,
                }
            )
        elif (
            centre
            and not centre["queue"]
            and len(civilians) < self.target_workers
            and resources["food"] >= 100
            and not (len(completed) >= 5 and resources["food"] < 600)
        ):
            actions.append(
                {
                    "action_id": f"train-{view['turn']}",
                    "type": "train",
                    "building": centre["handle"],
                    "template": f"units/{view['self']['civ']}/support_civilian",
                    "count": 2,
                }
            )
        return actions[:20]


class DefenseController:
    """Scripted defensive baseline: cavalry on siege, infantry on escorts, civilians garrisoned.

    Between waves it unloads, repairs damaged structures, retrains cavalry, and keeps gathering.
    """

    name = "defense"

    def decide(self, gateway):
        view = gateway.observation()
        actions = []
        centres = _own(view, "civil_centre")
        centre = centres[0] if centres and centres[0]["position"] else None
        civilians = _own(view, "support_civilian")
        cavalry = [e for e in _own(view, cls="Cavalry") if e["position"]]
        infantry = [e for e in _own(view, cls="Infantry") if e["position"]]
        if centre is None:
            return actions
        enemies = [
            e
            for e in _known(view, owner=2, cls="Unit", visible_only=True)
            if _distance(e["position"], centre["position"]) < 160
        ]
        if enemies:
            siege = [e for e in enemies if "Siege" in e["classes"]]
            others = [e for e in enemies if "Siege" not in e["classes"]]
            plans = [("cav", cavalry, siege or others), ("inf", infantry, others or siege)]
            for label, group, targets in plans:
                if group and targets:
                    target = _nearest(targets, group[0]["position"])
                    actions.append(
                        {
                            "action_id": f"atk-{label}-{view['turn']}",
                            "type": "attack",
                            "units": [e["handle"] for e in group],
                            "target": target["handle"],
                            "queued": False,
                            "allow_capture": False,
                        }
                    )
            outside = [e["handle"] for e in civilians if e["position"] is not None]
            inside = [e["handle"] for e in civilians if e["holder"] == centre["handle"]]
            if others and outside:
                actions.append(
                    {
                        "action_id": f"garrison-{view['turn']}",
                        "type": "garrison",
                        "units": outside[:20],
                        "holder": centre["handle"],
                        "queued": False,
                    }
                )
            elif not others:
                # Only siege remains: rams cannot strike units, so civilians repair the centre.
                if inside:
                    actions.append(
                        {
                            "action_id": f"unload-{view['turn']}",
                            "type": "unload",
                            "units": inside[:20],
                            "holder": centre["handle"],
                        }
                    )
                if outside:
                    actions.append(
                        {
                            "action_id": f"mend-{view['turn']}",
                            "type": "repair",
                            "units": outside[:20],
                            "target": centre["handle"],
                            "queued": False,
                            "autocontinue": True,
                        }
                    )
        else:
            inside = [e["handle"] for e in civilians if e["holder"] == centre["handle"]]
            if inside:
                actions.append(
                    {
                        "action_id": f"unload-{view['turn']}",
                        "type": "unload",
                        "units": inside[:20],
                        "holder": centre["handle"],
                    }
                )
            idle = [e for e in civilians if e["idle"] and e["position"]]
            actions += _resume_foundations(view, idle)
            damaged = [
                e
                for e in view["own_entities"]
                if "Structure" in e["classes"]
                and e["health"]
                and e["health"]["current"] < e["health"]["max"] * 0.9
                and e["foundation_progress"] is None
            ]
            if damaged and idle:
                repairers = [e["handle"] for e in idle[:2]]
                del idle[:2]
                actions.append(
                    {
                        "action_id": f"repair-{view['turn']}",
                        "type": "repair",
                        "units": repairers,
                        "target": damaged[0]["handle"],
                        "queued": False,
                        "autocontinue": True,
                    }
                )
            actions += _gather_orders(view, idle)
            resources = view["self"]["resources"]
            if (
                not centre["queue"]
                and len(cavalry) < 8
                and resources["food"] >= 120
                and resources["wood"] >= 60
            ):
                template = f"units/{view['self']['civ']}/cavalry_swordsman_b"
                if template in centre["trainable"]:
                    actions.append(
                        {
                            "action_id": f"train-{view['turn']}",
                            "type": "train",
                            "building": centre["handle"],
                            "template": template,
                            "count": 2,
                        }
                    )
            idle_soldiers = [e for e in cavalry + infantry if e["idle"]]
            if idle_soldiers:
                actions.append(
                    {
                        "action_id": f"guard-{view['turn']}",
                        "type": "move",
                        "units": [e["handle"] for e in idle_soldiers],
                        "position": {
                            "x": centre["position"]["x"] + 20,
                            "z": centre["position"]["z"] + 20,
                        },
                        "queued": False,
                    }
                )
        return actions[:20]


class ExpansionController:
    """Scripted expansion baseline: explore the target region, then build an outpost there.

    Foundations cannot be placed on unexplored ground, so a scout is sent first; builders follow
    once the target cell is no longer unknown, rotating through offsets when a spot is blocked.
    """

    name = "expansion"

    def __init__(self, bounds=None):
        self.bounds = bounds or {"min_x": 352, "min_z": 208, "max_x": 448, "max_z": 304}
        self.attempt = 0

    def explored(self, view, x, z):
        size = view["map"]["cell_size"]
        side = math.ceil(view["map"]["bounds"]["max_x"] / size)
        index = int(z // size) * side + int(x // size)
        cells = view["map"]["cells"]
        return 0 <= index < len(cells) and cells[index]["visibility"] != "unknown"

    def decide(self, gateway):
        view = gateway.observation()
        actions = []
        civilians = _own(view, "support_civilian")
        idle = [e for e in civilians if e["idle"] and e["position"]]
        centre_x = (self.bounds["min_x"] + self.bounds["max_x"]) / 2
        centre_z = (self.bounds["min_z"] + self.bounds["max_z"]) / 2
        inside = [
            e
            for e in _own(view, "outpost")
            if e["position"]
            and self.bounds["min_x"] <= e["position"]["x"] < self.bounds["max_x"]
            and self.bounds["min_z"] <= e["position"]["z"] < self.bounds["max_z"]
        ]
        actions += _resume_foundations(view, idle)
        scouts = [e for e in _own(view, cls="Cavalry") if e["position"]]
        if (
            not inside
            and scouts
            and scouts[0]["idle"]
            and not self.explored(view, centre_x, centre_z)
        ):
            actions.append(
                {
                    "action_id": f"explore-{view['turn']}",
                    "type": "move",
                    "units": [scouts[0]["handle"]],
                    "position": {"x": centre_x, "z": centre_z},
                    "queued": False,
                }
            )
        foundations = [e for e in view["own_entities"] if e["foundation_progress"] is not None]
        if (
            not inside
            and not foundations
            and view["self"]["resources"]["wood"] >= 60
            and self.explored(view, centre_x, centre_z)
        ):
            # Builders come off gathering if nobody is idle; the order interrupts their task.
            pool = idle or [e for e in civilians if e["position"]]
            builders = [e["handle"] for e in pool[:3]]
            idle = [e for e in idle if e["handle"] not in builders]
            offsets = [
                (0, 0),
                (24, 0),
                (-24, 0),
                (0, 24),
                (0, -24),
                (24, 24),
                (-24, -24),
                (24, -24),
            ]
            dx, dz = offsets[self.attempt % len(offsets)]
            self.attempt += 1
            actions.append(
                {
                    "action_id": f"outpost-{view['turn']}",
                    "type": "build",
                    "units": builders,
                    "template": f"structures/{view['self']['civ']}/outpost",
                    "position": {"x": centre_x + dx, "z": centre_z + dz},
                    "angle": 0,
                    "queued": False,
                    "autorepair": True,
                    "autocontinue": False,
                }
            )
        actions += _gather_orders(view, idle)
        return actions[:20]


class ScoutStrikeController:
    """Scripted scouting baseline: the cavalry sweeps candidate sites; soldiers strike on sight."""

    name = "scout_strike"

    def decide(self, gateway):
        view = gateway.observation()
        actions = []
        centres = _own(view, "civil_centre")
        centre = centres[0] if centres and centres[0]["position"] else None
        cavalry = [e for e in _own(view, cls="Cavalry") if e["position"]]
        soldiers = [e for e in _own(view, cls="Infantry") if e["position"]]
        if centre is None:
            return actions
        targets = _known(view, owner=2, cls="Unit")
        visible = _known(view, owner=2, cls="Unit", visible_only=True)
        if visible and soldiers:
            actions.append(
                {
                    "action_id": f"strike-{view['turn']}",
                    "type": "attack",
                    "units": [e["handle"] for e in soldiers],
                    "target": visible[0]["handle"],
                    "queued": False,
                    "allow_capture": False,
                }
            )
        elif targets and soldiers:
            point = targets[0]["position"]
            actions.append(
                {
                    "action_id": f"approach-{view['turn']}",
                    "type": "attack_move",
                    "units": [e["handle"] for e in soldiers],
                    "position": point,
                    "queued": False,
                    "allow_capture": False,
                }
            )
        elif cavalry and cavalry[0]["idle"]:
            base = centre["position"]
            leg = (view["turn"] // 150) % 2
            offset = 120 if leg == 0 else -120
            actions.append(
                {
                    "action_id": f"scout-{view['turn']}",
                    "type": "move",
                    "units": [cavalry[0]["handle"]],
                    "position": {
                        "x": min(500, max(10, base["x"] + offset)),
                        "z": min(500, max(10, base["z"] + offset)),
                    },
                    "queued": False,
                }
            )
        idle = [e for e in _own(view, "support_civilian") if e["idle"] and e["position"]]
        actions += _gather_orders(view, idle)
        return actions[:20]


class RandomLegalController:
    """Legal-random baseline: schema-valid actions with real handles, drawn from a seeded RNG."""

    name = "random"

    def __init__(self, seed=0, per_decision=3):
        import random  # noqa: PLC0415

        self.random = random.Random(seed)  # noqa: S311 - reproducible sampling, not security
        self.per_decision = per_decision

    def decide(self, gateway):
        view = gateway.observation()
        actions = []
        own = [e for e in view["own_entities"] if e["position"]]
        units = [e for e in own if "Unit" in e["classes"]]
        producers = [e for e in own if e["trainable"] or e["researchable"]]
        builders = [e for e in units if e["buildable"]]
        resources = _known(view, resource="wood") + _known(view, resource="food")
        enemies = _known(view, owner=2, visible_only=True)
        bounds = view["map"]["bounds"]
        for index in range(self.per_decision):
            kind = self.random.choice(["move", "gather", "train", "build", "attack", "research"])
            action = None
            if kind == "move" and units:
                group = self.random.sample(units, min(len(units), self.random.randint(1, 4)))
                action = {
                    "type": "move",
                    "units": [e["handle"] for e in group],
                    "position": {
                        "x": self.random.uniform(bounds["min_x"] + 8, bounds["max_x"] - 8),
                        "z": self.random.uniform(bounds["min_z"] + 8, bounds["max_z"] - 8),
                    },
                    "queued": False,
                }
            elif kind == "gather" and resources and builders:
                action = {
                    "type": "gather",
                    "units": [self.random.choice(builders)["handle"]],
                    "target": self.random.choice(resources)["handle"],
                    "queued": False,
                }
            elif kind == "train" and producers:
                producer = self.random.choice(
                    [p for p in producers if p["trainable"]] or producers
                )
                if producer["trainable"]:
                    action = {
                        "type": "train",
                        "building": producer["handle"],
                        "template": self.random.choice(producer["trainable"]),
                        "count": self.random.randint(1, 3),
                    }
            elif kind == "build" and builders:
                builder = self.random.choice(builders)
                action = {
                    "type": "build",
                    "units": [builder["handle"]],
                    "template": self.random.choice(builder["buildable"]),
                    "position": {
                        "x": builder["position"]["x"] + self.random.uniform(-30, 30),
                        "z": builder["position"]["z"] + self.random.uniform(-30, 30),
                    },
                    "angle": 0,
                    "queued": False,
                    "autorepair": True,
                    "autocontinue": False,
                }
            elif kind == "attack" and enemies and units:
                action = {
                    "type": "attack",
                    "units": [e["handle"] for e in self.random.sample(units, min(len(units), 3))],
                    "target": self.random.choice(enemies)["handle"],
                    "queued": False,
                    "allow_capture": False,
                }
            elif kind == "research":
                researchers = [p for p in producers if p["researchable"]]
                if researchers:
                    producer = self.random.choice(researchers)
                    technology = self.random.choice(producer["researchable"])
                    if isinstance(technology, str):
                        action = {
                            "type": "research",
                            "building": producer["handle"],
                            "technology": technology,
                        }
            if action is not None:
                action["position"] = (
                    {
                        k: min(bounds["max_x"] - 1, max(0.0, v))
                        for k, v in action["position"].items()
                    }
                    if "position" in action
                    else None
                )
                if action["position"] is None:
                    del action["position"]
                actions.append({"action_id": f"rnd-{view['turn']}-{index}", **action})
        return actions


CONTROLLERS = {
    "noop": NoOpController,
    "raid_recovery": RaidRecoveryController,
    "recovery": RaidRecoveryController,
    "economy": EconomyController,
    "defense": DefenseController,
    "expansion": ExpansionController,
    "scout_strike": ScoutStrikeController,
    "random": RandomLegalController,
}


def make_controller(spec):
    """Build a controller from a CLI spec such as `noop`, `raid_recovery`, or `sleep:2.5`."""
    name, _, argument = spec.partition(":")
    if name == "sleep":
        return SleepingController(float(argument or 5))
    if name == "random":
        return RandomLegalController(int(argument or 0))
    if name not in CONTROLLERS:
        raise ValueError(f"Unknown controller {spec}")
    return CONTROLLERS[name]()
