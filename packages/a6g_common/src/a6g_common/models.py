"""Model client factory.

Models are created the LangChain way, as in the LangChain Academy notebooks: ``load_dotenv()``
then ``init_chat_model("gpt-5-nano")`` (provider inferred; key from ``OPENAI_API_KEY``). A role
in ``models.yaml`` may instead set ``base_url`` to use a self-hosted OpenAI-compatible server.

Structured outputs use the provider's native ``with_structured_output`` when available
(JSON-in-text otherwise), with one retry on a validation error and then a deterministic
fallback. Models never drive control: they only choose, triage, or justify.
"""

import contextlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from a6g_common.clock import time_flows
from a6g_common.config import ModelRole, ModelsConfig

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

#: Internal model calls must not stream raw tokens into a chat UI (agent-chat-ui honours it).
NOSTREAM: dict[str, Any] = {"tags": ["langsmith:nostream"]}

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def load_env(path: str | None = None) -> None:
    """Load ``.env`` (repository root / cwd) without overriding variables already set."""
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(path or find_dotenv(usecwd=True), override=False)


def chat_model(
    role: ModelRole | str, cfg: ModelsConfig, env: Mapping[str, str] | None = None
) -> BaseChatModel:
    """LangChain chat model for ``role`` (coordinator_llm, edge_vlm, onboard_vlm).

    ``env=None`` loads ``.env`` and reads ``os.environ``; tests pass an explicit mapping.
    """
    from langchain.chat_models import init_chat_model

    if env is None:
        load_env()
        env = os.environ
    endpoint = cfg.roles[ModelRole(role)]
    kwargs: dict[str, Any] = {"timeout": endpoint.timeout_s, "max_retries": endpoint.max_retries}
    if endpoint.temperature is not None:
        kwargs["temperature"] = endpoint.temperature
    provider = endpoint.provider
    api_key = env.get(endpoint.api_key_env) if endpoint.api_key_env else None
    if endpoint.base_url:
        # Self-hosted OpenAI-compatible server (llama.cpp, Ollama, vLLM): keys are optional.
        provider = provider or "openai"
        kwargs["base_url"] = endpoint.base_url
        api_key = api_key or "not-needed"
    elif provider in (None, "openai") and "OPENAI_API_KEY" in env and api_key is None:
        api_key = env["OPENAI_API_KEY"]
    if api_key:
        kwargs["api_key"] = api_key
    if (endpoint.reasoning_effort or endpoint.reasoning_summary) and not endpoint.base_url:
        # OpenAI reasoning models: the Responses API exposes reasoning summaries ("thoughts")
        # as standard ``reasoning`` content blocks that a chat UI can stream.
        reasoning: dict[str, Any] = {}
        if endpoint.reasoning_effort:
            reasoning["effort"] = endpoint.reasoning_effort
        if endpoint.reasoning_summary:
            reasoning["summary"] = "auto"
        kwargs.update(use_responses_api=True, reasoning=reasoning, output_version="v1")
    return init_chat_model(endpoint.model, model_provider=provider, **kwargs)


def embeddings(role: ModelRole | str, cfg: ModelsConfig, env: Mapping[str, str] | None = None):
    """LangChain embeddings for ``role`` (the ``embedder`` role)."""
    from langchain.embeddings import init_embeddings

    if env is None:
        load_env()
        env = os.environ
    endpoint = cfg.roles[ModelRole(role)]
    kwargs: dict[str, Any] = {}
    if endpoint.base_url:
        kwargs["base_url"] = endpoint.base_url
        kwargs["api_key"] = env.get(endpoint.api_key_env or "", "not-needed")
    elif "OPENAI_API_KEY" in env:
        kwargs["api_key"] = env["OPENAI_API_KEY"]
    model = endpoint.model if ":" in endpoint.model else f"openai:{endpoint.model}"
    return init_embeddings(model, **kwargs)


def fake_model(responses: Sequence[str]) -> BaseChatModel:
    """Deterministic model for tests: returns ``responses`` in order (cycling)."""
    return FakeListChatModel(responses=list(responses))


def _parse(content: Any, schema: type[M]) -> M:
    text = content if isinstance(content, str) else json.dumps(content)
    text = _FENCE.sub("", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return schema.model_validate_json(text[start : end + 1])


@dataclass
class LLMTrace:
    """What the operator may see of a model call: its thoughts and how long it took."""

    thoughts: str = ""
    latency_s: float = 0.0
    message_id: str | None = None
    used_fallback: bool = False
    input_tokens: int | None = None  # tokens per model call, when reported
    output_tokens: int | None = None


def reasoning_text(content: Any) -> str:
    """Reasoning summary from LangChain content blocks (v1 ``reasoning`` or OpenAI ``summary``)."""
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "reasoning":
            continue
        if isinstance(block.get("reasoning"), str):
            parts.append(block["reasoning"])
        for item in block.get("summary") or []:
            if isinstance(item, dict) and item.get("text"):
                parts.append(item["text"])
    return "\n\n".join(p for p in parts if p)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(content)


#: Who is calling a model: the coordinator runtime sets "coordinator" around
#: its graph runs; robot cognition sets its robot id (it records its own calls).
MODEL_CALLER: ContextVar[str | None] = ContextVar("a6g_model_caller", default=None)
_RECORDER: Callable[[str, str, "LLMTrace"], None] | None = None


def set_call_recorder(recorder: Callable[[str, str, "LLMTrace"], None] | None) -> None:
    """``recorder(caller, function, trace)`` for calls made while MODEL_CALLER is set to a
    caller that does not record its own (the coordinator)."""
    global _RECORDER
    _RECORDER = recorder


def _record(schema: type, trace: "LLMTrace") -> None:
    caller = MODEL_CALLER.get()
    if _RECORDER is not None and caller == "coordinator":
        with contextlib.suppress(Exception):  # metrics never break a call
            _RECORDER(caller, schema.__name__, trace)


async def structured_call(
    model: BaseChatModel | None,
    schema: type[M],
    messages: Sequence[BaseMessage],
    fallback: Callable[[], M],
    *,
    visible: bool = False,
) -> tuple[M, LLMTrace]:
    """Ask for output matching ``schema``; one retry on invalid output, then ``fallback()``.

    Uses the provider's native structured output when supported, otherwise asks for JSON
    in the text. An unreachable endpoint, a timeout or invalid output twice yields the
    fallback, so callers always get a valid value. ``visible=True`` lets a chat UI stream
    the call (reasoning models stream their thoughts); otherwise it is tagged no-stream.
    The trace carries the model's reasoning summary (if any) and the latency.
    """
    trace = LLMTrace()
    if model is None:
        trace.used_fallback = True
        return fallback(), trace
    config = None if visible else NOSTREAM
    started = time.perf_counter()

    def done(value: M, raw: Any = None, fell_back: bool = False) -> tuple[M, LLMTrace]:
        trace.latency_s = time.perf_counter() - started
        trace.used_fallback = fell_back
        if raw is not None:
            trace.thoughts = reasoning_text(getattr(raw, "content", None))
            trace.message_id = getattr(raw, "id", None)
            usage = getattr(raw, "usage_metadata", None) or {}
            trace.input_tokens = usage.get("input_tokens")
            trace.output_tokens = usage.get("output_tokens")
        _record(schema, trace)
        return value, trace

    try:
        structured = model.with_structured_output(schema, include_raw=True)
    except NotImplementedError:  # e.g. fake models, servers without tool/JSON-schema support
        structured = None
    if structured is not None:
        for attempt in range(2):
            try:
                async with time_flows():  # the world does not wait for a model
                    out = await structured.ainvoke(list(messages), config=config)
                if out.get("parsing_error") or out.get("parsed") is None:
                    raise ValueError(out.get("parsing_error") or "no structured output")
                value = out["parsed"]
                value = value if isinstance(value, schema) else schema.model_validate(value)
                return done(value, out.get("raw"))
            except (ValidationError, ValueError) as exc:
                log.info("structured output invalid (attempt %d): %s", attempt + 1, exc)
            except Exception as exc:
                if "BadRequest" not in type(exc).__name__:  # endpoint down, timeout ...
                    log.warning("model unavailable, using fallback: %s", exc)
                    return done(fallback(), fell_back=True)
                log.info("native structured output rejected, trying JSON in text: %s", exc)
                break
        else:
            return done(fallback(), fell_back=True)
    value, raw, fell_back = await _json_in_text(model, schema, messages, fallback, config)
    return done(value, raw, fell_back)


async def structured_invoke(
    model: BaseChatModel | None,
    schema: type[M],
    messages: Sequence[BaseMessage],
    fallback: Callable[[], M],
) -> tuple[M, bool]:
    """``structured_call`` without the trace: returns ``(value, used_fallback)``."""
    value, trace = await structured_call(model, schema, messages, fallback)
    return value, trace.used_fallback


async def _json_in_text(
    model: BaseChatModel,
    schema: type[M],
    messages: Sequence[BaseMessage],
    fallback: Callable[[], M],
    config: dict[str, Any] | None = NOSTREAM,
) -> tuple[M, Any, bool]:
    schema_hint = json.dumps(schema.model_json_schema(), separators=(",", ":"))
    prompt = [
        *messages,
        HumanMessage(f"Answer with ONE JSON object only, matching this JSON Schema: {schema_hint}"),
    ]
    for attempt in range(2):
        try:
            async with time_flows():
                reply = await model.ainvoke(prompt, config=config)
            return _parse(_text(reply.content), schema), reply, False
        except (ValidationError, ValueError) as exc:
            log.info("JSON output invalid (attempt %d): %s", attempt + 1, exc)
            retry = HumanMessage(f"That was invalid ({exc}). Reply with valid JSON only.")
            prompt = [*prompt, retry]
        except Exception as exc:  # endpoint down, timeout ...
            log.warning("model unavailable, using fallback: %s", exc)
            break
    return fallback(), None, True


async def one_sentence(
    model: BaseChatModel | None, system: str, prompt: str, fallback: str
) -> tuple[str, bool]:
    """A single-sentence natural-language justification, or ``fallback`` on any problem."""
    if model is None:
        return fallback, True
    try:
        async with time_flows():
            reply = await model.ainvoke(
                [SystemMessage(system), HumanMessage(prompt)], config=NOSTREAM
            )
    except Exception as exc:
        log.warning("model unavailable, using template justification: %s", exc)
        return fallback, True
    text = reply.content if isinstance(reply.content, str) else ""
    text = " ".join(text.split())
    if not text:
        return fallback, True
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return first[:300], False
