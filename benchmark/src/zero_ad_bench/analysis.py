"""Aggregate statistics for preregistered trial sets: rates, uncertainty, censoring, accounting.

Every function reads only experiment rows (one per attempted episode). Invalid, failed,
interrupted, and incomplete episodes are excluded from denominators and reported separately.
"""

import random


BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20260912
VALID_STATUSES = ("completed",)


def valid_rows(rows):
    return [
        r
        for r in rows
        if r["status"] in VALID_STATUSES
        and r["result"] in ("success", "failure", "win", "draw", "loss")
    ]


def bootstrap_ci(rows, statistic, resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED):
    """Percentile 95% interval of `statistic(rows)`, resampling seed clusters with replacement."""
    clusters = {}
    for row in rows:
        clusters.setdefault(row["seed"], []).append(row)
    keys = sorted(clusters)
    if not keys:
        return None
    rng = random.Random(seed)  # noqa: S311 - reproducible resampling, not security
    values = []
    for _ in range(resamples):
        sample = []
        for _ in keys:
            sample += clusters[rng.choice(keys)]
        values.append(statistic(sample))
    values.sort()
    return {
        "low": values[int(0.025 * (len(values) - 1))],
        "high": values[int(0.975 * (len(values) - 1))],
        "resamples": resamples,
        "clusters": len(keys),
        "method": "percentile bootstrap clustered by seed",
    }


def success_rate(rows):
    """Goal-task success rate over valid episodes with its clustered bootstrap interval."""
    valid = valid_rows(rows)
    successes = sum(1 for r in valid if r["result"] == "success")

    def rate(sample):
        return sum(1 for r in sample if r["result"] == "success") / len(sample) if sample else 0.0

    return {
        "successes": successes,
        "valid": len(valid),
        "attempted": len(rows),
        "rate": rate(valid) if valid else None,
        "ci95": bootstrap_ci(valid, rate) if valid else None,
    }


def time_to_success(rows, horizon):
    """Achieved turns of successes; failures are censored at the horizon, counted, not averaged."""
    valid = valid_rows(rows)
    achieved = sorted(
        r["achieved_turn"]
        for r in valid
        if r["result"] == "success" and r.get("achieved_turn") is not None
    )
    censored = len(valid) - len(achieved)
    summary = {
        "successes": len(achieved),
        "censored_at_horizon": censored,
        "horizon": horizon,
        "achieved_turns": achieved,
    }
    # With all censoring at the horizon, the survival median is the first event at which
    # at least half the entire cohort has succeeded. Do not condition on success alone.
    median_index = (len(valid) + 1) // 2 - 1
    if valid and len(achieved) > median_index:
        summary["median_turn"] = achieved[median_index]
    else:
        summary["median_turn"] = None
    if achieved:
        summary["fastest_turn"] = achieved[0]
    return summary


def match_score(rows):
    """Full-game accounting: wins, draws, losses, administrative outcomes, and the match score."""
    valid = valid_rows(rows)
    counts = {"win": 0, "draw": 0, "loss": 0}
    for r in valid:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    administrative = sum(1 for r in rows if r.get("administrative"))
    denominator = counts["win"] + counts["draw"] + counts["loss"]
    return {
        **counts,
        "administrative": administrative,
        "valid_matches": denominator,
        "match_score": (counts["win"] + 0.5 * counts["draw"]) / denominator
        if denominator
        else None,
        "definition": "(wins + 0.5 * draws) / valid matches",
    }


def accounting(rows):
    """Every attempted episode by status and by exclusion reason."""
    by_status = {}
    reasons = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        if r["status"] not in VALID_STATUSES or r["result"] not in (
            "success",
            "failure",
            "win",
            "draw",
            "loss",
        ):
            reason = (r.get("failure") or {}).get("kind") or (
                r["status"] if r["status"] not in VALID_STATUSES else r.get("result")
            )
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "attempted": len(rows),
        "by_status": by_status,
        "valid": len(valid_rows(rows)),
        "excluded": len(rows) - len(valid_rows(rows)),
        "exclusion_reasons": reasons,
    }


def cost_summary(rows):
    known = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
    return {
        "episodes_with_cost": len(known),
        "episodes_without_cost": len(rows) - len(known),
        "total_usd": round(sum(known), 6) if known else None,
        "mean_usd": round(sum(known) / len(known), 6) if known else None,
    }


def summarize(rows, scenarios):
    """Per scenario and controller: rates or match scores, timing, cost, accounting."""
    summary = {}
    for scenario_id, scenario in scenarios.items():
        family = scenario.get("family", "")
        horizon = scenario["schedule"]["turn_limit"]
        for controller in sorted({r["controller"] for r in rows if r["scenario"] == scenario_id}):
            subset = [
                r for r in rows if r["scenario"] == scenario_id and r["controller"] == controller
            ]
            entry = {"accounting": accounting(subset), "cost": cost_summary(subset)}
            if family == "full_game":
                entry["match"] = match_score(subset)
            else:
                entry["success"] = success_rate(subset)
                entry["time_to_success"] = time_to_success(subset, horizon)
            summary.setdefault(scenario_id, {})[controller] = entry
    return summary
