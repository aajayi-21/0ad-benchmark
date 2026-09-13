"""Versioned scenario manifests and their resolution to engine attributes."""

import copy
import hashlib
import json
from pathlib import Path


PHASE_ORDER = ("village", "town", "city")
INFORMATION_TRACKS = {"partial_v1": "partial", "full_diagnostic_v1": "full"}
ROOT = Path(__file__).resolve().parents[3]
VICTORY_CONDITIONS = ROOT / "binaries/data/mods/public/simulation/data/settings/victory_conditions"
CONTROLLER_KINDS = ("external", "passive", "petra")


class ScenarioError(ValueError):
    """The manifest is malformed or unsupported."""


def _require(condition, message):
    if not condition:
        raise ScenarioError(message)


class Scenario:
    """A JSON scenario manifest. The format follows the specification's proposed YAML shape."""

    def __init__(self, data, path=None):
        self.path = Path(path) if path else None
        _require(isinstance(data, dict), "Manifest must be an object")
        _require(data.get("schema_version") == 1, "Unsupported manifest schema_version")
        self.id = data["id"]
        _require(isinstance(self.id, str) and self.id, "Manifest id is required")
        self.description = data.get("description", "")
        self.map_type = data["map"]["type"]
        self.map = data["map"]["path"]
        _require(self.map_type in ("scenario", "skirmish", "random"), "Unknown map type")
        self.settings = dict(data.get("settings", {}))
        self.seeds = {"map": int(data["seed_bundle"]["map"]), "ai": int(data["seed_bundle"]["ai"])}
        self.controllers = {}
        for seat, controller in data["controllers"].items():
            number = int(seat)
            _require(1 <= number <= 8, "Controller seats are numbered 1 to 8")
            _require(controller["kind"] in CONTROLLER_KINDS, "Unknown controller kind")
            self.controllers[number] = dict(controller)
        _require(
            sorted(self.controllers) == list(range(1, len(self.controllers) + 1)),
            "Controller seats must be contiguous from 1",
        )
        self.information = data.get("information", "partial_v1")
        _require(self.information in INFORMATION_TRACKS, "Unknown information track")
        schedule = data["schedule"]
        self.decision_turns = int(schedule["decision_turns"])
        self.turn_limit = int(schedule["turn_limit"])
        _require(1 <= self.decision_turns <= 300, "decision_turns must be 1 to 300")
        _require(1 <= self.turn_limit <= 12000, "turn_limit must be 1 to 12000")
        self.limits = {
            "actions_per_decision": 20,
            "group_size": 64,
            "train_batch": 5,
            "reads_per_decision": 8,
            **data.get("limits", {}),
        }
        self.objective = dict(data["objective"])
        _require("evaluator" in self.objective and "player" in self.objective, "Objective fields")
        self.objective.setdefault("params", {})
        self.objective.setdefault("stop_on_success", True)
        self.objective.setdefault("success", "")
        self.victory_conditions = list(data.get("victory_conditions", []))
        self.trigger_scripts = list(data.get("trigger_scripts", []))
        self.mods = list(data.get("mods", ["agent_benchmark"]))
        self.assets = list(data.get("assets", []))
        self.baseline = data.get("baseline", "noop")
        self.family = data.get("family", "")

    def victory_scripts(self):
        """Trigger scripts the game's own setup adds for the named victory conditions."""
        scripts = []
        for name in self.victory_conditions:
            path = VICTORY_CONDITIONS / f"{name}.json"
            _require(path.is_file(), f"Unknown victory condition {name!r}")
            for script in json.loads(path.read_text())["Data"]["Scripts"]:
                if script not in scripts:
                    scripts.append(script)
        return scripts

    def resolved_trigger_scripts(self):
        """Return custom scripts, then victory scripts, deduplicated as the game setup does."""
        scripts = list(self.trigger_scripts)
        scripts += [script for script in self.victory_scripts() if script not in scripts]
        return scripts

    def with_seeds(self, map_seed, ai_seed):
        """Return the same scenario with the seed bundle replaced (one trial of a suite)."""
        copy_ = copy.copy(self)
        copy_.seeds = {"map": int(map_seed), "ai": int(ai_seed)}
        return copy_

    @classmethod
    def load(cls, path):
        path = Path(path)
        return cls(json.loads(path.read_text()), path)

    def external_seats(self):
        return [seat for seat, c in sorted(self.controllers.items()) if c["kind"] == "external"]

    def public_objective(self):
        """Return the model-facing objective text; it never includes seeds or trigger details."""
        return self.objective.get("public", self.description) or self.description

    def resolve(self):
        """Build the resolved engine attributes. Seat N is PlayerData[N-1]; Gaia is implicit."""
        players = []
        for seat in sorted(self.controllers):
            controller = self.controllers[seat]
            player = {
                "Name": f"Player {seat}",
                "Civ": controller["civilization"],
                "AI": "petra" if controller["kind"] == "petra" else "",
                "AIDiff": int(controller.get("difficulty", 3)),
                "AIBehavior": controller.get("behavior", "balanced"),
                "Team": -1,
            }
            players.append(player)
        settings = {
            "mapType": self.map_type,
            # Display settings the graphical client reads during a visual replay; the engine
            # applies the per-player population cap whether or not its type is named.
            "mapName": Path(self.map).name.replace("_", " ").title(),
            "PopulationCapType": "player",
            "Seed": self.seeds["map"],
            "AISeed": self.seeds["ai"],
            "CheatsEnabled": False,
            "RevealMap": False,
            "ExploreMap": False,
            "LockTeams": True,
            "AllyView": False,
            "Ceasefire": 0,
            "VictoryConditions": list(self.victory_conditions),
            "TriggerScripts": self.resolved_trigger_scripts(),
            **copy.deepcopy(self.settings),
            "PlayerData": players,
        }
        return {
            "mapType": self.map_type,
            "map": self.map,
            "gameSpeed": 1,
            "settings": settings,
        }

    def content_hashes(self, mod_sources):
        """Hash declared assets from the given mod roots; unresolved assets are explicit."""
        roots = [Path(p) for p in mod_sources.values()]
        hashes = {}
        for asset in self.assets:
            found = next((root / asset for root in roots if (root / asset).is_file()), None)
            hashes[asset] = hashlib.sha256(found.read_bytes()).hexdigest() if found else None
        return hashes

    def describe(self):
        return {
            "id": self.id,
            "description": self.description,
            "path": str(self.path) if self.path else None,
            "map": {"type": self.map_type, "path": self.map},
            "seed_bundle": dict(self.seeds),
            "controllers": {str(s): dict(c) for s, c in sorted(self.controllers.items())},
            "information": self.information,
            "schedule": {"decision_turns": self.decision_turns, "turn_limit": self.turn_limit},
            "limits": dict(self.limits),
            "objective": dict(self.objective),
            "victory_conditions": list(self.victory_conditions),
            "trigger_scripts": list(self.trigger_scripts),
            "victory_scripts": self.victory_scripts(),
            "mods": list(self.mods),
            "assets": list(self.assets),
            "baseline": self.baseline,
            "family": self.family,
        }
