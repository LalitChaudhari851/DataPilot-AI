"""
Model Router — Routes LLM requests to the optimal provider with fallback.
Supports routing by task type (fast vs accurate), automatic failover,
circuit breaker protection, and retry with exponential backoff.
"""

import time
import threading
import structlog

from app.llm.base import BaseLLMProvider
from app.llm.providers import (
    HuggingFaceProvider,
    OpenAIProvider,
    AnthropicProvider,
    OllamaProvider,
    GroqProvider,
)

logger = structlog.get_logger()


class CircuitBreaker:
    """
    Circuit breaker for LLM providers.
    
    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Provider is failing, requests are short-circuited
    - HALF_OPEN: Testing if provider has recovered
    
    Trips after `failure_threshold` consecutive failures.
    Resets after `recovery_timeout` seconds.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 3, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == self.OPEN:
                # Check if recovery timeout has elapsed
                if time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._state = self.HALF_OPEN
            return self._state

    def record_success(self):
        """Record a successful call — resets the breaker."""
        with self._lock:
            self._failure_count = 0
            self._state = self.CLOSED

    def record_failure(self):
        """Record a failed call — may trip the breaker."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._failure_count >= self.failure_threshold:
                self._state = self.OPEN
                logger.warning(
                    "circuit_breaker_opened",
                    failures=self._failure_count,
                    recovery_in_seconds=self.recovery_timeout,
                )

    def is_available(self) -> bool:
        """Check if requests can pass through."""
        return self.state != self.OPEN


class TokenTracker:
    """
    Tracks token usage per LLM request for cost estimation and observability.

    Uses tiktoken for accurate counting when available, falls back to
    character-based heuristic (~4 chars per token). Thread-safe.
    """

    def __init__(self):
        self._encoder = None
        self._total_input = 0
        self._total_output = 0
        self._total_cost_usd = 0.0
        self._request_count = 0
        self._lock = threading.Lock()

        try:
            import tiktoken
            self._encoder = tiktoken.get_encoding("cl100k_base")
            logger.info("token_tracker_initialized", encoder="tiktoken_cl100k")
        except ImportError:
            logger.info("token_tracker_initialized", encoder="char_heuristic",
                        hint="pip install tiktoken for accurate token counts")

    # Pricing per token (approximate, GPT-4o-mini rates as baseline)
    _PRICING = {
        "input_per_token": 0.00000015,   # $0.15 / 1M input tokens
        "output_per_token": 0.0000006,   # $0.60 / 1M output tokens
    }

    def count_tokens(self, text: str) -> int:
        """Count tokens in a text string."""
        if self._encoder:
            return len(self._encoder.encode(text))
        return max(1, len(text) // 4)  # ~4 chars per token heuristic

    def track(self, messages: list[dict], response: str, provider: str = "unknown") -> dict:
        """
        Track token usage for a single request/response pair.

        Returns a dict with token counts and estimated cost.
        """
        input_text = " ".join(m.get("content", "") for m in messages)
        input_tokens = self.count_tokens(input_text)
        output_tokens = self.count_tokens(response)
        total = input_tokens + output_tokens

        cost = (
            input_tokens * self._PRICING["input_per_token"]
            + output_tokens * self._PRICING["output_per_token"]
        )

        with self._lock:
            self._total_input += input_tokens
            self._total_output += output_tokens
            self._total_cost_usd += cost
            self._request_count += 1

        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total,
            "estimated_cost_usd": round(cost, 6),
            "provider": provider,
        }

    def get_totals(self) -> dict:
        """Get aggregate token usage stats."""
        with self._lock:
            return {
                "total_input_tokens": self._total_input,
                "total_output_tokens": self._total_output,
                "total_tokens": self._total_input + self._total_output,
                "total_cost_usd": round(self._total_cost_usd, 4),
                "total_requests": self._request_count,
                "avg_tokens_per_request": round(
                    (self._total_input + self._total_output) / max(self._request_count, 1), 1
                ),
            }


class ModelRouter:
    """
    Intelligent model router with fallback chains and circuit breakers.
    
    Routing strategies:
    - "fast": Use the quickest available model (intent classification, simple queries)
    - "accurate": Use the most capable model (complex SQL generation)
    - "default": Use the configured default provider
    """

    def __init__(self, config: dict):
        """
        Initialize with provider configs.
        
        config = {
            "default_provider": "huggingface",
            "huggingface_token": "...",
            "huggingface_model": "Qwen/Qwen2.5-Coder-32B-Instruct",
            "openai_api_key": "...",  # optional
            "anthropic_api_key": "...",  # optional
            "ollama_base_url": "...",  # optional
        }
        """
        self.providers: dict[str, BaseLLMProvider] = {}
        self.breakers: dict[str, CircuitBreaker] = {}
        self.default_provider = config.get("default_provider", "groq")
        self.token_tracker = TokenTracker()
        self._init_providers(config)

        # Auto-detect default if configured provider isn't available
        if self.default_provider not in self.providers and self.providers:
            self.default_provider = next(iter(self.providers))
            logger.warning("default_provider_unavailable", fallback=self.default_provider)

        # Routing preferences — Groq excels at both speed and accuracy
        self.routing = {
            "fast": self.default_provider,       # Fast model for classification
            "accurate": self.default_provider,    # Best model for SQL generation
            "default": self.default_provider,     # Default
        }

        # Groq has a dedicated fast model (8B) for lightweight tasks
        if "groq" in self.providers:
            self.routing["fast"] = "groq"       # Uses fast_model via model_override
            self.routing["accurate"] = "groq"   # Uses primary model (70B)
            self.routing["default"] = "groq"
        if "openai" in self.providers and "groq" not in self.providers:
            self.routing["accurate"] = "openai"
        if "anthropic" in self.providers and "groq" not in self.providers:
            self.routing["accurate"] = "anthropic"

        logger.info(
            "model_router_initialized",
            providers=list(self.providers.keys()),
            default=self.default_provider,
            routing=self.routing,
        )

    def _init_providers(self, config: dict):
        """Initialize available providers based on config."""
        # Groq (primary — ultra-low-latency LPU inference)
        groq_key = config.get("groq_api_key")
        if groq_key:
            try:
                self.providers["groq"] = GroqProvider(
                    api_key=groq_key,
                    model=config.get("groq_model_primary", "llama-3.3-70b-versatile"),
                    fast_model=config.get("groq_model_fast", "llama-3.1-8b-instant"),
                    base_url=config.get("groq_base_url", "https://api.groq.com/openai/v1"),
                )
                self.breakers["groq"] = CircuitBreaker()
                logger.info("provider_initialized", provider="groq",
                            model=config.get("groq_model_primary", "llama-3.3-70b-versatile"))
            except Exception as e:
                logger.warning("provider_init_failed", provider="groq", error=str(e))

        # HuggingFace (fallback)
        hf_token = config.get("huggingface_token")
        if hf_token:
            try:
                self.providers["huggingface"] = HuggingFaceProvider(
                    api_token=hf_token,
                    model=config.get("huggingface_model", "Qwen/Qwen2.5-Coder-32B-Instruct"),
                )
                self.breakers["huggingface"] = CircuitBreaker()
                logger.info("provider_initialized", provider="huggingface")
            except Exception as e:
                logger.warning("provider_init_failed", provider="huggingface", error=str(e))

        # OpenAI (fallback)
        openai_key = config.get("openai_api_key")
        if openai_key:
            try:
                self.providers["openai"] = OpenAIProvider(
                    api_key=openai_key,
                    model=config.get("openai_model", "gpt-4o-mini"),
                )
                self.breakers["openai"] = CircuitBreaker()
                logger.info("provider_initialized", provider="openai")
            except Exception as e:
                logger.warning("provider_init_failed", provider="openai", error=str(e))

        # Anthropic (fallback)
        anthropic_key = config.get("anthropic_api_key")
        if anthropic_key:
            try:
                self.providers["anthropic"] = AnthropicProvider(
                    api_key=anthropic_key,
                    model=config.get("anthropic_model", "claude-sonnet-4-20250514"),
                )
                self.breakers["anthropic"] = CircuitBreaker()
                logger.info("provider_initialized", provider="anthropic")
            except Exception as e:
                logger.warning("provider_init_failed", provider="anthropic", error=str(e))

        # Ollama (local fallback)
        ollama_url = config.get("ollama_base_url")
        if ollama_url:
            try:
                provider = OllamaProvider(
                    base_url=ollama_url,
                    model=config.get("ollama_model", "llama3"),
                )
                if provider.health_check():
                    self.providers["ollama"] = provider
                    self.breakers["ollama"] = CircuitBreaker()
                    logger.info("provider_initialized", provider="ollama")
                else:
                    logger.warning("provider_unavailable", provider="ollama")
            except Exception as e:
                logger.warning("provider_init_failed", provider="ollama", error=str(e))

        if not self.providers:
            raise RuntimeError(
                "No LLM providers configured. Set at least one of: "
                "GROQ_API_KEY, HUGGINGFACEHUB_API_TOKEN, OPENAI_API_KEY in .env"
            )

    def generate(
        self,
        messages: list[dict],
        model_preference: str = "default",
        max_retries: int = 2,
        timeout: float = 15.0,
        **kwargs,
    ) -> str:
        """
        Route a generation request to the best available provider.
        Falls back through providers if the primary one fails.
        Applies circuit breaker, retry logic, and a total timeout per request.
        """
        # Total deadline prevents thread pool exhaustion under LLM degradation
        deadline = time.monotonic() + timeout

        # Provider pinning for strict deterministic evaluation (Phase 8)
        pinned_provider = kwargs.pop("pinned_provider", None)
        if pinned_provider:
            if pinned_provider not in self.providers:
                raise RuntimeError(
                    f"Pinned provider '{pinned_provider}' is not configured/available. "
                    f"Configured providers: {list(self.providers.keys())}"
                )
            target = pinned_provider
            fallback_chain = [pinned_provider]
        else:
            # Determine target provider
            target = self.routing.get(model_preference, self.default_provider)

            # Build fallback chain: target → default → all others
            fallback_chain = [target]
            if self.default_provider not in fallback_chain:
                fallback_chain.append(self.default_provider)
            for name in self.providers:
                if name not in fallback_chain:
                    fallback_chain.append(name)

        # Try each provider in order
        last_error = None
        for provider_name in fallback_chain:
            provider = self.providers.get(provider_name)
            breaker = self.breakers.get(provider_name)
            if not provider:
                continue

            # Abort if total deadline exceeded
            if time.monotonic() > deadline:
                logger.warning("llm_request_timeout", elapsed_providers=len(fallback_chain))
                break

            # Circuit breaker check
            if breaker and not breaker.is_available():
                logger.info("circuit_breaker_skipped", provider=provider_name, state=breaker.state)
                continue

            # Retry loop per provider
            for attempt in range(1, max_retries + 1):
                try:
                    start_time = time.perf_counter()
                    response = provider.generate(messages, **kwargs)
                    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

                    # Record success
                    if breaker:
                        breaker.record_success()

                    if provider_name != target:
                        logger.info("fallback_used", target=target, actual=provider_name)

                    # Track token usage
                    token_info = self.token_tracker.track(messages, response, provider=provider_name)

                    logger.info(
                        "llm_call_success",
                        provider=provider_name,
                        elapsed_ms=elapsed_ms,
                        attempt=attempt,
                        input_tokens=token_info["input_tokens"],
                        output_tokens=token_info["output_tokens"],
                        cost_usd=token_info["estimated_cost_usd"],
                    )
                    return response

                except Exception as e:
                    last_error = e
                    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
                    logger.warning(
                        "llm_call_failed",
                        provider=provider_name,
                        attempt=attempt,
                        max_retries=max_retries,
                        elapsed_ms=elapsed_ms,
                        error=str(e),
                    )

                    # Don't retry on last attempt — fall through to next provider
                    if attempt == max_retries:
                        if breaker:
                            breaker.record_failure()
                        break

                    # Exponential backoff — but respect the deadline
                    backoff = 0.5 * attempt
                    if time.monotonic() + backoff > deadline:
                        logger.warning("llm_backoff_skipped_deadline", provider=provider_name)
                        if breaker:
                            breaker.record_failure()
                        break
                    time.sleep(backoff)

        if pinned_provider:
            raise RuntimeError(f"Pinned evaluation provider '{pinned_provider}' failed: {last_error}")
        raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")

    def get_provider_status(self) -> dict[str, dict]:
        """Check health and circuit breaker state of all registered providers."""
        status = {}
        for name, provider in self.providers.items():
            breaker = self.breakers.get(name)
            status[name] = {
                "healthy": provider.health_check(),
                "circuit_breaker": breaker.state if breaker else "unknown",
            }
        return status

    def list_providers(self) -> list[str]:
        """List all available provider names."""
        return list(self.providers.keys())

    def get_token_usage(self) -> dict:
        """Get aggregate token usage and cost stats."""
        return self.token_tracker.get_totals()

    async def agenerate(
        self,
        messages: list[dict],
        model_preference: str = "default",
        **kwargs,
    ) -> str:
        """
        Async version of generate().

        If the target provider supports native async (e.g. Groq), uses it directly
        for zero thread-pool overhead. Otherwise falls back to asyncio.to_thread().
        """
        import asyncio

        # Resolve target provider
        pinned_provider = kwargs.get("pinned_provider")
        if pinned_provider:
            if pinned_provider not in self.providers:
                raise RuntimeError(
                    f"Pinned provider '{pinned_provider}' is not configured/available. "
                    f"Configured providers: {list(self.providers.keys())}"
                )
            target = pinned_provider
        else:
            target = self.routing.get(model_preference, self.default_provider)

        provider = self.providers.get(target)
        breaker = self.breakers.get(target)

        # In strict eval mode, do not proceed if breaker is open
        if pinned_provider and breaker and not breaker.is_available():
            raise RuntimeError(f"Pinned evaluation provider '{pinned_provider}' circuit breaker is OPEN.")

        # Try native async on Groq first
        if provider and hasattr(provider, 'agenerate') and (not breaker or breaker.is_available()):
            try:
                import time
                start = time.perf_counter()

                # For 'fast' routing, use the fast model
                if model_preference == "fast" and hasattr(provider, 'fast_model'):
                    kwargs["model_override"] = provider.fast_model

                response = await provider.agenerate(messages, **kwargs)
                elapsed_ms = round((time.perf_counter() - start) * 1000, 2)

                if breaker:
                    breaker.record_success()

                # Track tokens
                token_info = self.token_tracker.track(messages, response, provider=target)
                logger.info(
                    "async_llm_call_success",
                    provider=target,
                    elapsed_ms=elapsed_ms,
                    input_tokens=token_info["input_tokens"],
                    output_tokens=token_info["output_tokens"],
                    native_async=True,
                )
                return response

            except Exception as e:
                logger.warning("async_native_failed", provider=target, error=str(e))
                if breaker:
                    breaker.record_failure()
                if pinned_provider:
                    raise RuntimeError(f"Pinned evaluation provider '{pinned_provider}' failed: {str(e)}") from e
                # Fall through to thread-wrapped sync

        # Fallback: thread-wrapped sync generate() with full fallback chain
        return await asyncio.to_thread(
            self.generate,
            messages=messages,
            model_preference=model_preference,
            **kwargs,
        )

    async def astream_tokens(
        self,
        messages: list[dict],
        model: str = None,
        **kwargs,
    ):
        """
        Async generator that yields tokens as they stream from the LLM.

        Priority order:
        1. Groq native streaming (fastest — LPU hardware)
        2. OpenAI native streaming
        3. Fallback: generate full response + yield in word chunks
        """
        # 1. Try Groq native streaming first
        if "groq" in self.providers:
            try:
                provider = self.providers["groq"]
                async for token in provider.astream(messages, **kwargs):
                    yield token
                return
            except Exception as e:
                logger.warning("groq_streaming_failed", error=str(e))

        # 2. Try OpenAI native streaming
        if "openai" in self.providers:
            try:
                from openai import AsyncOpenAI
                provider = self.providers["openai"]
                api_key = getattr(provider, "api_key", None)
                if api_key:
                    client = AsyncOpenAI(api_key=api_key)
                    stream = await client.chat.completions.create(
                        model=model or "gpt-4o-mini",
                        messages=messages,
                        stream=True,
                        **kwargs,
                    )
                    async for chunk in stream:
                        if chunk.choices[0].delta.content:
                            yield chunk.choices[0].delta.content
                    return
            except ImportError:
                logger.debug("openai_async_not_available", hint="pip install openai>=1.0")
            except Exception as e:
                logger.warning("openai_streaming_failed", error=str(e))

        # 3. Fallback: generate full response, yield in word chunks
        import asyncio
        response = await self.agenerate(messages, **kwargs)
        for word in response.split(" "):
            yield word + " "
            await asyncio.sleep(0.01)  # Small delay for streaming UX


# Alias for convenience
LLMRouter = ModelRouter
