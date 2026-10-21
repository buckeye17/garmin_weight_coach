"""LLM backends behind one interface.

- `subscription` (default): the Claude Agent SDK, which runs the Claude Code
  CLI bundled with it under this machine's Claude login (the Max plan). API
  key variables are blanked in its environment so usage can't silently bill a
  key, built-in tools (files, shell) are removed, and only the app's own tools
  are offered, plus Claude Code's WebSearch/WebFetch when a request asks for web.
- `api`: the Anthropic SDK with `ANTHROPIC_API_KEY` from `.env`. Web access is a
  `web_research` tool that runs its own request with the server-side web search
  and fetch tools: their answers always carry citations, which can't be combined
  with a JSON-schema answer in one request.

Both take the same request: a system prompt, a text-only conversation, the
app's tools, optionally web access, and optionally a JSON schema the final
answer must follow.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

from gwc.coach.settings import FeatureConfig, backend_name, full_model_id
from gwc.config import get_settings

log = logging.getLogger(__name__)

MAX_TURNS = 12
MCP_SERVER = "gwc"
# Models that accept the API's server-side refusal fallback (`fallbacks: "default"`).
REFUSAL_FALLBACK_MODELS = {
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
}
# Claude Code's built-in web tools, offered on the subscription backend for web requests.
SUBSCRIPTION_WEB_TOOLS = ["WebSearch", "WebFetch"]
WEB_RESEARCH = "web_research"
# Models that take the dynamic-filtering web tool versions; others get basic web search.
DYNAMIC_WEB_MODELS = {
    "claude-fable-5-1",
    "claude-fable-5",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
}
WEB_MAX_USES = 6
MAX_RESEARCH_CONTINUATIONS = 5
RESEARCH_SYSTEM = """\
You research one question on the web for a nutrition tracker and report what you find.
Prefer official sources (the company's own website, nutrition page, calculator or PDF). \
Quote numbers exactly as published, with the serving they apply to, and give the URL of \
every page the numbers came from. If you can't find something, say so plainly instead \
of guessing."""


class LLMError(Exception):
    """A user-presentable failure from the LLM backend."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]  # JSON Schema for the input
    handler: Callable[[dict[str, Any]], Any]  # returns JSON-serializable data
    activity: Callable[[dict[str, Any]], str] | None = None  # "Searching USDA for eggs…"

    def call(self, args: dict[str, Any]) -> str:
        return json.dumps(self.handler(args), default=str)


@dataclass
class LLMRequest:
    system: str
    messages: list[dict[str, str]]  # {"role": "user"|"assistant", "content": text}
    config: FeatureConfig
    tools: list[ToolSpec] = field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    max_turns: int = MAX_TURNS
    on_text: Callable[[str], None] | None = None  # streamed text of the current turn
    on_turn: Callable[[], None] | None = None  # a new assistant turn started
    on_activity: Callable[[str], None] | None = None  # tool activity for the UI
    web: bool = False  # let Claude search and read the web


@dataclass
class LLMResult:
    text: str
    data: Any = None  # parsed structured output, when a schema was given
    model: str | None = None
    tool_calls: list[str] = field(default_factory=list)


class Backend(Protocol):
    name: str

    def run(self, req: LLMRequest) -> LLMResult: ...


def _tool_activity(tool: ToolSpec, args: dict[str, Any]) -> str:
    if tool.activity is not None:
        try:
            return tool.activity(args)
        except Exception:
            pass
    return f"Reading {tool.name.removeprefix('get_').replace('_', ' ')}…"


def _web_activity(name: str, args: dict[str, Any]) -> str:
    if name in ("WebSearch", "web_search"):
        return f"Searching the web for “{args.get('query', '')}”…"
    if name in ("WebFetch", "web_fetch"):
        host = urlparse(str(args.get("url", ""))).netloc.removeprefix("www.")
        return f"Reading {host or 'a web page'}…"
    question = str(args.get("question", ""))
    return f"Researching on the web: {question[:80]}…"


def transcript_prompt(messages: list[dict[str, str]]) -> str:
    """Flatten a conversation into one prompt for the single-shot subscription backend."""
    if len(messages) == 1:
        return messages[0]["content"]
    lines = ["Conversation so far:"]
    for m in messages[:-1]:
        who = "User" if m["role"] == "user" else "Coach (you)"
        lines.append(f"\n{who}:\n{m['content']}")
    lines.append(f"\nThe user's new message:\n{messages[-1]['content']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- subscription


class SubscriptionBackend:
    name = "subscription"

    def options(self, req: LLMRequest, tool_server: Any = None) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        settings = get_settings()
        cwd = settings.data_dir / "coach"
        cwd.mkdir(parents=True, exist_ok=True)
        web = list(SUBSCRIPTION_WEB_TOOLS) if req.web else []
        return ClaudeAgentOptions(
            system_prompt=req.system,
            model=req.config.model,
            fallback_model=req.config.fallback_model,
            effort=req.config.effort,
            tools=list(web),  # no built-in file/shell tools; web only when asked
            mcp_servers={MCP_SERVER: tool_server} if tool_server is not None else {},
            allowed_tools=[f"mcp__{MCP_SERVER}__{t.name}" for t in req.tools] + web,
            strict_mcp_config=True,
            setting_sources=[],
            max_turns=req.max_turns,
            output_format=(
                {"type": "json_schema", "schema": req.output_schema} if req.output_schema else None
            ),
            include_partial_messages=req.on_text is not None,
            cwd=str(cwd),
            env={
                # Use the Claude login (subscription), never an API key.
                "ANTHROPIC_API_KEY": "",
                "ANTHROPIC_AUTH_TOKEN": "",
                # A dozen small tools: load them upfront instead of searching.
                "ENABLE_TOOL_SEARCH": "false",
            },
        )

    def _server(self, req: LLMRequest, calls: list[str]) -> Any:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        def wrap(spec: ToolSpec):
            @tool(spec.name, spec.description, spec.schema)
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                calls.append(spec.name)
                if req.on_activity:
                    req.on_activity(_tool_activity(spec, args))
                try:
                    text = await asyncio.to_thread(spec.call, args)
                except Exception as e:  # reported to Claude, which can recover
                    log.warning("Coach tool %s failed: %s", spec.name, e)
                    return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}
                return {"content": [{"type": "text", "text": text}]}

            return handler

        return create_sdk_mcp_server(
            name=MCP_SERVER, version="1.0.0", tools=[wrap(t) for t in req.tools]
        )

    def run(self, req: LLMRequest) -> LLMResult:
        return asyncio.run(self._run(req))

    async def _run(self, req: LLMRequest) -> LLMResult:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeSDKError,
            ResultMessage,
            StreamEvent,
            TextBlock,
            ToolUseBlock,
            query,
        )

        calls: list[str] = []
        server = self._server(req, calls) if req.tools else None
        options = self.options(req, server)
        result: ResultMessage | None = None
        model = None
        last_text = ""
        try:
            async for msg in query(prompt=transcript_prompt(req.messages), options=options):
                if isinstance(msg, StreamEvent):
                    event = msg.event or {}
                    if event.get("type") == "message_start" and req.on_turn:
                        req.on_turn()
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and req.on_text:
                        req.on_text(delta.get("text", ""))
                elif isinstance(msg, AssistantMessage):
                    if msg.error:
                        raise LLMError(_assistant_error(msg.error))
                    model = msg.model or model
                    for b in msg.content:
                        # The app's own tools report from their handlers; web tools are built in.
                        if isinstance(b, ToolUseBlock) and b.name in SUBSCRIPTION_WEB_TOOLS:
                            calls.append(b.name)
                            if req.on_activity:
                                req.on_activity(_web_activity(b.name, b.input or {}))
                    texts = [b.text for b in msg.content if isinstance(b, TextBlock)]
                    if texts:
                        last_text = "".join(texts)
                elif isinstance(msg, ResultMessage):
                    result = msg
        except LLMError:
            raise
        except ClaudeSDKError as e:
            log.warning("Agent SDK error: %s", e)
            if result is None:
                raise LLMError(f"Claude couldn't finish: {e}") from e
        if result is None:
            raise LLMError("Claude didn't return a result.")
        if result.is_error or result.subtype != "success":
            detail = "; ".join(result.errors or []) or result.subtype
            raise LLMError(f"Claude couldn't finish ({detail}).")
        return LLMResult(
            text=result.result or last_text,
            data=result.structured_output,
            model=model,
            tool_calls=calls,
        )


def _assistant_error(kind: str) -> str:
    return {
        "authentication_failed": "Claude login failed. Run `claude` in a terminal and log in "
        "with your Max account, then try again.",
        "billing_error": "Your Claude plan reported a billing problem.",
        "rate_limit": "You've hit your Claude plan's usage limit for now. Try again later.",
        "server_error": "Claude is having trouble right now. Try again shortly.",
    }.get(kind, f"Claude returned an error ({kind}).")


# --------------------------------------------------------------------------- API key


class ApiBackend:
    name = "api"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def request_kwargs(self, req: LLMRequest, messages: list[dict[str, Any]]) -> dict[str, Any]:
        model = full_model_id(req.config.model)
        output_config: dict[str, Any] = {"effort": req.config.effort}
        if req.output_schema:
            output_config["format"] = {"type": "json_schema", "schema": req.output_schema}
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": 16000,
            "system": req.system,
            "messages": messages,
            "output_config": output_config,
        }
        tools = [
            {"name": t.name, "description": t.description, "input_schema": t.schema}
            for t in req.tools
        ]
        if req.web:
            tools.append(
                {
                    "name": WEB_RESEARCH,
                    "description": "Research a question on the web (search and read pages). "
                    "Returns findings with exact numbers and the URLs they came from.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "What to find, e.g. 'Chipotle chicken burrito "
                                "bowl nutrition: calories, protein, carbs, fat, fiber, sugar, "
                                "sodium per ingredient'",
                            }
                        },
                        "required": ["question"],
                    },
                }
            )
        if tools:
            kwargs["tools"] = tools
        self._add_fallbacks(kwargs, model)
        return kwargs

    @staticmethod
    def _add_fallbacks(kwargs: dict[str, Any], model: str) -> None:
        if model in REFUSAL_FALLBACK_MODELS:
            # If a safety classifier declines, the API re-runs the request on a suitable model.
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"

    @staticmethod
    def web_tools(model: str) -> list[dict[str, Any]]:
        if model in DYNAMIC_WEB_MODELS:
            return [
                {"type": "web_search_20260209", "name": "web_search", "max_uses": WEB_MAX_USES},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": WEB_MAX_USES},
            ]
        return [{"type": "web_search_20250305", "name": "web_search", "max_uses": WEB_MAX_USES}]

    def research(self, client: Any, req: LLMRequest, question: str) -> dict[str, Any]:
        """One web-research request (server-side search/fetch), resumed across pause_turn."""
        model = full_model_id(req.config.model)
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": 16000,
            "system": RESEARCH_SYSTEM,
            "tools": self.web_tools(model),
            "output_config": {"effort": req.config.effort},
        }
        self._add_fallbacks(kwargs, model)
        response = None
        for _ in range(MAX_RESEARCH_CONTINUATIONS + 1):
            response = client.beta.messages.create(messages=messages, **kwargs)
            if response.stop_reason != "pause_turn":
                break
            # The server paused its search loop; send the turn back unchanged to resume.
            messages.append({"role": "assistant", "content": response.content})
        if response is None or response.stop_reason == "refusal":
            return {"error": "Claude declined this research request."}
        sources: list[str] = []
        texts = []
        for block in response.content:
            if block.type != "text":
                continue
            texts.append(block.text)
            for c in getattr(block, "citations", None) or []:
                url = getattr(c, "url", None)
                if url and url not in sources:
                    sources.append(url)
        result: dict[str, Any] = {"findings": "".join(texts).strip(), "sources": sources}
        if response.stop_reason == "pause_turn":
            result["note"] = "Research stopped before finishing; findings may be incomplete."
        return result

    def run(self, req: LLMRequest) -> LLMResult:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        messages: list[dict[str, Any]] = [dict(m) for m in req.messages]
        tools = {t.name: t for t in req.tools}
        calls: list[str] = []
        try:
            for _ in range(req.max_turns):
                kwargs = self.request_kwargs(req, messages)
                if req.on_turn:
                    req.on_turn()
                with client.beta.messages.stream(**kwargs) as stream:
                    if req.on_text and not req.output_schema:
                        for text in stream.text_stream:
                            req.on_text(text)
                    response = stream.get_final_message()
                if response.stop_reason == "refusal":
                    raise LLMError("Claude declined this request.")
                uses = [b for b in response.content if b.type == "tool_use"]
                if response.stop_reason != "tool_use" or not uses:
                    text = "".join(b.text for b in response.content if b.type == "text")
                    data = json.loads(text) if req.output_schema else None
                    return LLMResult(text=text, data=data, model=response.model, tool_calls=calls)
                messages.append({"role": "assistant", "content": response.content})
                results = []
                for use in uses:
                    calls.append(use.name)
                    spec = tools.get(use.name)
                    if use.name == WEB_RESEARCH and req.web:
                        if req.on_activity:
                            req.on_activity(_web_activity(use.name, use.input))
                        question = str(use.input.get("question") or "")
                        content, is_error = json.dumps(self.research(client, req, question)), False
                    elif spec is None:
                        content, is_error = f"Unknown tool {use.name}", True
                    else:
                        if req.on_activity:
                            req.on_activity(_tool_activity(spec, use.input))
                        try:
                            content, is_error = spec.call(use.input), False
                        except Exception as e:
                            content, is_error = f"Error: {e}", True
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": use.id,
                            "content": content,
                            "is_error": is_error,
                        }
                    )
                messages.append({"role": "user", "content": results})
        except anthropic.AuthenticationError as e:
            raise LLMError("The Anthropic API rejected ANTHROPIC_API_KEY.") from e
        except anthropic.RateLimitError as e:
            raise LLMError("The Anthropic API rate-limited this request. Try again soon.") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"The Anthropic API returned an error ({e.status_code}).") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Couldn't reach the Anthropic API.") from e
        raise LLMError("Claude used too many tool calls without finishing.")


# --------------------------------------------------------------------------- selection

_override: Backend | None = None


def set_backend_override(backend: Backend | None) -> None:
    """Tests (and the fake test server) swap in a scripted backend here."""
    global _override
    _override = backend


def get_backend() -> Backend:
    if _override is not None:
        return _override
    if backend_name() == "api":
        key = get_settings().anthropic_api_key
        if key is None or not key.get_secret_value():
            raise LLMError("The API backend needs ANTHROPIC_API_KEY in .env.")
        return ApiBackend(key.get_secret_value())
    return SubscriptionBackend()
