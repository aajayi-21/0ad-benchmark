"""Versioned goal evaluators computed from privileged decision-boundary snapshots.

Evaluators read only artifacts, so a score can be reproduced offline without an engine or a
model call. Absent milestones are censored at the last observed turn, never treated as zero.
"""

from zero_ad_bench.scenario import PHASE_ORDER


EVALUATOR_VERSION = "1"


def _player(snapshot, seat):
    return snapshot["players"].get(str(seat)) or snapshot["players"].get(seat)


def _count(player, template_suffix=None, entity_class=None):
    if template_suffix:
        return sum(n for name, n in player["templates"].items() if name.endswith(template_suffix))
    return player["classes"].get(entity_class or "Unit", 0)


def reached_phase_v1(params, snapshots, _outcome):
    target = PHASE_ORDER.index(params["phase"])
    by_turn = int(params["by_turn"])
    for snapshot in snapshots:
        player = _player(snapshot, params["player"])
        if snapshot["turn"] <= by_turn and PHASE_ORDER.index(player["phase"]) >= target:
            return {"success": True, "achieved_turn": snapshot["turn"]}
    return {"success": False, "achieved_turn": None}


def entity_count_v1(params, snapshots, _outcome):
    """Match a count bound at a boundary within `(after_turn, by_turn]`.

    `min_count` succeeds when the player owns at least that many matching entities;
    `max_count` succeeds when the player owns at most that many (for example zero enemy
    structures of a kind, measured from the privileged snapshot of that player).
    """
    minimum = params.get("min_count")
    maximum = params.get("max_count")
    after = int(params.get("after_turn", -1))
    by_turn = int(params["by_turn"])
    for snapshot in snapshots:
        player = _player(snapshot, params["player"])
        count = _count(player, params.get("template_suffix"), params.get("class"))
        satisfied = (minimum is None or count >= int(minimum)) and (
            maximum is None or count <= int(maximum)
        )
        if after < snapshot["turn"] <= by_turn and satisfied:
            return {"success": True, "achieved_turn": snapshot["turn"], "count": count}
    return {"success": False, "achieved_turn": None}


def structure_in_region_v1(params, snapshots, _outcome):
    """Complete an eligible structure inside the bounds and keep one there for `hold_turns`.

    A completed (non-foundation) structure whose template ends with one of
    `template_suffixes` must lie inside the half-open bounds at every boundary of a window of
    `hold_turns` turns that ends by `by_turn`. Foundations do not count; a structure lost and
    rebuilt restarts the window.
    """
    bounds = params["bounds"]
    suffixes = tuple(params["template_suffixes"])
    hold = int(params["hold_turns"])
    by_turn = int(params["by_turn"])
    start = None
    for snapshot in snapshots:
        if snapshot["turn"] > by_turn:
            break
        player = _player(snapshot, params["player"])
        present = any(
            not s["foundation"]
            and s["template"].endswith(suffixes)
            and bounds["min_x"] <= s["x"] < bounds["max_x"]
            and bounds["min_z"] <= s["z"] < bounds["max_z"]
            for s in player.get("structures", [])
        )
        if not present:
            start = None
            continue
        if start is None:
            start = snapshot["turn"]
        if snapshot["turn"] - start >= hold:
            return {"success": True, "achieved_turn": snapshot["turn"], "held_since_turn": start}
    return {"success": False, "achieved_turn": None, "held_since_turn": start}


def preserve_entity_v1(params, snapshots, _outcome):
    """Keep at least one matching entity at every boundary through `until_turn`."""
    until = int(params["until_turn"])
    reached = None
    for snapshot in snapshots:
        player = _player(snapshot, params["player"])
        if snapshot["turn"] > until:
            break
        if _count(player, params.get("template_suffix"), params.get("class")) < 1:
            return {"success": False, "achieved_turn": None, "lost_turn": snapshot["turn"]}
        reached = snapshot["turn"]
    if reached is not None and reached >= until:
        return {"success": True, "achieved_turn": until}
    return {"success": False, "achieved_turn": None}


def conquest_v1(params, _snapshots, outcome):
    state = (outcome.get("player_states") or {}).get(str(params["player"]))
    if state == "won":
        return {"success": True, "achieved_turn": outcome.get("turn")}
    if state == "defeated" or outcome.get("terminal_reason"):
        return {"success": False, "achieved_turn": None}
    return {"success": None, "achieved_turn": None}


def match_v1(params, _snapshots, outcome):
    """Per-side outcome of a multi-seat match under the frozen competition rules.

    In-game: a seat marked `won` wins, `defeated` loses, and a turn limit is a draw. The runner's
    administrative outcomes (forfeit after consecutive decision failures, budget stop, agent
    stop) override the engine: the surviving seat wins administratively, and forfeits at the
    same decision are an administrative draw even though the engine, applying the resignations
    in seat order, may have marked the later seat as the winner.
    """
    seats = [str(seat) for seat in params.get("seats", [1, 2])]
    states = outcome.get("player_states") or {}
    administrative = {
        seat: entry
        for seat, entry in (outcome.get("administrative") or {}).items()
        if entry and seat in seats
    }
    if not outcome.get("terminal_reason"):
        return {"success": None, "achieved_turn": None, "sides": None}
    simultaneous = len(administrative) == len(seats) and (
        len({entry.get("turn") for entry in administrative.values()}) == 1
    )
    sides = {}
    for seat in seats:
        if simultaneous:
            sides[seat] = "draw"
        elif seat in administrative:
            sides[seat] = "loss"
        elif administrative or states.get(seat) == "won":
            sides[seat] = "win"
        elif states.get(seat) == "defeated":
            sides[seat] = "loss"
        else:
            sides[seat] = "draw"
    return {
        "success": None,
        "achieved_turn": None,
        "sides": sides,
        "administrative_outcome": {s: e["kind"] for s, e in administrative.items()} or None,
        "player": str(params["player"]),
    }


EVALUATORS = {
    "reached_phase_v1": reached_phase_v1,
    "entity_count_v1": entity_count_v1,
    "preserve_entity_v1": preserve_entity_v1,
    "structure_in_region_v1": structure_in_region_v1,
    "match_v1": match_v1,
    "conquest_v1": conquest_v1,
}


def evaluate(objective, snapshots, outcome):
    """Return the objective result. `success` is None only while the run is incomplete."""
    evaluator = EVALUATORS[objective["evaluator"]]
    params = {"player": objective["player"], **objective.get("params", {})}
    if not snapshots:
        return {
            "evaluator": objective["evaluator"],
            "version": EVALUATOR_VERSION,
            "timing": objective.get("timing", "decision_boundary_v1"),
            "success": None,
            "achieved_turn": None,
            "censored_at_turn": None,
        }
    result = evaluator(params, snapshots, outcome)
    last_turn = snapshots[-1]["turn"]
    complete = bool(outcome.get("terminal_reason")) or outcome.get("stopped_on_success")
    if not result["success"] and not complete:
        result["success"] = None
    return {
        "evaluator": objective["evaluator"],
        "version": EVALUATOR_VERSION,
        "timing": objective.get("timing", "decision_boundary_v1"),
        "params": params,
        "censored_at_turn": None if result.get("success") else last_turn,
        **result,
    }


def score(evaluation, _outcome, status, administrative, invalid_reasons):
    """Map an evaluation to the reported result label. Infrastructure failure is never a loss."""
    if status in ("failed", "running"):
        return "incomplete" if status == "running" else "invalid"
    if invalid_reasons:
        return "invalid"
    if "sides" in evaluation:
        # A match reports the objective player's side; administrative rules are inside.
        sides = evaluation["sides"]
        return sides[evaluation["player"]] if sides else "incomplete"
    if any(administrative.values()) or evaluation["success"] is False:
        return "failure"
    if evaluation["success"] is True:
        return "success"
    return "incomplete" if status == "interrupted" else "failure"
