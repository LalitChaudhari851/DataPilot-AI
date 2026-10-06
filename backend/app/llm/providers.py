"""
LLM Providers — Concrete implementations for each supported model provider.
"""

import structlog
from huggingface_hub import InferenceClient

from app.llm.base import BaseLLMProvider

logger = structlog.get_logger()


class HuggingFaceProvider(BaseLLMProvider):
    """HuggingFace Inference API provider (Qwen, Mistral, etc.)"""

    def __init__(self, api_token: str, model: str = "Qwen/Qwen2.5-Coder-32B-Instruct"):
        self.api_token = api_token
        self.model = model
        self.client = InferenceClient(token=api_token, timeout=30.0)

    def generate(self, messages: list[dict], **kwargs) -> str:
        max_tokens = kwargs.get("max_tokens", 1024)
        temperature = kwargs.get("temperature", 0.1)
        seed = kwargs.get("seed")
        top_p = kwargs.get("top_p")

        call_kwargs = {
            "messages": messages,
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if seed is not None:
            call_kwargs["seed"] = seed
        if top_p is not None:
            call_kwargs["top_p"] = top_p

        response = self.client.chat_completion(**call_kwargs)
        return response.choices[0].message.content

    def health_check(self) -> bool:
        try:
            self.client.chat_completion(
                messages=[{"role": "user", "content": "ping"}],
                model=self.model,
                max_tokens=5,
            )
            return True
        except Exception:
            return False

    @property
    def name(self) -> str:
        return "huggingface"


class OpenAIProvider(BaseLLMProvider):
    """OpenAI API provider (GPT-4, GPT-3.5, etc.)"""

    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        self.api_key = api_key
        self.model = model
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key=self.api_key)
            except ImportError:
                raise ImportError("openai package not installed. Run: pip install openai")
        return self._client

    def generate(self, messages: list[dict], **kwargs) -> str:
        client = self._get_client()
        max_tokens = kwargs.get("max_tokens", 1024)
        temperature = kwargs.get("temperature", 0.1)
        seed = kwargs.get("seed")
        top_p = kwargs.get("top_p")

        call_kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if seed is not None:
            call_kwargs["seed"] = seed
        if top_p is not None:
            call_kwargs["top_p"] = top_p

        response = client.chat.completions.create(**call_kwargs)
        return response.choices[0].message.content

    def health_check(self) -> bool:
        try:
            client = self._get_client()
            client.models.list()
            return True
        except Exception:
            return False

    @property
    def name(self) -> str:
        return "openai"


class AnthropicProvider(BaseLLMProvider):
    """Anthropic API provider (Claude models)"""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self.api_key = api_key
        self.model = model
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.api_key)
            except ImportError:
                raise ImportError("anthropic package not installed. Run: pip install anthropic")
        return self._client

    def generate(self, messages: list[dict], **kwargs) -> str:
        client = self._get_client()
        max_tokens = kwargs.get("max_tokens", 1024)

        # Anthropic format: separate system from user messages
        system_msg = ""
        user_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_msg = msg["content"]
            else:
                user_messages.append(msg)

        response = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system_msg,
            messages=user_messages,
        )
        return response.content[0].text

    def health_check(self) -> bool:
        try:
            self._get_client()
            return True
        except Exception:
            return False

    @property
    def name(self) -> str:
        return "anthropic"


class OllamaProvider(BaseLLMProvider):
    """Ollama local model provider"""

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llama3"):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def generate(self, messages: list[dict], **kwargs) -> str:
        import requests

        options = {
            "temperature": kwargs.get("temperature", 0.1),
            "num_predict": kwargs.get("max_tokens", 1024),
        }
        if kwargs.get("seed") is not None:
            options["seed"] = kwargs.get("seed")
        if kwargs.get("top_p") is not None:
            options["top_p"] = kwargs.get("top_p")

        response = requests.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "stream": False,
                "options": options,
            },
            timeout=60,
        )
        response.raise_for_status()
        return response.json()["message"]["content"]

    def health_check(self) -> bool:
        try:
            import requests
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    @property
    def name(self) -> str:
        return "ollama"


class GroqProvider(BaseLLMProvider):
    """
    Groq LPU Inference provider — ultra-low-latency LLM inference.

    Uses the OpenAI-compatible SDK pointed at Groq's API endpoint.
    Supports both sync and native async generation + streaming.

    Models:
    - llama-3.3-70b-versatile (primary — best accuracy for SQL)
    - llama-3.1-8b-instant    (fast — intent classification, simple tasks)
    """

    def __init__(
        self,
        api_key: str,
        model: str = "llama-3.3-70b-versatile",
        fast_model: str = "llama-3.1-8b-instant",
        base_url: str = "https://api.groq.com/openai/v1",
    ):
        self.api_key = api_key
        self.model = model
        self.fast_model = fast_model
        self.base_url = base_url
        self._sync_client = None
        self._async_client = None

    def _get_sync_client(self):
        """Lazy-init sync OpenAI client pointed at Groq."""
        if self._sync_client is None:
            try:
                from openai import OpenAI
                self._sync_client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=30.0,
                    max_retries=2,
                )
            except ImportError:
                raise ImportError("openai package required for Groq. Run: pip install openai>=1.0")
        return self._sync_client

    def _get_async_client(self):
        """Lazy-init async OpenAI client pointed at Groq."""
        if self._async_client is None:
            try:
                from openai import AsyncOpenAI
                self._async_client = AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=30.0,
                    max_retries=2,
                )
            except ImportError:
                raise ImportError("openai package required for Groq. Run: pip install openai>=1.0")
        return self._async_client

    def generate(self, messages: list[dict], **kwargs) -> str:
        """Synchronous generation via Groq LPU."""
        import time
        client = self._get_sync_client()
        model = kwargs.pop("model_override", self.model)
        max_tokens = kwargs.get("max_tokens", 1024)
        temperature = kwargs.get("temperature", 0.1)
        seed = kwargs.get("seed")
        top_p = kwargs.get("top_p")

        call_kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if seed is not None:
            call_kwargs["seed"] = seed
        if top_p is not None:
            call_kwargs["top_p"] = top_p

        start = time.perf_counter()
        response = client.chat.completions.create(**call_kwargs)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)

        content = response.choices[0].message.content

        # Extract native token usage from Groq response
        usage = getattr(response, "usage", None)
        logger.info(
            "groq_request_completed",
            model=model,
            latency_ms=elapsed_ms,
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        )

        return content

    async def agenerate(self, messages: list[dict], **kwargs) -> str:
        """
        True async generation — uses httpx under the hood via openai AsyncClient.
        No thread pool overhead.
        """
        import time
        client = self._get_async_client()
        model = kwargs.pop("model_override", self.model)
        max_tokens = kwargs.get("max_tokens", 1024)
        temperature = kwargs.get("temperature", 0.1)
        seed = kwargs.get("seed")
        top_p = kwargs.get("top_p")

        call_kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if seed is not None:
            call_kwargs["seed"] = seed
        if top_p is not None:
            call_kwargs["top_p"] = top_p

        start = time.perf_counter()
        response = await client.chat.completions.create(**call_kwargs)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)

        content = response.choices[0].message.content

        usage = getattr(response, "usage", None)
        logger.info(
            "groq_async_completed",
            model=model,
            latency_ms=elapsed_ms,
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        )

        return content

    async def astream(self, messages: list[dict], **kwargs):
        """
        True async streaming — yields tokens as they arrive from Groq LPU.
        Uses native OpenAI streaming protocol.
        """
        client = self._get_async_client()
        model = kwargs.pop("model_override", self.model)
        max_tokens = kwargs.get("max_tokens", 1024)
        temperature = kwargs.get("temperature", 0.1)
        seed = kwargs.get("seed")
        top_p = kwargs.get("top_p")

        call_kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if seed is not None:
            call_kwargs["seed"] = seed
        if top_p is not None:
            call_kwargs["top_p"] = top_p

        stream = await client.chat.completions.create(**call_kwargs)

        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def health_check(self) -> bool:
        """Lightweight health check — verifies API key and connectivity."""
        try:
            client = self._get_sync_client()
            client.models.list()
            return True
        except Exception:
            return False

    @property
    def name(self) -> str:
        return "groq"

