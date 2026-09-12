"""Frontier-model controller: standardized prompt and tools, private notes, budgets, logging.

The controller sees only its seat's gateway. Every provider attempt is logged exactly as sent
and received; the runner submits at most one action batch per decision, so provider retries
can never duplicate game actions. Budget exhaustion raises `AgentStop`, which the runner
records administratively. A persistent provider outage raises `ProviderFailure`, which the
runner records as an infrastructure failure, never as a strategic no-op.
"""

import hashlib
import json
import time

from zero_ad_bench.agents import AgentStop, DecisionResult, ProviderFailure
from zero_ad_bench.environment import BudgetExceeded
from zero_ad_bench.providers import ProviderError, ProviderRequest, ToolSpec, tool_schema_hash


SCAFFOLD_VERSION = "2"

# Public action families (M2 contract): required inputs and the scaffold's documented defaults
# for inputs a model may omit. The engine contract stays strict; the scaffold fills these
# before submission and records the filled action.
ACTION_FIELDS = {
    "wait": [],
    "resign": [],
    "move": ["units", "position", "queued"],
    "attack_move": ["units", "position", "queued", "allow_capture"],
    "attack": ["units", "target", "queued", "allow_capture"],
    "gather": ["units", "target", "queued"],
    "return_resources": ["units", "target", "queued"],
    "repair": ["units", "target", "queued", "autocontinue"],
    "build": ["units", "template", "position", "angle", "queued", "autorepair", "autocontinue"],
    "train": ["building", "template", "count"],
    "research": ["building", "technology"],
    "cancel_production": ["building", "queue"],
    "set_rally_point": ["building", "position", "queued"],
    "stop": ["units", "queued"],
    "stance": ["units", "stance"],
    "garrison": ["units", "holder", "queued"],
    "unload": ["units", "holder"],
}
ACTION_DEFAULTS = {
    "queued": False,
    "allow_capture": False,
    "autorepair": True,
    "autocontinue": False,
    "angle": 0,
}
MAX_ACTIONS = 20


def validate_actions(actions):
    """Check a batch against the public action schema and fill documented defaults.

    Returns `(normalized_actions, errors)`. Errors are specific and safe (field names and
    types only), so a model can correct its batch within the same decision. Engine-side
    validation still runs on every submitted action.
    """
    if not isinstance(actions, list):
        return [], ["actions must be a list of objects"]
    if len(actions) > MAX_ACTIONS:
        return [], [f"at most {MAX_ACTIONS} actions per decision, got {len(actions)}"]
    normalized = []
    errors = []
    seen = set()
    for index, action in enumerate(actions):
        label = f"action {index}"
        if not isinstance(action, dict):
            errors.append(f"{label}: must be an object")
            continue
        action_id = action.get("action_id")
        if isinstance(action_id, str) and action_id:
            label = f"action {action_id!r}"
        kind = action.get("type")
        if not isinstance(action_id, str) or not action_id:
            errors.append(f"{label}: action_id must be a non-empty string")
        elif action_id in seen:
            errors.append(f"{label}: duplicate action_id")
        seen.add(action_id)
        if kind not in ACTION_FIELDS:
            errors.append(
                f"{label}: unknown type {kind!r}; known types: " + ", ".join(sorted(ACTION_FIELDS))
            )
            continue
        allowed = {"action_id", "type", *ACTION_FIELDS[kind]}
        unknown = sorted(set(action) - allowed)
        if unknown:
            errors.append(
                f"{label}: unknown field(s) {unknown} for type {kind!r}; allowed: "
                + ", ".join(ACTION_FIELDS[kind])
            )
        filled = dict(action)
        for field in ACTION_FIELDS[kind]:
            if field not in filled and field in ACTION_DEFAULTS:
                filled[field] = ACTION_DEFAULTS[field]
        missing = [f for f in ACTION_FIELDS[kind] if f not in filled]
        if missing:
            errors.append(f"{label}: missing required field(s) {missing} for type {kind!r}")
        normalized.append(filled)
    return normalized, errors


SECTIONS = [
    "own_entities",
    "visible_entities",
    "last_seen",
    "events",
    "action_results",
    "action_lifecycle",
    "map_cells",
    "resource_clusters",
]
HANDLE = {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,80}$"}


def _schema(properties, required):
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


TOOLS = [
    ToolSpec(
        "read_briefing",
        "Read the next page of the deterministic text briefing for the "
        "current observation. Pass the cursor from a previous page to continue.",
        _schema(
            {
                "cursor": {"type": ["string", "null"]},
                "max_chars": {"type": "integer", "minimum": 1024, "maximum": 32768},
            },
            ["cursor", "max_chars"],
        ),
    ),
    ToolSpec(
        "inspect_entities",
        "Return the full permitted records for up to 64 known handles "
        "(own, visible, or remembered). Unknown handles return unavailable_entity.",
        _schema(
            {"handles": {"type": "array", "items": HANDLE, "minItems": 1, "maxItems": 64}},
            ["handles"],
        ),
    ),
    ToolSpec(
        "inspect_region",
        "Return known entities and map cells inside half-open world bounds.",
        _schema(
            {
                "min_x": {"type": "number"},
                "min_z": {"type": "number"},
                "max_x": {"type": "number"},
                "max_z": {"type": "number"},
            },
            ["min_x", "min_z", "max_x", "max_z"],
        ),
    ),
    ToolSpec(
        "inspect_section",
        "Page through one observation array.",
        _schema(
            {
                "section": {"type": "string", "enum": SECTIONS},
                "cursor": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 64},
            },
            ["section", "cursor", "limit"],
        ),
    ),
    ToolSpec(
        "catalog",
        "Look up public rules and your effective values for template and "
        "technology names (up to 64 each).",
        _schema(
            {
                "templates": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
                "technologies": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
            },
            ["templates", "technologies"],
        ),
    ),
    ToolSpec(
        "write_notes",
        "Replace your private notes, which are returned to you at every "
        "later decision. Keep them under the character limit; longer notes are cut.",
        _schema({"text": {"type": "string"}}, ["text"]),
    ),
    ToolSpec(
        "submit_actions",
        "Submit this decision's action batch (an empty list means wait) "
        "and optionally a short declared plan. This ends the decision.",
        _schema(
            {
                "actions": {"type": "array", "items": {"type": "object"}, "maxItems": 20},
                "plan": {"type": ["string", "null"]},
            },
            ["actions", "plan"],
        ),
    ),
]

SYSTEM_PROMPT = """You control one player in a real-time strategy game (0 A.D.) through text.
Simulated time is paused while you decide. Each decision covers a fixed interval of engine
turns (one turn is 200 ms). You act through tools only; you cannot see the map as an image.

Information policy: you see your own entities in full, currently visible enemy or neutral
entities with public fields only, and your remembered last-seen records with their observation
turn. Nothing you cannot see is refreshed. "unknown", "null", or "unavailable" means the fact
is not known to you; it is never a zero. Never invent handles: use only handles that appear in
your observation or tool results.

Coordinates: (x, z) in world meters, origin (0, 0), positive axes as reported in the map bounds;
angles in radians. Map cells are 16 meters square and describe their center sample.

Actions (every action needs a unique "action_id" and a "type"; unknown fields are rejected):
- wait; resign
- move {units, position, queued}; attack_move {units, position, queued, allow_capture}
- attack {units, target, queued, allow_capture}; gather {units, target, queued}
- return_resources {units, target, queued}; repair {units, target, queued, autocontinue}
- build {units, template, position, angle, queued, autorepair, autocontinue}
- train {building, template, count}; research {building, technology}
- cancel_production {building, queue}; set_rally_point {building, position, queued}
- stop {units, queued}; stance {units, stance}
- garrison {units, holder, queued}; unload {units, holder}
Units are 1-64 distinct own handles; train count is 1-5; stances are violent, aggressive,
defensive, passive, standground. Build and train templates must be offered by the selected own
entities (see their buildable/trainable lists); research must be in the building's researchable
list. An accepted action is not a completed one: results report rejected, submitted, applied,
or failed, and later lifecycle records report completion.

When omitted, queued defaults to false, allow_capture to false, autorepair to true,
autocontinue to false, and angle to 0; every other listed field is required, and unknown fields
are rejected. If a submitted batch has schema problems, submit_actions returns an error naming
each problem and nothing is submitted; fix the batch and call submit_actions again.

Budget per decision: at most 20 actions, a fixed number of read tool calls, and a fixed number
of model requests; the remaining counts are stated in each decision header. Finish every
decision by calling submit_actions exactly once (an empty list means wait). If you cannot
decide, submit an empty list rather than nothing. Keep private notes with write_notes; they
are the only memory carried between decisions besides the observation itself. You may call
several tools in one response: read what you need together, and call write_notes in the same
response as submit_actions so notes never cost a separate model request.
"""

JSON_FALLBACK_PROMPT = """
This provider has no native tool calling. Reply with exactly one JSON object per turn of the
form {"tool": <tool name>, "input": <object matching that tool's schema>} and nothing else.
Available tools and their input schemas:
"""


class ModelController:
    """Decide through a provider with exact logging, budgets, and a private notes document."""

    name = "model"

    def __init__(self, provider, config):
        self.provider = provider
        self.config = config
        budgets = config.get("budgets", {})
        self.requests_per_decision = int(budgets.get("model_requests_per_decision", 4))
        self.output_tokens_per_decision = int(budgets.get("output_tokens_per_decision", 8000))
        self.episode_tokens = budgets.get("episode_tokens")
        self.episode_cost_usd = budgets.get("episode_cost_usd")
        self.pricing = config.get("pricing_usd_per_million") or {}
        self.notes_max_chars = int((config.get("notes") or {}).get("max_chars", 4000))
        self.tool_result_max_chars = int(config.get("tool_result_max_chars", 24000))
        retry = config.get("retry") or {}
        self.max_attempts = int(retry.get("max_attempts", 3))
        self.backoff_s = float(retry.get("backoff_s", 2.0))
        sampling = config.get("sampling") or {}
        self.temperature = sampling.get("temperature", 0.0)
        self.effort = (config.get("provider") or {}).get("effort")
        self.thinking = (config.get("provider") or {}).get("thinking")
        self.supports_tools = getattr(provider, "supports_tools", True)
        self.system = SYSTEM_PROMPT
        if not self.supports_tools:
            self.system += JSON_FALLBACK_PROMPT + json.dumps(
                [tool.neutral() for tool in TOOLS], sort_keys=True
            )
        self.system_prompt_hash = hashlib.sha256(self.system.encode()).hexdigest()
        self.tool_schema_hash = tool_schema_hash(TOOLS)
        self.notes = ""
        self.previous_plan = None
        self.totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "cost_unavailable": False,
            "requests": 0,
            "attempts": 0,
            "retries": 0,
        }
        self.budget_stop = None

    # Accounting

    def cost(self, usage):
        if usage.get("provider_cost_usd") is not None:
            return round(float(usage["provider_cost_usd"]), 8)
        rates = self.pricing
        parts = [
            ("input_tokens", "input"),
            ("output_tokens", "output"),
            ("cache_read_input_tokens", "cache_read"),
            ("cache_creation_input_tokens", "cache_write"),
        ]
        total = 0.0
        for field, rate in parts:
            count = usage.get(field)
            price = rates.get(rate)
            if count is None:
                continue
            if price is None:
                return None
            total += count * price / 1_000_000
        return round(total, 8)

    def _account(self, usage):
        self.totals["requests"] += 1
        for key in ("input_tokens", "output_tokens"):
            if usage.get(key) is not None:
                self.totals[key] += usage[key]
        cost = self.cost(usage)
        if cost is None:
            self.totals["cost_unavailable"] = True
        else:
            self.totals["cost_usd"] = round(self.totals["cost_usd"] + cost, 8)
        return cost

    def _exhausted(self):
        used = self.totals["input_tokens"] + self.totals["output_tokens"]
        if self.episode_tokens is not None and used >= self.episode_tokens:
            return f"episode token ceiling {self.episode_tokens} reached ({used})"
        if (
            self.episode_cost_usd is not None
            and not self.totals["cost_unavailable"]
            and self.totals["cost_usd"] >= self.episode_cost_usd
        ):
            return f"episode cost ceiling {self.episode_cost_usd} USD reached"
        return None

    # Prompt assembly

    def header(self, gateway, requests_left, output_left):
        view = gateway.observation()
        return (
            f"Decision {gateway.decision_id} at completed turn {gateway.turn} "
            f"({view['sim_time_ms']} ms simulated). Interval: {gateway.decision_turns} turns. "
            f"Budgets: {gateway.read_budget - gateway.reads} read tool calls, {requests_left} "
            f"model requests, {output_left} output tokens, 20 actions.\n"
            f"Your previous declared plan: {self.previous_plan or 'none'}\n"
            f"Your private notes ({len(self.notes)}/{self.notes_max_chars} chars):\n"
            f"{self.notes or '(empty)'}\n"
        )

    # Tool execution

    def execute_tool(self, gateway, name, arguments):  # noqa: PLR0911 - one exit per tool
        """Run one tool. Returns (content, is_error, submission) where submission ends the turn."""
        if not isinstance(arguments, dict):
            return "invalid tool input: expected an object", True, None
        if name == "submit_actions":
            actions, errors = validate_actions(arguments.get("actions"))
            if errors:
                self.submission_errors += len(errors)
                return (
                    "batch not submitted; fix these and submit again: " + "; ".join(errors),
                    True,
                    None,
                )
            plan = arguments.get("plan")
            return "submitted", False, (actions, plan if isinstance(plan, str) else None)
        if name == "write_notes":
            text = arguments.get("text")
            if not isinstance(text, str):
                return "invalid write_notes input: text must be a string", True, None
            self.notes_compacted = len(text) > self.notes_max_chars
            self.notes = text[: self.notes_max_chars]
            suffix = ", truncated to the limit)" if self.notes_compacted else ")"
            return f"notes stored ({len(self.notes)} chars" + suffix, False, None
        try:
            data = self._read(gateway, name, arguments)
        except BudgetExceeded as exc:
            return str(exc), True, None
        if data is None:
            return f"unknown tool {name}", True, None
        if isinstance(data, dict) and "error" in data:
            code = (data["error"] or {}).get("code", "error")
            return f"query rejected: {code}", True, None
        content = json.dumps(data, sort_keys=True)
        if len(content) > self.tool_result_max_chars:
            omitted = len(content) - self.tool_result_max_chars
            content = content[: self.tool_result_max_chars]
            content += f"... [truncated {omitted} chars; narrow the query]"
        return content, False, None

    @staticmethod
    def _read(gateway, name, arguments):
        """Dispatch a read tool to the gateway; None means the tool name is unknown."""
        readers = {
            "read_briefing": lambda: gateway.inspect(
                "briefing",
                cursor=arguments.get("cursor"),
                max_chars=arguments.get("max_chars", 12000),
            ),
            "inspect_entities": lambda: gateway.inspect(
                "entities", handles=arguments.get("handles")
            ),
            "inspect_region": lambda: gateway.inspect(
                "region",
                bounds={k: arguments.get(k) for k in ("min_x", "min_z", "max_x", "max_z")},
            ),
            "inspect_section": lambda: gateway.inspect(
                "section",
                section=arguments.get("section"),
                cursor=arguments.get("cursor"),
                limit=arguments.get("limit", 32),
            ),
            "catalog": lambda: gateway.catalog(
                arguments.get("templates") or [], arguments.get("technologies") or []
            ),
        }
        reader = readers.get(name)
        return reader() if reader else None

    # Provider calls

    def _call(self, gateway, request, request_index, started):
        last = None
        for attempt in range(1, self.max_attempts + 1):
            began = time.monotonic()
            error = None
            response = None
            try:
                response = self.provider.complete(request)
            except ProviderError as exc:
                error = exc
            except Exception as exc:  # noqa: BLE001 - an adapter defect is infrastructure
                error = ProviderError("adapter_error", repr(exc), retryable=False)
            elapsed = round(time.monotonic() - began, 6)
            self.totals["attempts"] += 1
            cost = self._account(response.usage) if response else None
            gateway.record(
                "model",
                {
                    "request_index": request_index,
                    "attempt": attempt,
                    "provider": self.provider.name,
                    "model": request.model,
                    "request": request.neutral(),
                    "response": response.record() if response else None,
                    "error": error.record() if error else None,
                    "usage": response.usage if response else None,
                    "cost_usd": cost,
                    "cost_source": None
                    if cost is None
                    else (
                        "provider"
                        if response.usage.get("provider_cost_usd") is not None
                        else "configured"
                    ),
                    "latency_s": elapsed,
                    "system_prompt_hash": self.system_prompt_hash,
                    "tool_schema_hash": self.tool_schema_hash,
                    "scaffold_version": SCAFFOLD_VERSION,
                },
            )
            if response is not None:
                return response
            last = error
            if not error.retryable or attempt == self.max_attempts:
                break
            self.totals["retries"] += 1
            wait = error.retry_after_s or self.backoff_s * attempt
            remaining = gateway.deadline_s - (time.monotonic() - started)
            if wait >= remaining:
                break
            time.sleep(wait)
        raise ProviderFailure(last.record() if last else {"kind": "unknown"})

    def decide(self, gateway):
        started = time.monotonic()
        exhausted = self._exhausted()
        if exhausted:
            self.budget_stop = exhausted
            raise AgentStop("budget_stop", exhausted)
        self.notes_compacted = False
        self.submission_errors = 0
        output_left = self.output_tokens_per_decision
        briefing = gateway.briefing()
        text = briefing.get("text", "") if isinstance(briefing, dict) else ""
        omitted = briefing.get("remaining_count", 0) if isinstance(briefing, dict) else 0
        messages = [
            {
                "role": "user",
                "content": self.header(gateway, self.requests_per_decision, output_left)
                + "\nObservation briefing:\n"
                + text
                + (
                    f"\n[{omitted} more briefing rows; use read_briefing with cursor "
                    f"{briefing.get('next_cursor')!r} to continue]"
                    if omitted
                    else ""
                ),
            }
        ]
        final = None
        reason = "request_budget_exhausted"
        requests_used = 0
        for request_index in range(self.requests_per_decision):
            if output_left <= 0:
                reason = "decision_output_tokens_exhausted"
                break
            request = ProviderRequest(
                model=self.provider.model,
                system=self.system,
                messages=list(messages),
                tools=TOOLS if self.supports_tools else [],
                max_output_tokens=output_left,
                temperature=self.temperature,
                effort=self.effort,
                thinking=self.thinking,
            )
            response = self._call(gateway, request, request_index, started)
            requests_used += 1
            if response.usage.get("output_tokens") is not None:
                output_left -= response.usage["output_tokens"]
            if response.stop_reason == "refusal":
                reason = "refusal"
                break
            calls = list(response.tool_calls)
            if not calls and not self.supports_tools and response.text.strip():
                parsed = self._parse_json_call(response.text)
                if parsed is None:
                    reason = "malformed_model_output"
                    break
                calls = [parsed]
            messages.append({"role": "assistant", "content": response.text, "tool_calls": calls})
            if not calls:
                reason = "no_submission" if response.stop_reason != "max_tokens" else "max_tokens"
                break
            results = []
            for call in calls:
                content, is_error, submission = self.execute_tool(
                    gateway, call["name"], call["input"]
                )
                results.append(
                    {
                        "id": call["id"],
                        "name": call["name"],
                        "content": content,
                        "is_error": is_error,
                    }
                )
                if submission is not None:
                    final = submission
                    break
            messages.append({"role": "tool", "results": results})
            if final is not None:
                reason = "submitted"
                break
        actions, plan = final if final is not None else ([], None)
        if final is None and self.submission_errors and reason == "request_budget_exhausted":
            reason = "invalid_submission"
        self.previous_plan = plan if final is not None else self.previous_plan
        return DecisionResult(
            actions,
            {
                "controller": self.name,
                "scaffold_version": SCAFFOLD_VERSION,
                "provider": self.provider.name,
                "model": self.provider.model,
                "system_prompt_hash": self.system_prompt_hash,
                "tool_schema_hash": self.tool_schema_hash,
                "reason": reason,
                "plan": plan,
                "notes": self.notes,
                "notes_compacted": self.notes_compacted,
                "model_requests": requests_used,
                "submission_errors": self.submission_errors,
                "output_tokens_left": output_left,
                "episode_totals": dict(self.totals),
                "transcript_messages": len(messages),
            },
        )

    @staticmethod
    def _parse_json_call(text):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("tool"), str):
            return None
        return {
            "id": "json-" + hashlib.sha1(text.encode()).hexdigest()[:8],  # noqa: S324
            "name": data["tool"],
            "input": data.get("input", {}),
        }
