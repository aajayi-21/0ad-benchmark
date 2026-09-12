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
    """Own at least `min_count` matching entities at a boundary within `(after_turn, by_turn]`."""
    minimum = int(params["min_count"])
    after = int(params.get("after_turn", -1))
    by_turn = int(params["by_turn"])
    for snapshot in snapshots:
        player = _player(snapshot, params["player"])
        count = _count(player, params.get("template_suffix"), params.get("class"))
        if after < snapshot["turn"] <= by_turn and count >= minimum:
            return {"success": True, "achieved_turn": snapshot["turn"], "count": count}
    return {"success": False, "achieved_turn": None}


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


EVALUATORS = {
    "reached_phase_v1": reached_phase_v1,
    "entity_count_v1": entity_count_v1,
    "preserve_entity_v1": preserve_entity_v1,
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
    if any(administrative.values()):
        return "failure"
    if evaluation["success"] is True:
        return "success"
    if evaluation["success"] is False:
        return "failure"
    return "incomplete" if status == "interrupted" else "failure"
