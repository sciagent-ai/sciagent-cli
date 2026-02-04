"""
LLM Interface - Model-agnostic LLM calls via litellm
"""
# Suppress pydantic serialization warnings BEFORE any imports
# These warnings occur when litellm's pydantic models serialize responses
# and fields don't match expected schema (e.g., thinking_blocks for Claude)
import warnings
import os

# Method 1: Comprehensive warning filters
# Filter by message content (most reliable for runtime warnings)
warnings.filterwarnings("ignore", message=".*Pydantic serializer warnings.*")
warnings.filterwarnings("ignore", message=".*PydanticSerializationUnexpectedValue.*")
warnings.filterwarnings("ignore", message=".*Expected.*fields but got.*")
warnings.filterwarnings("ignore", message=".*serialized value may not be as expected.*")

# Filter by category and module
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic.main")
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic.*")

# Method 2: Monkey-patch pydantic's warning mechanism (backup)
# This runs after pydantic loads but before serialization
def _suppress_pydantic_serializer_warnings():
    """Suppress pydantic's internal serializer warnings at the source."""
    try:
        import pydantic
        if hasattr(pydantic, 'warnings'):
            pydantic.warnings.filterwarnings = lambda *args, **kwargs: None
    except Exception:
        pass

import json
from typing import List, Dict, Any, Optional, Generator
from dataclasses import dataclass, field

from .defaults import DEFAULT_MODEL

try:
    import litellm
    from litellm import completion
    from litellm.caching import Cache
    LITELLM_AVAILABLE = True

    # Apply pydantic warning suppression after litellm loads
    _suppress_pydantic_serializer_warnings()

    # Initialize LiteLLM's built-in response caching (cross-provider)
    # This caches entire LLM responses based on input hash
    # Options: "local" (in-memory), "redis", "disk"
    litellm.cache = Cache(
        type="local",           # In-memory cache (use "redis" or "disk" for persistence)
        ttl=3600,               # Cache TTL in seconds (1 hour)
    )
    litellm.enable_cache = True

except ImportError:
    LITELLM_AVAILABLE = False
    print("Warning: litellm not installed. Run: pip install litellm")


@dataclass
class Message:
    """Represents a message in the conversation"""
    role: str  # "system", "user", "assistant", "tool"
    content: str
    tool_calls: Optional[List[Dict]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None  # For tool messages
    
    def to_dict(self) -> Dict:
        msg = {"role": self.role, "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        if self.name:
            msg["name"] = self.name
        return msg
    
    @classmethod
    def from_dict(cls, d: Dict) -> "Message":
        return cls(
            role=d["role"],
            content=d.get("content", ""),
            tool_calls=d.get("tool_calls"),
            tool_call_id=d.get("tool_call_id"),
            name=d.get("name")
        )


@dataclass
class ToolCall:
    """Represents a tool call from the LLM"""
    id: str
    name: str
    arguments: Dict[str, Any]
    
    @classmethod
    def from_response(cls, tool_call: Dict) -> "ToolCall":
        """Parse tool call from LLM response"""
        args = tool_call.get("function", {}).get("arguments", "{}")
        if isinstance(args, str):
            args = json.loads(args)
        return cls(
            id=tool_call.get("id", ""),
            name=tool_call.get("function", {}).get("name", ""),
            arguments=args
        )


@dataclass
class LLMResponse:
    """Structured response from LLM"""
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Dict[str, int] = field(default_factory=dict)
    cache_info: Dict[str, Any] = field(default_factory=dict)  # Cache metrics

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def cache_hit(self) -> bool:
        """Check if this response used cached tokens (Anthropic prompt caching)"""
        return self.cache_info.get("cache_read_input_tokens", 0) > 0

    @property
    def tokens_cached(self) -> int:
        """Number of tokens read from cache"""
        return self.cache_info.get("cache_read_input_tokens", 0)

    @property
    def tokens_written_to_cache(self) -> int:
        """Number of tokens written to cache for future use"""
        return self.cache_info.get("cache_creation_input_tokens", 0)


class LLMClient:
    """
    Model-agnostic LLM client using litellm
    
    Supports: OpenAI, Anthropic, Google, Mistral, local models, etc.
    """
    
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        
        # Set API key if provided
        if api_key:
            # litellm auto-detects provider from model name
            if "anthropic" in model.lower() or "claude" in model.lower():
                os.environ["ANTHROPIC_API_KEY"] = api_key
            elif "gpt" in model.lower() or "openai" in model.lower():
                os.environ["OPENAI_API_KEY"] = api_key
            elif "gemini" in model.lower():
                os.environ["GEMINI_API_KEY"] = api_key
        
        if base_url:
            self.base_url = base_url
        else:
            self.base_url = None
            
        # Configure litellm
        if LITELLM_AVAILABLE:
            litellm.drop_params = True  # Ignore unsupported params
            
    def _is_anthropic_model(self) -> bool:
        """Check if current model is an Anthropic model"""
        model_lower = self.model.lower()
        return "anthropic" in model_lower or "claude" in model_lower

    def _format_messages_with_prompt_caching(self, messages: List[Dict]) -> List[Dict]:
        """
        Format messages for Anthropic prompt caching.

        Adds cache_control to system messages and long user messages to enable
        Anthropic's prompt caching feature. This can reduce costs by ~90% on
        cached tokens and improve response latency.

        Only applies to Anthropic models.
        """
        if not self._is_anthropic_model():
            return messages

        formatted = []
        for msg in messages:
            msg_copy = msg.copy()

            # Add cache_control to system messages (typically long, static prompts)
            if msg_copy["role"] == "system":
                content = msg_copy["content"]
                # Convert string content to structured format with cache_control
                if isinstance(content, str):
                    msg_copy["content"] = [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"}
                        }
                    ]
                elif isinstance(content, list):
                    # Already structured, add cache_control to last block
                    for i, block in enumerate(content):
                        if isinstance(block, dict) and block.get("type") == "text":
                            # Add cache_control to the last text block
                            if i == len(content) - 1:
                                block["cache_control"] = {"type": "ephemeral"}
                    msg_copy["content"] = content

            # Optionally cache long user messages (e.g., large code files, documents)
            elif msg_copy["role"] == "user":
                content = msg_copy["content"]
                # Cache user messages over 2000 chars (significant context)
                if isinstance(content, str) and len(content) > 2000:
                    msg_copy["content"] = [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"}
                        }
                    ]

            formatted.append(msg_copy)

        return formatted

    def _format_tools(self, tools: List[Dict]) -> List[Dict]:
        """Format tools for the LLM API"""
        formatted = []
        for tool in tools:
            formatted.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}})
                }
            })
        return formatted
    
    def chat(
        self,
        messages: List[Message],
        tools: Optional[List[Dict]] = None,
        tool_choice: str = "auto",
        **kwargs
    ) -> LLMResponse:
        """
        Send messages to LLM and get response
        
        Args:
            messages: List of Message objects
            tools: List of tool definitions
            tool_choice: "auto", "none", or {"type": "function", "function": {"name": "..."}}
            
        Returns:
            LLMResponse with content and/or tool calls
        """
        if not LITELLM_AVAILABLE:
            raise RuntimeError("litellm not installed")

        # Convert messages to dicts
        msg_dicts = [m.to_dict() for m in messages]

        # Apply Anthropic prompt caching if applicable
        msg_dicts = self._format_messages_with_prompt_caching(msg_dicts)

        # Prepare kwargs
        call_kwargs = {
            "model": self.model,
            "messages": msg_dicts,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            **kwargs
        }
        
        if self.base_url:
            call_kwargs["base_url"] = self.base_url
        
        # Add tools if provided
        if tools:
            call_kwargs["tools"] = self._format_tools(tools)
            call_kwargs["tool_choice"] = tool_choice
        
        # Make the call
        response = completion(**call_kwargs)
        
        # Parse response
        choice = response.choices[0]
        message = choice.message
        
        # Extract tool calls if present
        tool_calls = []
        if hasattr(message, 'tool_calls') and message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append(ToolCall.from_response(tc.model_dump()))
        
        # Extract usage info
        usage = {
            "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
            "completion_tokens": response.usage.completion_tokens if response.usage else 0,
        }

        # Extract Anthropic prompt caching metrics if available
        cache_info = {}
        if response.usage:
            # Anthropic returns these fields for prompt caching
            if hasattr(response.usage, "cache_read_input_tokens"):
                cache_info["cache_read_input_tokens"] = response.usage.cache_read_input_tokens or 0
            if hasattr(response.usage, "cache_creation_input_tokens"):
                cache_info["cache_creation_input_tokens"] = response.usage.cache_creation_input_tokens or 0
            # Also check _hidden_params for litellm's cache info
            if hasattr(response, "_hidden_params"):
                hidden = response._hidden_params or {}
                if hidden.get("cache_hit"):
                    cache_info["litellm_cache_hit"] = True

        return LLMResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            cache_info=cache_info
        )
    
    def chat_stream(
        self,
        messages: List[Message],
        tools: Optional[List[Dict]] = None,
        **kwargs
    ) -> Generator[str, None, LLMResponse]:
        """
        Stream response from LLM

        Yields content chunks, returns final LLMResponse
        """
        if not LITELLM_AVAILABLE:
            raise RuntimeError("litellm not installed")

        msg_dicts = [m.to_dict() for m in messages]

        # Apply Anthropic prompt caching if applicable
        msg_dicts = self._format_messages_with_prompt_caching(msg_dicts)

        call_kwargs = {
            "model": self.model,
            "messages": msg_dicts,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
            **kwargs
        }

        if tools:
            call_kwargs["tools"] = self._format_tools(tools)

        response = completion(**call_kwargs)

        full_content = ""
        tool_calls = []

        for chunk in response:
            if chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                full_content += content
                yield content

        return LLMResponse(
            content=full_content,
            tool_calls=tool_calls,
            finish_reason="stop"
        )


# Convenience function for simple calls
def ask(
    prompt: str,
    model: str = DEFAULT_MODEL,
    system: Optional[str] = None,
    **kwargs
) -> str:
    """Simple one-shot LLM call"""
    client = LLMClient(model=model, **kwargs)
    messages = []
    if system:
        messages.append(Message(role="system", content=system))
    messages.append(Message(role="user", content=prompt))
    response = client.chat(messages)
    return response.content


def configure_cache(
    cache_type: str = "local",
    ttl: int = 3600,
    redis_host: Optional[str] = None,
    redis_port: int = 6379,
    disk_cache_dir: Optional[str] = None,
    enabled: bool = True
) -> None:
    """
    Configure LiteLLM's response caching at runtime.

    Args:
        cache_type: "local" (in-memory), "redis", or "disk"
        ttl: Cache time-to-live in seconds (default: 1 hour)
        redis_host: Redis server host (required if cache_type="redis")
        redis_port: Redis server port (default: 6379)
        disk_cache_dir: Directory for disk cache (required if cache_type="disk")
        enabled: Whether to enable caching (default: True)

    Examples:
        # Use in-memory cache (default)
        configure_cache(cache_type="local", ttl=3600)

        # Use Redis for persistent caching
        configure_cache(cache_type="redis", redis_host="localhost", ttl=7200)

        # Use disk cache
        configure_cache(cache_type="disk", disk_cache_dir="/tmp/llm_cache")

        # Disable caching
        configure_cache(enabled=False)
    """
    if not LITELLM_AVAILABLE:
        print("Warning: litellm not installed, caching not available")
        return

    litellm.enable_cache = enabled

    if not enabled:
        litellm.cache = None
        return

    cache_kwargs = {"type": cache_type, "ttl": ttl}

    if cache_type == "redis":
        if not redis_host:
            raise ValueError("redis_host required for Redis cache")
        cache_kwargs["host"] = redis_host
        cache_kwargs["port"] = redis_port

    elif cache_type == "disk":
        if not disk_cache_dir:
            raise ValueError("disk_cache_dir required for disk cache")
        cache_kwargs["disk_cache_dir"] = disk_cache_dir

    litellm.cache = Cache(**cache_kwargs)
