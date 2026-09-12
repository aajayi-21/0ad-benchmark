"""Failure investigation from the player's knowledge at the time, then the privileged timeline.

The first section of each investigation uses only what the seat could see: its frozen
observations, the actions it submitted, and the results it was told. The privileged section
follows separately so hindsight cannot leak into the player-perspective account.
"""

import json
from collections import Counter
from pathlib import Path

from zero_ad_bench.telemetry import read_jsonl


def sample_failures(experiment_dir, limit=3):
    """Pick failed or invalid trials, one per scenario first, then by trial order."""
    rows, _ = read_jsonl(Path(experiment_dir) / "experiment.jsonl")
    failed = [r for r in rows if r["result"] not in ("success", "win") and r.get("episode")]
    chosen = []
    seen = set()
    for row in failed:
        if row["scenario"] not in seen:
            chosen.append(row)
            seen.add(row["scenario"])
    for row in failed:
        if row not in chosen:
            chosen.append(row)
    return chosen[:limit]


def player_view(observation):
    """Summarize one boundary observation using player knowledge only."""
    own = observation["own_entities"]
    seat = observation["seat"]
    visible = observation["visible_entities"]
    threats = [
        e
        for e in visible
        if e.get("owner") not in (seat, 0) and "Unit" in e.get("classes", []) and e.get("position")
    ]
    remembered = [e for e in observation["last_seen"] if e.get("owner") not in (seat, 0)]
    return {
        "turn": observation["turn"],
        "resources": observation["self"]["resources"],
        "population": observation["self"]["population"]["used"],
        "own": dict(Counter(e["template"].split("/")[-1] for e in own)),
        "idle_workers": sum(1 for e in own if e["idle"] and e["buildable"]),
        "visible_threats": [
            (e["template"].split("/")[-1], round(e["position"]["x"]), round(e["position"]["z"]))
            for e in threats
        ][:8],
        "remembered_enemies": len(remembered),
        "events": dict(Counter(e["type"] for e in observation["events"])),
    }


def _loss_turn(result, events, seat):
    lost = (result.get("objective") or {}).get("lost_turn")
    if lost is not None:
        return lost
    return next(
        (
            e["turn"]
            for e in events
            if e.get("type") == "destroyed"
            and e.get("entity", {}).get("owner") == seat
            and "civil_centre" in e["entity"].get("template", "")
        ),
        None,
    )


def investigate_episode(episode_dir, window=6):
    """Render one episode: the player's view around the loss, then the evaluator's view."""
    episode_dir = Path(episode_dir)
    result = json.loads((episode_dir / "result.json").read_text())
    resolved = json.loads((episode_dir / "resolved-scenario.json").read_text())
    observations, _ = read_jsonl(episode_dir / "observations.jsonl")
    decisions, _ = read_jsonl(episode_dir / "decisions.jsonl")
    actions, _ = read_jsonl(episode_dir / "actions.jsonl")
    events, _ = read_jsonl(episode_dir / "events.jsonl")
    objective = resolved["scenario"]["objective"]
    achieved = (result.get("objective") or {}).get("achieved_turn")
    seat = observations[0]["seat"]
    # Anchor the window on the objective's loss point when one exists, else the last decisions.
    loss_turn = _loss_turn(result, events, seat)
    anchor = loss_turn if loss_turn is not None else observations[-1]["turn"]
    boundaries = [o for o in observations if o["turn"] <= anchor][-window:]
    results_by_decision = {}
    for action in actions:
        if action["kind"] == "result":
            label = f"{action['action_id']}:{action['stage']}"
            if action.get("reason"):
                label += f"({action['reason']})"
            results_by_decision.setdefault(action["decision_id"], []).append(label)
    lines = [
        (
            f"### Episode {result['episode_id']} ({result['scenario_id']}, "
            f"{result['status']}/{result['result']})"
        ),
        "",
        f"Objective: {objective.get('public') or objective.get('success')}",
        "",
        "#### What the player knew, decision by decision",
        "",
    ]
    for observation in boundaries:
        view = player_view(observation["observation"])
        decision = next(
            (d for d in decisions if d["decision_id"] == observation["decision_id"]), None
        )
        submitted = results_by_decision.get(observation["decision_id"], [])
        lines.append(
            f"- Turn {view['turn']}: resources {json.dumps(view['resources'])}, pop "
            f"{view['population']}, own {json.dumps(view['own'], sort_keys=True)}, idle workers "
            f"{view['idle_workers']}, visible threats {view['visible_threats'] or 'none'}, "
            f"remembered enemies {view['remembered_enemies']}, interval events "
            f"{json.dumps(view['events'], sort_keys=True)}"
        )
        if decision is not None:
            plan = (decision.get("metadata") or {}).get("plan")
            detail = f"  - decided: {decision['outcome']}, {decision['action_count']} action(s)"
            if plan:
                detail += f", plan: {plan}"
            if submitted:
                detail += f"; results {submitted}"
            lines.append(detail)
    lines += ["", "#### Privileged timeline (evaluator view)", ""]
    losses = [e for e in events if e.get("type") == "destroyed" and e.get("cause") == "killed"]
    owner_losses = Counter(
        (e["entity"].get("owner"), e["entity"].get("template", "").split("/")[-1]) for e in losses
    )
    attacks = [e for e in events if e.get("type") == "attacked"]
    unseen = [e for e in attacks if seat not in (e.get("entity") or {}).get("visible_to", [])]
    lines += [
        (
            f"- Result {result['result']}; terminal {result['terminal_reason']}; final turn "
            f"{result['final_turn']}; objective achieved turn {achieved}; loss anchor turn "
            f"{loss_turn}."
        ),
        "- Killed entities by owner and template: "
        + json.dumps({f"{o}:{t}": n for (o, t), n in owner_losses.items()}, sort_keys=True),
        f"- Attacks the player could not see when they happened: {len(unseen)} of {len(attacks)}.",
        "- Action reliability: "
        + json.dumps(result["action_reliability"]["stages"], sort_keys=True)
        + " non-applied "
        + json.dumps(result["action_reliability"]["reasons"], sort_keys=True),
    ]
    return "\n".join(lines)


def investigate(experiment_dir, limit=3, window=6):
    """Write `failure-investigation.md` for a sample of an experiment's failed trials."""
    experiment_dir = Path(experiment_dir)
    sample = sample_failures(experiment_dir, limit)
    lines = [
        f"# Failure investigation: {experiment_dir.name}",
        "",
        (
            f"{len(sample)} sampled failed or invalid trial(s); player-knowledge sections come "
            "from the recorded observations and action results only, privileged sections from "
            "the ledger."
        ),
        "",
    ]
    for row in sample:
        lines += [
            f"## {row['trial_id']}",
            "",
            investigate_episode(experiment_dir / row["episode"], window),
            "",
        ]
    if not sample:
        lines.append("No failed trials to investigate.")
    text = "\n".join(lines)
    (experiment_dir / "failure-investigation.md").write_text(text)
    return text
