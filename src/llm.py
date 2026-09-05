"""The model boundary: one call, two backends, a committed cache.

The demo must never depend on the network. That is not a convenience -- a live
API call in front of a judge is a coin flip, and "it worked this morning" is not
a result. So every request is content-addressed and its response is written to
`cache/llm_responses.jsonl`, which is committed. With a key present the live
backend answers and populates the cache; with no key the replay backend serves
the same bytes back and the whole pipeline runs offline and deterministically.

The cache key is a hash of the *entire* request -- model, temperature, tools and
every message so far -- so a replay is exact rather than approximate. Change the
prompt and the cache misses, loudly, instead of quietly serving an answer to a
question nobody asked.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from config import MAX_TOKENS, MODEL, TEMPERATURE

CACHE_PATH = Path(__file__).resolve().parent.parent / "cache" / "llm_responses.jsonl"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


class LLMError(RuntimeError):
    pass


class CacheMiss(LLMError):
    """The offline backend was asked something the cache has never seen."""


def load_env(path: Path = ENV_PATH) -> None:
    """Read `.env` into the environment if it exists.

    Deliberately tiny and dependency-free. `.env` is gitignored; nothing in this
    repository ever reads a key from anywhere else, and no key is ever logged,
    cached or written into an audit row.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class Response:
    """One assistant turn, normalised away from any vendor's response object."""

    content: str | None
    tool_calls: tuple[dict[str, Any], ...]
    input_tokens: int
    output_tokens: int
    model: str
    backend: str

    def as_cache_payload(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "tool_calls": list(self.tool_calls),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model": self.model,
        }


def request_key(messages: list[dict], tools: list[dict], model: str) -> str:
    payload = json.dumps(
        {
            "model": model,
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
            "tools": tools,
            "messages": messages,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Backend(Protocol):
    name: str

    def complete(self, messages: list[dict], tools: list[dict]) -> Response: ...


@dataclass
class ResponseCache:
    path: Path = CACHE_PATH
    _entries: dict[str, dict] = field(default_factory=dict)
    _loaded: bool = False

    def load(self) -> None:
        self._entries = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self._entries[entry["key"]] = entry["response"]
        self._loaded = True

    def get(self, key: str) -> dict | None:
        if not self._loaded:
            self.load()
        return self._entries.get(key)

    def put(self, key: str, response: dict) -> None:
        if not self._loaded:
            self.load()
        if key in self._entries:
            return
        self._entries[key] = response
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"key": key, "response": response}, sort_keys=True) + "\n")

    def __len__(self) -> int:
        if not self._loaded:
            self.load()
        return len(self._entries)


@dataclass
class ReplayBackend:
    """Serves committed responses. This is what runs in CI and in the demo."""

    cache: ResponseCache = field(default_factory=ResponseCache)
    name: str = "replay"

    def complete(self, messages: list[dict], tools: list[dict]) -> Response:
        key = request_key(messages, tools, MODEL)
        entry = self.cache.get(key)
        if entry is None:
            raise CacheMiss(
                f"no cached response for request {key[:12]}. The prompt or the tool "
                "schemas have changed since the cache was recorded -- re-record it "
                "with a key present (`make cache`) rather than editing the cache."
            )
        return Response(
            content=entry.get("content"),
            tool_calls=tuple(entry.get("tool_calls", ())),
            input_tokens=entry.get("input_tokens", 0),
            output_tokens=entry.get("output_tokens", 0),
            model=entry.get("model", MODEL),
            backend=self.name,
        )


@dataclass
class LiveBackend:
    """Calls the API against a pinned model and records every response into the
    cache, so the next run needs no network.

    Temperature is sent only when `config.TEMPERATURE` is set; the gpt-5 family
    accepts no explicit value but its default. Determinism of the demo comes from
    the content-addressed cache, not from sampling -- see FAILURES.md #6."""

    cache: ResponseCache = field(default_factory=ResponseCache)
    model: str = MODEL
    name: str = "live"
    _client: Any = None

    def client(self) -> Any:
        if self._client is None:
            load_env()
            if not os.environ.get("OPENAI_API_KEY"):
                raise LLMError(
                    "OPENAI_API_KEY is not set. Put it in .env (gitignored) or run "
                    "offline against the committed cache."
                )
            from openai import OpenAI

            self._client = OpenAI()
        return self._client

    def complete(self, messages: list[dict], tools: list[dict]) -> Response:
        key = request_key(messages, tools, self.model)
        cached = self.cache.get(key)
        if cached is not None:
            return Response(
                content=cached.get("content"),
                tool_calls=tuple(cached.get("tool_calls", ())),
                input_tokens=cached.get("input_tokens", 0),
                output_tokens=cached.get("output_tokens", 0),
                model=cached.get("model", self.model),
                backend="replay",
            )

        try:
            client = self.client()
        except LLMError:
            raise
        try:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_completion_tokens": MAX_TOKENS,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
            }
            if TEMPERATURE is not None:
                kwargs["temperature"] = TEMPERATURE
            completion = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 -- vendor exceptions vary; the caller
            # only needs to know the model could not be reached, and every caller
            # of this treats that as "escalate" rather than "crash".
            raise LLMError(
                f"the API call failed ({type(exc).__name__}: {exc}). The recorded "
                "cache is what the demo runs on; this path is only used when "
                "re-recording."
            ) from exc
        choice = completion.choices[0].message
        calls = tuple(
            {
                "id": call.id,
                "name": call.function.name,
                "arguments": call.function.arguments,
            }
            for call in (choice.tool_calls or ())
        )
        usage = completion.usage
        response = Response(
            content=choice.content,
            tool_calls=calls,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model=completion.model,
            backend=self.name,
        )
        self.cache.put(key, response.as_cache_payload())
        return response


def default_backend(*, prefer_live: bool | None = None) -> Backend:
    """Live when a key is present, replay otherwise.

    `prefer_live=False` forces offline even with a key, which is what the tests
    and `make demo` use -- a test that silently starts spending money is a bad
    test.
    """
    load_env()
    if prefer_live is False:
        return ReplayBackend()
    has_key = bool(os.environ.get("OPENAI_API_KEY"))
    if prefer_live and not has_key:
        raise LLMError("live backend requested but OPENAI_API_KEY is not set")
    return LiveBackend() if has_key else ReplayBackend()
