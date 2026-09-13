"""Provider adapters behind one contract: neutral request in, normalized response out.

Adapters never touch the simulation. They translate a provider-neutral request (system text,
neutral messages, neutral tool specs) into the provider's wire format and return a normalized
`ProviderResponse`. Retries are owned by the model controller so every attempt is logged; SDK
or transport retries are disabled to keep the recorded attempt count exact.
"""

import hashlib
import json
import secrets
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib import error, request


STOP_REASONS = ("end_turn", "tool_use", "max_tokens", "refusal", "other")


class ProviderError(Exception):
    """A provider call failed; `retryable` separates transport/rate limits from client errors."""

    def __init__(self, kind, message, *, retryable, status=None, retry_after_s=None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.retryable = retryable
        self.status = status
        self.retry_after_s = retry_after_s

    def record(self):
        return {
            "kind": self.kind,
            "message": self.message[:500],
            "retryable": self.retryable,
            "http_status": self.status,
            "retry_after_s": self.retry_after_s,
        }


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict

    def neutral(self):
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class ProviderRequest:
    model: str
    system: str
    messages: list
    tools: list
    max_output_tokens: int
    temperature: float | None = 0.0
    effort: str | None = None
    thinking: str | None = None
    # When set, the provider must make the model call this tool (the last request of a
    # decision is restricted to submit_actions).
    force_tool: str | None = None
    metadata: dict = field(default_factory=dict)
    # Runner monotonic time, never sent to a provider or used as an observation.
    deadline_at: float | None = None

    def timeout_seconds(self, configured):
        if self.deadline_at is None:
            return configured
        remaining = self.deadline_at - time.monotonic()
        if remaining <= 0:
            raise ProviderError("timeout", "Decision deadline expired", retryable=False)
        return min(configured, remaining)

    def neutral(self):
        """Return the provider-independent request that is logged and hashed."""
        return {
            "model": self.model,
            "system": self.system,
            "messages": self.messages,
            "tools": [tool.neutral() for tool in self.tools],
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "effort": self.effort,
            "thinking": self.thinking,
            "force_tool": self.force_tool,
        }


@dataclass
class ProviderResponse:
    text: str
    tool_calls: list
    stop_reason: str
    usage: dict
    model: str | None
    request_id: str | None
    raw: dict
    served_by: str | None = None

    def record(self):
        return {
            "text": self.text,
            "tool_calls": self.tool_calls,
            "stop_reason": self.stop_reason,
            "usage": self.usage,
            "model": self.model,
            "request_id": self.request_id,
            "served_by": self.served_by,
        }


def tool_schema_hash(tools):
    """Stable hash of the neutral tool contract; identical across adapters by construction."""
    canonical = json.dumps(
        [tool.neutral() for tool in tools], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def usage_record(
    input_tokens=None,
    output_tokens=None,
    cache_read=None,
    cache_write=None,
    reasoning=None,
    provider_cost_usd=None,
    *,
    input_tokens_include_cache=False,
):
    """Usage with explicit unavailability: a missing provider count is None, never zero."""
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
        "reasoning_tokens": reasoning,
        "provider_cost_usd": provider_cost_usd,
        "input_tokens_include_cache": input_tokens_include_cache,
    }


def total_usage_tokens(usage):
    """Total provider tokens, counting caches once; None means incomplete token accounting."""
    if usage.get("input_tokens") is None or usage.get("output_tokens") is None:
        return None
    total = usage["input_tokens"] + usage["output_tokens"]
    if not usage.get("input_tokens_include_cache"):
        total += (usage.get("cache_read_input_tokens") or 0) + (
            usage.get("cache_creation_input_tokens") or 0
        )
    return total


NEUTRAL_MESSAGE_SHAPES = """
user:      {"role": "user", "content": text}
assistant: {"role": "assistant", "content": text, "tool_calls": [{id, name, input}]}
tool:      {"role": "tool", "results": [{id, name, content: text, is_error: bool}]}
"""


class AnthropicProvider:
    """Claude through the official `anthropic` SDK (Messages API with native tool use)."""

    name = "anthropic"
    supports_tools = True

    def __init__(
        self,
        model,
        *,
        api_key=None,
        base_url=None,
        timeout_s=120.0,
        fallbacks=None,
        default_thinking=None,
    ):
        import anthropic  # noqa: PLC0415 - optional dependency, imported when configured

        self.anthropic = anthropic
        self.model = model
        self.fallbacks = fallbacks
        self.default_thinking = default_thinking
        self.timeout_s = timeout_s
        # SDK retries are disabled: the controller logs every attempt itself.
        self.client = anthropic.Anthropic(
            api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=0
        )

    @staticmethod
    def wire_messages(messages):
        wire = []
        for message in messages:
            if message["role"] == "user":
                wire.append({"role": "user", "content": message["content"]})
            elif message["role"] == "assistant":
                content = []
                if message.get("content"):
                    content.append({"type": "text", "text": message["content"]})
                for call in message.get("tool_calls", []):
                    content.append(
                        {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": call["name"],
                            "input": call["input"],
                        }
                    )
                wire.append({"role": "assistant", "content": content})
            elif message["role"] == "tool":
                content = [
                    {
                        "type": "tool_result",
                        "tool_use_id": r["id"],
                        "content": r["content"],
                        "is_error": bool(r.get("is_error")),
                    }
                    for r in message["results"]
                ]
                if message.get("note"):
                    content.append({"type": "text", "text": message["note"]})
                wire.append(
                    {
                        "role": "user",
                        "content": content,
                    }
                )
        return wire

    def wire_request(self, req):
        body = {
            "model": self.model,
            "max_tokens": req.max_output_tokens,
            "system": req.system,
            "messages": self.wire_messages(req.messages),
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    "strict": True,
                }
                for t in req.tools
            ],
            "tool_choice": {"type": "tool", "name": req.force_tool}
            if req.force_tool
            else {"type": "auto"},
        }
        # Current Claude models accept no sampling parameters; determinism is not offered and
        # the recorded request states temperature as not applicable.
        thinking = req.thinking or self.default_thinking
        if thinking:
            body["thinking"] = {"type": thinking}
        if req.effort:
            body["output_config"] = {"effort": req.effort}
        return body

    def complete(self, req):
        body = self.wire_request(req)
        body["timeout"] = req.timeout_seconds(self.timeout_s)
        anthropic = self.anthropic
        try:
            if self.fallbacks:
                message = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-06-01"], fallbacks=self.fallbacks, **body
                )
            else:
                message = self.client.messages.create(**body)
        except anthropic.RateLimitError as exc:
            raise ProviderError(
                "rate_limit",
                exc.message,
                retryable=True,
                status=429,
                retry_after_s=_retry_after(exc),
            ) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                "server" if exc.status_code >= 500 else "client",
                exc.message,
                retryable=exc.status_code >= 500 or exc.status_code in (408, 409),
                status=exc.status_code,
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("transport", str(exc), retryable=True) from exc
        text = "".join(b.text for b in message.content if b.type == "text")
        calls = [
            {"id": b.id, "name": b.name, "input": b.input}
            for b in message.content
            if b.type == "tool_use"
        ]
        stop = message.stop_reason if message.stop_reason in STOP_REASONS else "other"
        usage = message.usage
        return ProviderResponse(
            text=text,
            tool_calls=calls,
            stop_reason=stop,
            usage=usage_record(
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_input_tokens,
                usage.cache_creation_input_tokens,
            ),
            model=message.model,
            request_id=getattr(message, "_request_id", None),
            raw=message.to_dict(),
        )


def _retry_after(exc):
    try:
        return float(exc.response.headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return None


class OpenAICompatibleProvider:
    """Chat Completions with function calling over plain HTTP (second provider family)."""

    name = "openai_compatible"
    supports_tools = True

    def __init__(self, model, *, api_key, base_url, timeout_s=120.0, reasoning_effort=None):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.reasoning_effort = reasoning_effort
        self.opener = request.build_opener(request.ProxyHandler({}))

    @staticmethod
    def wire_messages(system, messages):
        wire = [{"role": "system", "content": system}]
        for message in messages:
            if message["role"] == "user":
                wire.append({"role": "user", "content": message["content"]})
            elif message["role"] == "assistant":
                entry = {"role": "assistant", "content": message.get("content") or None}
                if message.get("tool_calls"):
                    entry["tool_calls"] = [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": json.dumps(c["input"])},
                        }
                        for c in message["tool_calls"]
                    ]
                wire.append(entry)
            elif message["role"] == "tool":
                for r in message["results"]:
                    wire.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
                if message.get("note"):
                    wire.append({"role": "user", "content": message["note"]})
        return wire

    def wire_tools(self, req):
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                    "strict": True,
                },
            }
            for t in req.tools
        ]

    @staticmethod
    def wire_tool_choice(req):
        if req.force_tool:
            return {"type": "function", "function": {"name": req.force_tool}}
        return "auto"

    def wire_request(self, req):
        body = {
            "model": self.model,
            "messages": self.wire_messages(req.system, req.messages),
            "tools": self.wire_tools(req),
            "tool_choice": self.wire_tool_choice(req),
            "max_completion_tokens": req.max_output_tokens,
        }
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.effort or self.reasoning_effort:
            body["reasoning_effort"] = req.effort or self.reasoning_effort
        return body

    def headers(self):
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}

    def post(self, body, timeout_s=None):
        """POST one chat completion; returns (payload, response headers)."""
        data = json.dumps(body).encode()
        # The base URL is operator configuration, never model-supplied.
        http = request.Request(  # noqa: S310
            f"{self.base_url}/chat/completions", data=data, headers=self.headers(), method="POST"
        )
        try:
            with self.opener.open(http, timeout=timeout_s or self.timeout_s) as response:
                return json.loads(response.read().decode()), dict(response.headers)
        except error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            retry_after = exc.headers.get("retry-after")
            if exc.code == 429:
                raise ProviderError(
                    "rate_limit",
                    detail,
                    retryable=True,
                    status=429,
                    retry_after_s=float(retry_after) if retry_after else None,
                ) from exc
            raise ProviderError(
                "server" if exc.code >= 500 else "client",
                detail,
                retryable=exc.code >= 500 or exc.code in (408, 409),
                status=exc.code,
            ) from exc
        except (OSError, error.URLError) as exc:
            raise ProviderError("transport", str(exc), retryable=True) from exc

    def normalize(self, payload, headers):
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {"_unparseable_arguments": function.get("arguments")}
            calls.append({"id": call.get("id"), "name": function.get("name"), "input": arguments})
        finish = choice.get("finish_reason")
        stop = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
            "content_filter": "refusal",
        }.get(finish, "other")
        usage = payload.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        cost = usage.get("cost")
        return ProviderResponse(
            text=message.get("content") or "",
            tool_calls=calls,
            stop_reason=stop,
            usage=usage_record(
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                prompt_details.get("cached_tokens"),
                None,
                details.get("reasoning_tokens"),
                float(cost) if isinstance(cost, (int, float)) else None,
                input_tokens_include_cache=True,
            ),
            model=payload.get("model"),
            request_id=headers.get("x-request-id") or headers.get("X-Request-Id"),
            raw=payload,
            served_by=payload.get("provider"),
        )

    def complete(self, req):
        payload, headers = self.post(self.wire_request(req), req.timeout_seconds(self.timeout_s))
        return self.normalize(payload, headers)


class OpenRouterProvider(OpenAICompatibleProvider):
    """OpenRouter's chat completions: one key for many models, with usage cost in every reply.

    Differences from a plain OpenAI-compatible endpoint: `max_tokens` (not
    `max_completion_tokens`), the `reasoning` object for effort, optional app attribution
    headers, optional provider routing preferences, and `usage.cost` in USD plus the upstream
    provider that served the request. No fallback model list is sent: a fallback would change
    the measured model.
    """

    name = "openrouter"
    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        model,
        *,
        api_key,
        base_url=None,
        timeout_s=120.0,
        reasoning_effort=None,
        app_url=None,
        app_title=None,
        provider_routing=None,
    ):
        super().__init__(
            model,
            api_key=api_key,
            base_url=base_url or self.DEFAULT_BASE_URL,
            timeout_s=timeout_s,
            reasoning_effort=reasoning_effort,
        )
        self.app_url = app_url
        self.app_title = app_title
        self.provider_routing = provider_routing

    def headers(self):
        headers = super().headers()
        if self.app_url:
            headers["HTTP-Referer"] = self.app_url
        if self.app_title:
            headers["X-Title"] = self.app_title
        return headers

    def wire_request(self, req):
        body = {
            "model": self.model,
            "messages": self.wire_messages(req.system, req.messages),
            "tools": self.wire_tools(req),
            "tool_choice": self.wire_tool_choice(req),
            "max_tokens": req.max_output_tokens,
        }
        if req.temperature is not None:
            body["temperature"] = req.temperature
        effort = req.effort or self.reasoning_effort
        if effort:
            body["reasoning"] = {"effort": effort}
        if self.provider_routing:
            body["provider"] = self.provider_routing
        return body


def render_transcript(messages, force_tool=None):
    """Render neutral messages as one prompt for single-shot, tool-less backends."""
    parts = []
    for message in messages:
        if message["role"] == "user":
            parts.append("### User\n" + message["content"])
        elif message["role"] == "assistant":
            text = message.get("content") or ""
            for call in message.get("tool_calls", []):
                text += "\n" + json.dumps({"tool": call["name"], "input": call["input"]})
            parts.append("### Assistant\n" + text.strip())
        elif message["role"] == "tool":
            for result in message["results"]:
                label = "error" if result.get("is_error") else "result"
                parts.append(f"### Tool {label} ({result['name']})\n{result['content']}")
            if message.get("note"):
                parts.append("### User\n" + message["note"])
    if force_tool:
        parts.append(
            f"### Assistant\nReply with exactly one JSON object calling the tool {force_tool}; "
            "no other tool is accepted for this request."
        )
    else:
        parts.append("### Assistant\nReply with exactly one JSON object for your next tool call.")
    return "\n\n".join(parts)


class CommandProvider:
    """Run a local command per request (for example `claude -p` or `codex exec`).

    The command is a completion backend only: it receives the scaffold's system prompt and the
    rendered transcript, must reply with one JSON tool call, and gets no tools of its own. The
    benchmark scaffold, budgets, and logging stay in the runner. `argv` placeholders: `{model}`,
    `{system}`, `{prompt}`, `{max_tokens}`, `{last_message_file}`, `{effort}`.
    """

    name = "command"
    supports_tools = False

    def __init__(
        self,
        model,
        *,
        argv,
        parser="claude_json",
        timeout_s=600.0,
        prompt_via="stdin",
        system_in_prompt=False,
        effort=None,
        workdir=None,
    ):
        self.model = model
        self.argv = list(argv)
        self.parser = parser
        self.timeout_s = timeout_s
        self.prompt_via = prompt_via
        self.system_in_prompt = system_in_prompt
        self.effort = effort
        # A fresh empty working directory keeps project instruction files out of the context.
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="zab-command-"))
        self.workdir.mkdir(parents=True, exist_ok=True)

    def complete(self, req):
        prompt = render_transcript(req.messages, req.force_tool)
        if self.system_in_prompt:
            prompt = "### System\n" + req.system + "\n\n" + prompt
        last_message = self.workdir / f"last-message-{secrets.token_hex(4)}.txt"
        values = {
            "model": self.model,
            "system": req.system,
            "prompt": prompt,
            "max_tokens": str(req.max_output_tokens),
            "last_message_file": str(last_message),
            "effort": req.effort or self.effort or "",
        }
        argv = [arg.format(**values) for arg in self.argv]
        if self.prompt_via == "argument":
            argv.append(prompt)
        try:
            completed = subprocess.run(
                argv,
                input=prompt if self.prompt_via == "stdin" else None,
                capture_output=True,
                text=True,
                timeout=req.timeout_seconds(self.timeout_s),
                cwd=self.workdir,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                "timeout", f"command exceeded {self.timeout_s}s", retryable=False
            ) from exc
        except OSError as exc:
            raise ProviderError("transport", str(exc), retryable=False) from exc
        if completed.returncode != 0:
            raise ProviderError(
                "client",
                f"exit {completed.returncode}: {completed.stderr[-500:]}",
                retryable=False,
                status=completed.returncode,
            )
        text, usage, model, raw = self.parse(completed.stdout, last_message)
        return ProviderResponse(
            text=text,
            tool_calls=[],
            stop_reason="end_turn",
            usage=usage,
            model=model or self.model,
            request_id=None,
            raw=raw,
            served_by="local_command",
        )

    def parse(self, stdout, last_message):
        if self.parser == "claude_json":
            payload = json.loads(stdout)
            if payload.get("is_error"):
                raise ProviderError("client", str(payload.get("result"))[:500], retryable=False)
            counts = payload.get("usage") or {}
            structured = payload.get("structured_output")
            text = json.dumps(structured) if structured is not None else payload.get("result", "")
            usage = usage_record(
                counts.get("input_tokens"),
                counts.get("output_tokens"),
                counts.get("cache_read_input_tokens"),
                counts.get("cache_creation_input_tokens"),
                None,
                payload.get("total_cost_usd"),
            )
            served = next(iter(payload.get("modelUsage") or {}), None)
            return text, usage, served, {k: v for k, v in payload.items() if k != "result"}
        if self.parser == "last_message_file":
            if not last_message.is_file():
                raise ProviderError("client", "command wrote no last message", retryable=False)
            text = last_message.read_text()
            last_message.unlink()
            return text, usage_record(), None, {"stdout_tail": stdout[-2000:]}
        return stdout.strip(), usage_record(), None, {}


COMMAND_PRESETS = {
    # Claude Code headless: our system prompt replaces its default, no built-in tools, no
    # hooks/plugins/MCP, no session persistence, JSON result with usage and cost.
    "claude_code": {
        "argv": [
            "claude",
            "-p",
            "--bare",
            "--output-format",
            "json",
            "--model",
            "{model}",
            "--system-prompt",
            "{system}",
            "--tools",
            "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--no-chrome",
        ],
        "parser": "claude_json",
        "prompt_via": "stdin",
        "system_in_prompt": False,
    },
    # Codex CLI non-interactive: read-only sandbox, final message written to a file.
    "codex": {
        "argv": [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-m",
            "{model}",
            "--output-last-message",
            "{last_message_file}",
        ],
        "parser": "last_message_file",
        "prompt_via": "argument",
        "system_in_prompt": True,
    },
}


class MockProvider:
    """Deterministic provider for tests: a policy function maps the request to a response.

    The policy sees only the neutral request (what a real model would see) and returns either
    a `ProviderResponse`, a `ProviderError`, or a plain dict `{"text", "tool_calls",
    "stop_reason", "usage"}`. Token counts are synthesized from character lengths so budget
    accounting can be exercised without a provider.
    """

    name = "mock"

    def __init__(self, policy, *, model="mock-model", supports_tools=True):
        self.policy = policy
        self.model = model
        self.supports_tools = supports_tools
        self.calls = 0

    def complete(self, req):
        self.calls += 1
        outcome = self.policy(req, self.calls)
        if isinstance(outcome, ProviderError):
            raise outcome
        if isinstance(outcome, ProviderResponse):
            return outcome
        text = outcome.get("text", "")
        calls = outcome.get("tool_calls", [])
        for call in calls:
            call.setdefault("id", "call_" + secrets.token_hex(4))
        input_chars = len(json.dumps(req.neutral()))
        output_chars = len(text) + len(json.dumps(calls))
        usage = outcome.get("usage") or usage_record(input_chars // 4, max(1, output_chars // 4))
        return ProviderResponse(
            text=text,
            tool_calls=calls,
            stop_reason=outcome.get("stop_reason", "tool_use" if calls else "end_turn"),
            usage=usage,
            model=self.model,
            request_id=f"mock-{self.calls}",
            raw={"mock": True, "attempt": self.calls, "elapsed_s": outcome.get("elapsed_s", 0)},
        )


def make_provider(config):
    """Build a provider from experiment configuration; secrets come from named env vars."""
    import os  # noqa: PLC0415

    kind = config["kind"]
    if kind == "anthropic":
        key = os.environ.get(config.get("api_key_env", "ANTHROPIC_API_KEY"))
        return AnthropicProvider(
            config["model"],
            api_key=key,
            base_url=config.get("base_url"),
            timeout_s=config.get("timeout_s", 120.0),
            fallbacks=config.get("fallbacks"),
            default_thinking=config.get("thinking"),
        )
    if kind == "openai_compatible":
        key = os.environ.get(config.get("api_key_env", "OPENAI_API_KEY"), "")
        return OpenAICompatibleProvider(
            config["model"],
            api_key=key,
            base_url=config["base_url"],
            timeout_s=config.get("timeout_s", 120.0),
            reasoning_effort=config.get("reasoning_effort"),
        )
    if kind == "openrouter":
        key = os.environ.get(config.get("api_key_env", "OPENROUTER_API_KEY"), "")
        return OpenRouterProvider(
            config["model"],
            api_key=key,
            base_url=config.get("base_url"),
            timeout_s=config.get("timeout_s", 120.0),
            reasoning_effort=config.get("reasoning_effort"),
            app_url=config.get("app_url"),
            app_title=config.get("app_title"),
            provider_routing=config.get("provider_routing"),
        )
    if kind == "command":
        preset = dict(COMMAND_PRESETS.get(config.get("preset", ""), {}))
        argv = list(config.get("argv") or preset.get("argv") or [])
        if not argv:
            raise ValueError("command provider needs a preset or argv")
        effort = config.get("effort")
        if effort and config.get("preset") == "claude_code" and "--effort" not in argv:
            argv += ["--effort", "{effort}"]
        return CommandProvider(
            config["model"],
            argv=argv,
            parser=config.get("parser", preset.get("parser", "raw")),
            timeout_s=config.get("timeout_s", 600.0),
            prompt_via=config.get("prompt_via", preset.get("prompt_via", "stdin")),
            system_in_prompt=config.get("system_in_prompt", preset.get("system_in_prompt", False)),
            effort=effort,
            workdir=config.get("workdir"),
        )
    if kind == "mock":
        # A null model: submits a wait every decision through the same tools. It is the
        # no-op baseline for the scaffold, not a stand-in for the test policies.
        return MockProvider(
            lambda _req, _n: {
                "tool_calls": [{"name": "submit_actions", "input": {"actions": [], "plan": None}}]
            },
            model=config.get("model", "mock-model"),
        )
    raise ValueError(f"Unknown provider kind {kind}")


def elapsed_since(started):
    return round(time.monotonic() - started, 6)
