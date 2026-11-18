import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    supabase_url: str
    public_key: str
    secret_key: str
    openai_key: str
    model: str
    max_output_tokens: int
    context_input_tokens: int = 16000
    context_window_tokens: int = 128000
    max_model_steps: int = 8
    max_tool_calls: int = 8
    summary_output_tokens: int = 1500

    @classmethod
    def from_env(cls) -> "Config":
        def integer(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, str(default)))
            except ValueError:
                return 0

        return cls(
            supabase_url=os.environ.get("SUPABASE_URL", "").strip().rstrip("/"),
            public_key=os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip(),
            secret_key=os.environ.get("SUPABASE_SECRET_KEY", "").strip(),
            openai_key=os.environ.get("OPENAI_API_KEY", "").strip(),
            model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini").strip(),
            max_output_tokens=integer("MAX_OUTPUT_TOKENS", 2048),
            context_input_tokens=integer("CONTEXT_INPUT_TOKENS", 16000),
            context_window_tokens=integer("CONTEXT_WINDOW_TOKENS", 128000),
            max_model_steps=integer("MAX_MODEL_STEPS", 8),
            max_tool_calls=integer("MAX_TOOL_CALLS", 8),
            summary_output_tokens=integer("SUMMARY_OUTPUT_TOKENS", 1500),
        )

    @property
    def auth_configured(self) -> bool:
        try:
            url = urlsplit(self.supabase_url)
            valid_url = bool(
                url.hostname and not url.username and not url.password
                and not url.query and not url.fragment
                and (url.scheme == "https" or (
                    url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
                ))
            )
            return valid_url and bool(self.public_key)
        except ValueError:
            return False

    @property
    def database_configured(self) -> bool:
        return self.auth_configured and bool(self.secret_key)

    @property
    def chat_configured(self) -> bool:
        return bool(
            self.database_configured and self.openai_key
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", self.model)
            and 1 <= self.max_output_tokens <= 16384
            and 1024 <= self.context_input_tokens < self.context_window_tokens <= 1000000
            and self.context_input_tokens + self.max_output_tokens <= self.context_window_tokens
            and 128 <= self.summary_output_tokens < self.context_input_tokens
            and 1 <= self.max_model_steps <= 32
            and 1 <= self.max_tool_calls <= 32
        )

