"""Offline result reproduction, static Markdown report, and artifact verification."""

import json
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from zero_ad_bench import ARTIFACT_SCHEMA_VERSION, PACKAGE_VERSION, evaluation
from zero_ad_bench.telemetry import STREAMS, read_jsonl, sha256_file


def load_episode(directory):
    """Read every artifact stream without an engine. Truncated streams are reported, not fixed."""
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    resolved_path = directory / "resolved-scenario.json"
    streams = {}
    truncated = {}
    for name in STREAMS:
        streams[name], truncated[name] = read_jsonl(directory / f"{name}.jsonl")
    return {
        "directory": directory,
        "manifest": json.loads(manifest_path.read_text()) if manifest_path.is_file() else None,
        "resolved": json.loads(resolved_path.read_text()) if resolved_path.is_file() else None,
        "streams": streams,
        "truncated": truncated,
    }


def _outcome(snapshots, manifest):
    if not snapshots:
        return {
            "terminal_reason": None,
            "terminated": False,
            "truncated": False,
            "turn": None,
            "sim_time_ms": None,
            "player_states": {},
            "stopped_on_success": False,
        }
    last = snapshots[-1]
    return {
        "terminal_reason": last["stop_reason"] or None,
        "terminated": last["terminated"],
        "truncated": last["truncated"],
        "turn": last["turn"],
        "sim_time_ms": last["sim_time_ms"],
        "player_states": last["player_states"],
        "stopped_on_success": bool(manifest.get("stopped_on_success")),
    }


def build_result(directory, override=None):
    """Recompute `result.json` purely from artifacts (plus a live status override)."""
    episode = load_episode(directory)
    manifest = {**(episode["manifest"] or {}), **(override or {})}
    status = manifest.get("status", "running")
    streams = episode["streams"]
    snapshots = streams["snapshots"]
    outcome = _outcome(snapshots, manifest)
    seats = [str(seat) for seat in manifest.get("seats", [])]
    administrative = dict.fromkeys(seats)
    for decision in streams["decisions"]:
        if decision["outcome"] in ("forfeit", "agent_stop"):
            kind = (
                decision["outcome"]
                if decision["outcome"] == "forfeit"
                else (decision.get("metadata") or {}).get("stop_kind")
                or decision.get("administrative")
            )
            administrative[str(decision["seat"])] = {
                "kind": kind,
                "turn": decision["turn"],
                "reason": decision.get("error"),
            }
    invalid = [
        {"reason": "ledger_overflow", "decision_id": e["decision_id"], "dropped": e["dropped"]}
        for e in streams["events"]
        if e.get("type") == "ledger_overflow"
    ]
    invalid += [
        {
            "reason": "provider_failure",
            "decision_id": d["decision_id"],
            "seat": d["seat"],
            "detail": d.get("error"),
        }
        for d in streams["decisions"]
        if d["outcome"] == "provider_failure"
    ]
    if status in ("completed", "invalid"):
        invalid += [
            {"reason": "truncated_stream", "stream": name}
            for name, flag in episode["truncated"].items()
            if flag
        ]
    resolved = episode["resolved"]
    verdict = None
    if resolved:
        verdict = evaluation.evaluate(resolved["scenario"]["objective"], snapshots, outcome)
    if status == "completed" and invalid:
        status = "invalid"
    decisions = Counter(d["outcome"] for d in streams["decisions"])
    stages = Counter()
    reasons = Counter()
    for action in streams["actions"]:
        if action["kind"] != "result":
            continue
        stage = action["stage"] + ("_partial" if action.get("partial") else "")
        stages[stage] += 1
        if action.get("reason") and action["stage"] != "applied":
            reasons[f"{action['stage']}:{action['reason']}"] += 1
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "runner_version": PACKAGE_VERSION,
        "evaluator_version": evaluation.EVALUATOR_VERSION,
        "episode_id": manifest.get("episode_id"),
        "scenario_id": manifest.get("scenario_id"),
        "experiment_id": manifest.get("experiment_id"),
        "status": status,
        "result": evaluation.score(verdict, outcome, status, administrative, invalid)
        if verdict
        else ("invalid" if status == "failed" else "incomplete"),
        "failure": manifest.get("failure"),
        "terminal_reason": outcome["terminal_reason"],
        "terminated": outcome["terminated"],
        "truncated": outcome["truncated"],
        "final_turn": outcome["turn"],
        "final_sim_time_ms": outcome["sim_time_ms"],
        "player_states": outcome["player_states"],
        "administrative": administrative,
        "objective": verdict,
        "invalid_reasons": invalid,
        "decision_outcomes": dict(decisions),
        "action_reliability": {
            "denominator": "submitted action results for all bound seats",
            "stages": dict(stages),
            "reasons": dict(reasons),
        },
        "stream_truncation": episode["truncated"],
        "record_counts": {name: len(records) for name, records in streams.items()},
        "model_usage": model_usage(streams["model-calls"], streams["decisions"]),
    }


def model_usage(calls, decisions):
    """Aggregate provider accounting; counts a provider did not report stay unavailable."""
    model_calls = [c for c in calls if c.get("kind") == "model"]
    if not model_calls:
        return None
    usage = {
        "attempts": len(model_calls),
        "responses": 0,
        "errors": 0,
        "retries": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": 0.0,
        "cost_unavailable": False,
        "latency_s_total": 0.0,
        "usage_unavailable_responses": 0,
        "models": {},
        "providers": {},
        "stop_reasons": {},
        "error_kinds": {},
    }
    seen = set()
    for call in model_calls:
        key = (call.get("seat"), call.get("decision_id"), call.get("request_index"))
        if key in seen:
            usage["retries"] += 1
        seen.add(key)
        usage["latency_s_total"] += call.get("latency_s") or 0
        if call.get("error"):
            usage["errors"] += 1
            kind = call["error"].get("kind", "unknown")
            usage["error_kinds"][kind] = usage["error_kinds"].get(kind, 0) + 1
            continue
        usage["responses"] += 1
        response = call.get("response") or {}
        stop = response.get("stop_reason", "unknown")
        usage["stop_reasons"][stop] = usage["stop_reasons"].get(stop, 0) + 1
        model = response.get("model") or call.get("model")
        usage["models"][model] = usage["models"].get(model, 0) + 1
        usage["providers"][call.get("provider")] = (
            usage["providers"].get(call.get("provider"), 0) + 1
        )
        counts = call.get("usage") or {}
        if counts.get("input_tokens") is None or counts.get("output_tokens") is None:
            usage["usage_unavailable_responses"] += 1
        for field in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
            if counts.get(field) is not None:
                usage[field] += counts[field]
        if call.get("cost_usd") is None:
            usage["cost_unavailable"] = True
        else:
            usage["cost_usd"] = round(usage["cost_usd"] + call["cost_usd"], 8)
    usage["latency_s_total"] = round(usage["latency_s_total"], 6)
    reasons = {}
    for decision in decisions:
        reason = (decision.get("metadata") or {}).get("reason") or decision["outcome"]
        reasons[reason] = reasons.get(reason, 0) + 1
    usage["decision_reasons"] = reasons
    return usage


def _table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def _ratio(numerator, denominator):
    if not denominator:
        return "n/a (0 denominator)"
    return f"{numerator / denominator:.3f} ({numerator}/{denominator})"


def build_report(directory, result=None):
    """Render the static episode report from artifacts only; facts are trace-backed."""
    episode = load_episode(directory)
    result = result or build_result(directory)
    manifest = episode["manifest"] or {}
    resolved = episode["resolved"] or {}
    scenario = resolved.get("scenario", {})
    streams = episode["streams"]
    snapshots = streams["snapshots"]
    seats = [str(seat) for seat in manifest.get("seats", [])]
    lines = [
        f"# Episode report: {result['scenario_id']} / {result['episode_id']}",
        "",
        "Generated from artifacts only; no engine or model call was needed.",
        "",
        f"- Status: **{result['status']}**; result: **{result['result']}**",
        (
            f"- Terminal reason: {result['terminal_reason'] or 'none recorded'}; "
            f"final turn {result['final_turn']}; simulated {result['final_sim_time_ms']} ms"
        ),
        f"- Player states: {json.dumps(result['player_states'], sort_keys=True)}",
        (
            f"- Runner {result['runner_version']}, protocol {manifest.get('protocol_version')}, "
            f"evaluator {result['evaluator_version']}, engine "
            f"{(manifest.get('engine') or {}).get('build_version', 'unknown')}"
        ),
        (
            f"- Controllers: {json.dumps(manifest.get('controllers', {}), sort_keys=True)}; "
            f"information {scenario.get('information')}; decision interval "
            f"{(scenario.get('schedule') or {}).get('decision_turns')} turns"
        ),
        f"- Replay: `{resolved.get('replay_directory', 'unavailable')}` (copied under `replay/`)",
    ]
    if result.get("failure"):
        lines.append(
            f"- Infrastructure failure: `{json.dumps(result['failure'], sort_keys=True)}`"
        )
    if result["invalid_reasons"]:
        lines.append(f"- Invalid reasons: `{json.dumps(result['invalid_reasons'])}`")
    objective = result.get("objective") or {}
    lines += [
        "",
        "## Objective",
        "",
        f"- Public description: {scenario.get('description', '')}",
        (
            f"- Evaluator: `{objective.get('evaluator')}` v{objective.get('version')} with "
            f"`{json.dumps(objective.get('params', {}), sort_keys=True)}`"
        ),
        (
            f"- Success: {objective.get('success')}; achieved turn: "
            f"{objective.get('achieved_turn')}; censored at turn: "
            f"{objective.get('censored_at_turn')}"
        ),
        f"- Administrative outcomes: {json.dumps(result['administrative'], sort_keys=True)}",
    ]
    by_decision = defaultdict(
        lambda: {"submitted": 0, "rejected": 0, "applied": 0, "failed": 0, "partial": 0}
    )
    for action in streams["actions"]:
        if action["kind"] == "result":
            bucket = by_decision[action["decision_id"]]
            bucket["submitted"] += 1
            bucket[action["stage"]] = bucket.get(action["stage"], 0) + 1
            if action.get("partial"):
                bucket["partial"] += 1
    decisions = streams["decisions"]
    rows = []
    for decision in decisions:
        bucket = by_decision[decision["decision_id"]]
        rows.append(
            [
                decision["decision_id"],
                decision["turn"],
                decision["seat"],
                decision["outcome"],
                decision["action_count"],
                bucket["applied"],
                bucket["rejected"],
                bucket["failed"],
                bucket["partial"],
                f"{decision['reads_used']}/{decision['read_budget']}",
                f"{decision['elapsed_s']:.2f}",
            ]
        )
    lines += [
        "",
        "## Decisions",
        "",
        _table(
            [
                "decision",
                "turn",
                "seat",
                "outcome",
                "actions",
                "applied",
                "rejected",
                "failed",
                "partial",
                "reads",
                "wall s",
            ],
            rows,
        )
        if rows
        else "No decisions were recorded.",
    ]
    lines += ["", "## Economy and allocation", ""]
    for seat in seats:
        rows = []
        for snapshot in snapshots:
            player = snapshot["players"].get(seat)
            if not player:
                continue
            metrics = (snapshot.get("interval_metrics") or {}).get(seat) or {}
            gathered = player["statistics"]["resourcesGathered"] if player["statistics"] else {}
            rows.append(
                [
                    snapshot["turn"],
                    player["phase"],
                    player["state"],
                    " ".join(f"{k}={v}" for k, v in sorted(player["resources"].items())),
                    f"{player['population']['used']}/{player['population']['limit']}",
                    f"{player['entities']['workers']} ({player['entities']['idle_workers']} idle)",
                    " ".join(
                        f"{k}={gathered[k]}"
                        for k in ("food", "wood", "stone", "metal")
                        if k in gathered
                    ),
                    _ratio(metrics.get("idle_worker_turns", 0), metrics.get("worker_turns", 0)),
                    _ratio(
                        metrics.get("blocked_producer_turns", 0), metrics.get("producer_turns", 0)
                    ),
                ]
            )
        lines += [
            f"### Seat {seat}",
            "",
            _table(
                [
                    "turn",
                    "phase",
                    "state",
                    "resources",
                    "pop",
                    "workers",
                    "gathered (cumulative)",
                    "idle worker fraction (interval)",
                    "blocked producer fraction (interval)",
                ],
                rows,
            )
            if rows
            else "No snapshots.",
            "",
        ]
    lines += ["## Military", ""]
    for seat in seats:
        last = next(
            (s["players"].get(seat) for s in reversed(snapshots) if s["players"].get(seat)), None
        )
        stats = (last or {}).get("statistics") or {}
        if not stats:
            lines.append(f"- Seat {seat}: statistics unavailable")
            continue
        killed_value = stats["enemyUnitsKilledValue"] + stats["enemyBuildingsDestroyedValue"]
        lost_value = stats["unitsLostValue"] + stats["buildingsLostValue"]
        # StatisticsTracker's loss/kill/capture `total` fields stay zero; the `Unit` and
        # `Structure` class counters carry the totals (units exclude Domestic animals).
        lines.append(
            f"- Seat {seat}: units lost {stats['unitsLost'].get('Unit')} "
            f"(value {stats['unitsLostValue']}), enemy units killed "
            f"{stats['enemyUnitsKilled'].get('Unit')} (value {stats['enemyUnitsKilledValue']}), "
            f"buildings lost {stats['buildingsLost'].get('Structure')}, enemy buildings "
            f"destroyed {stats['enemyBuildingsDestroyed'].get('Structure')}, captured "
            f"{stats['unitsCaptured'].get('Unit')}/{stats['buildingsCaptured'].get('Structure')}"
            f"; combat value efficiency {_ratio(killed_value, lost_value)}"
        )
    # Malformed records (a tampered or truncated stream) are excluded here; verify reports them.
    events = [e for e in streams["events"] if isinstance(e, dict) and "type" in e]
    lines += ["", "## Milestones", ""]
    milestones = []
    for name, kind in (
        ("first research finished", "research_finished"),
        ("first construction finished", "construction_finished"),
        ("first training finished", "training_finished"),
        ("first entity destroyed", "destroyed"),
        ("first attack", "attacked"),
    ):
        first = next((e for e in events if e["type"] == kind), None)
        milestones.append(
            [
                name,
                first["turn"] if first else "absent (censored)",
                first["decision_id"] if first else "-",
            ]
        )
    for seat in seats:
        for phase in ("town", "city"):
            first = next(
                (
                    s["turn"]
                    for s in snapshots
                    if s["players"].get(seat, {}).get("phase")
                    in ({"town": ("town", "city"), "city": ("city",)}[phase])
                ),
                None,
            )
            milestones.append(
                [
                    f"seat {seat} {phase} phase",
                    first if first is not None else "absent (censored)",
                    "-",
                ]
            )
    lines.append(_table(["milestone", "turn", "decision"], milestones))
    lines += ["", "## Notable events by decision", ""]
    grouped = defaultdict(list)
    for event in events:
        grouped[event["decision_id"]].append(event)
    for decision_id in sorted(grouped):
        items = grouped[decision_id]
        counts = Counter(e["type"] for e in items)
        summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        lines.append(f"- Decision {decision_id}: {summary}")
        damage = defaultdict(float)
        for event in items:
            if event["type"] == "attacked":
                damage[(event["attacker_owner"], event["entity"].get("owner"))] += event["damage"]
        for (attacker, target), total in sorted(damage.items(), key=lambda item: str(item[0])):
            lines.append(f"  - damage: owner {attacker} → owner {target}: {total:.1f}")
        for event in items:
            if event["type"] == "destroyed":
                killer = event.get("killer") or {}
                lines.append(
                    f"  - turn {event['turn']}: destroyed {event['entity'].get('template')} "
                    f"(owner {event['entity'].get('owner')}, cause {event['cause']}"
                    + (f", killer owner {killer.get('attacker_owner')}" if killer else "")
                    + ")"
                )
            elif event["type"] in (
                "research_finished",
                "construction_finished",
                "player_won",
                "player_defeated",
            ):
                detail = (
                    event.get("technology")
                    or (event.get("entity") or {}).get("template")
                    or event.get("player")
                )
                lines.append(f"  - turn {event['turn']}: {event['type']} {detail}")
            elif event["type"] == "training_finished":
                names = Counter(e.get("template") for e in event["entities"])
                lines.append(
                    f"  - turn {event['turn']}: training_finished owner {event['owner']}: "
                    + ", ".join(f"{k} x{v}" for k, v in sorted(names.items()))
                )
            elif event["type"] == "ownership_changed" and event["kind"] != "created":
                lines.append(
                    f"  - turn {event['turn']}: {event['kind']} {event['entity'].get('template')} "
                    f"{event['from']} → {event['to']}"
                )
    if not grouped:
        lines.append("No events were recorded.")
    reliability = result["action_reliability"]
    lines += [
        "",
        "## Action reliability",
        "",
        f"Denominator: {reliability['denominator']}.",
        f"- Stages: {json.dumps(reliability['stages'], sort_keys=True)}",
        f"- Non-applied reasons: {json.dumps(reliability['reasons'], sort_keys=True)}",
        f"- Decision outcomes: {json.dumps(result['decision_outcomes'], sort_keys=True)}",
    ]
    tools = [c for c in streams["model-calls"] if c.get("kind") == "tool"]
    usage = result.get("model_usage")
    lines += ["", "## Model usage", ""]
    if usage:
        cost = "unavailable" if usage["cost_unavailable"] else f"{usage['cost_usd']:.4f} USD"
        lines += [
            (
                f"- Provider attempts {usage['attempts']}, responses {usage['responses']}, "
                f"errors {usage['errors']}, retries {usage['retries']}; models "
                f"{json.dumps(usage['models'], sort_keys=True)}"
            ),
            (
                f"- Tokens: input {usage['input_tokens']}, output {usage['output_tokens']}, "
                f"cache read {usage['cache_read_input_tokens']}; responses without provider "
                f"counts: {usage['usage_unavailable_responses']}; cost {cost}; total latency "
                f"{usage['latency_s_total']:.2f} s"
            ),
            (
                f"- Stop reasons {json.dumps(usage['stop_reasons'], sort_keys=True)}; error "
                f"kinds {json.dumps(usage['error_kinds'], sort_keys=True)}; decision reasons "
                f"{json.dumps(usage['decision_reasons'], sort_keys=True)}"
            ),
        ]
    else:
        lines.append(
            "No model adapter was attached: provider calls, tokens, latency, retries, and cost "
            "are unavailable, not zero."
        )
    lines.append(
        f"- Tool calls through the player gateway: {len(tools)} "
        f"({json.dumps(dict(Counter(c['operation'] for c in tools)), sort_keys=True)})"
    )
    lines += ["", "## Usability versus strategy", ""]
    usability = {
        "decisions_without_submission": sum(
            1
            for d in decisions
            if d["outcome"] != "submitted"
            or (d.get("metadata") or {}).get("reason") not in (None, "submitted")
        ),
        "tool_errors": sum(
            1 for c in tools if isinstance(c.get("response"), dict) and "error" in c["response"]
        ),
        "non_applied_actions": sum(reliability["reasons"].values()),
        "provider_failures": sum(1 for d in decisions if d["outcome"] == "provider_failure"),
    }
    lines += [
        "Environment usability failures (interface, tools, budgets): "
        + json.dumps(usability, sort_keys=True),
        "",
        (
            f"Strategy outcome (objective and game state): result {result['result']}, "
            f"objective success {objective.get('success')}, achieved turn "
            f"{objective.get('achieved_turn')}."
        ),
    ]
    lines += ["", "## Trace-backed observations", ""]
    facts = []
    timeouts = [
        d for d in decisions if d["outcome"] in ("timeout", "controller_error", "malformed")
    ]
    if timeouts:
        facts.append(
            f"{len(timeouts)} decision(s) produced no batch: "
            + ", ".join(
                f"decision {d['decision_id']} seat {d['seat']} {d['outcome']}" for d in timeouts
            )
        )
    for seat, entry in result["administrative"].items():
        if entry:
            facts.append(f"Seat {seat} forfeited administratively at turn {entry['turn']}.")
    rejected = reliability["reasons"]
    if rejected:
        facts.append("Non-applied actions by reason: " + json.dumps(rejected, sort_keys=True))
    for snapshot in snapshots:
        for seat in seats:
            metrics = (snapshot.get("interval_metrics") or {}).get(seat) or {}
            if (
                metrics.get("worker_turns")
                and metrics["idle_worker_turns"] / metrics["worker_turns"] > 0.5
            ):
                facts.append(
                    f"Seat {seat}: idle worker fraction above 0.5 in the interval ending at "
                    f"turn {snapshot['turn']}."
                )
            if metrics.get("blocked_producer_turns"):
                facts.append(
                    f"Seat {seat}: production blocked for {metrics['blocked_producer_turns']} "
                    f"producer-turns in the interval ending at turn {snapshot['turn']}."
                )
    losses = [e for e in events if e["type"] == "destroyed" and e["cause"] == "killed"]
    if losses:
        owners = Counter(e["entity"].get("owner") for e in losses)
        facts.append(
            "Killed entities by owner: "
            + json.dumps({str(k): v for k, v in owners.items()}, sort_keys=True)
        )
    if any(episode["truncated"].values()):
        facts.append(
            "Truncated streams (interrupted writer): "
            + ", ".join(k for k, v in episode["truncated"].items() if v)
        )
    lines += [f"- {fact}" for fact in facts] or ["- No anomalies were recorded in the trace."]
    lines += [
        "",
        (
            "These statements are computed from the recorded trace. Interpretations of intent "
            "require additional evidence and are not made here."
        ),
        "",
    ]
    return "\n".join(lines)


def verify(directory, *, replay=False, engine=None, mod_sources=None, work_dir=None):
    """Check checksums, reproduce the result offline, and optionally replay the command log."""
    directory = Path(directory)
    episode = load_episode(directory)
    manifest = episode["manifest"]
    outcome = {"directory": str(directory)}
    if manifest is None:
        outcome["status"] = "missing_manifest"
        return outcome
    status = manifest["status"]
    outcome["manifest_status"] = status
    outcome["classification"] = "incomplete" if status == "running" else status
    mismatches = {}
    if status != "running":
        for relative, expected in manifest.get("files", {}).items():
            path = directory / relative
            actual = sha256_file(path) if path.is_file() else None
            if actual != expected:
                mismatches[relative] = {"expected": expected, "actual": actual}
    outcome["checksum_mismatches"] = mismatches
    recomputed = build_result(directory)
    stored_path = directory / "result.json"
    stored = json.loads(stored_path.read_text()) if stored_path.is_file() else None
    outcome["result_matches"] = stored == recomputed if stored else None
    outcome["result"] = recomputed["result"]
    outcome["stream_truncation"] = episode["truncated"]
    outcome["record_counts"] = recomputed["record_counts"]
    if replay:
        outcome["replay"] = replay_check(directory, engine, mod_sources or {}, work_dir)
    outcome["ok"] = (
        status in ("completed", "invalid")
        and not mismatches
        and outcome["result_matches"] is True
        and (not replay or outcome["replay"].get("ok"))
    )
    return outcome


def replay_check(directory, engine, mod_sources, work_dir=None):
    """Replay `replay/commands.txt` headlessly and compare every recorded hash."""
    from zero_ad_bench.engine import DEFAULT_ENGINE  # noqa: PLC0415

    directory = Path(directory)
    commands = directory / "replay/commands.txt"
    if not commands.is_file():
        return {"ok": False, "error": "no replay commands"}
    profile = Path(work_dir or directory / "replay-check")
    if profile.exists():
        shutil.rmtree(profile)
    profile.mkdir(parents=True)
    for name, source in mod_sources.items():
        shutil.copytree(source, profile / "data/0ad/mods" / name)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(profile)}
    for key in ("DATA", "CONFIG", "CACHE", "STATE"):
        env[f"XDG_{key}_HOME"] = str(profile / key.lower())
    log_path = profile / "replay.log"
    with log_path.open("w") as log:
        completed = subprocess.run(
            [str(engine or DEFAULT_ENGINE), f"--replay={commands}", "--hashtest-full=true"],
            env=env,
            cwd=profile,
            stdout=log,
            stderr=log,
            timeout=600,
            check=False,
        )
    log = log_path.read_text()
    hashes, _ = read_jsonl(directory / "hashes.jsonl")
    expected = len(re.findall(r"^hash ", commands.read_text(), re.MULTILINE))
    final_hash = hashes[-1]["hash"] if hashes else None
    return {
        "ok": completed.returncode == 0
        and "MISMATCH" not in log
        and "ERROR:" not in log
        and log.count("hash ok") == expected
        and (final_hash is None or f"# Final state: {final_hash}" in log),
        "return_code": completed.returncode,
        "boundary_hashes_expected": expected,
        "boundary_hashes_ok": log.count("hash ok"),
        "final_hash": final_hash,
        "final_hash_matches": final_hash is not None and f"# Final state: {final_hash}" in log,
        "log": str(log_path),
    }
