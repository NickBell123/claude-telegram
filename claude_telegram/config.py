import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    allowed_user_id: int
    default_chat_id: str
    push_token: str
    push_host: str = "127.0.0.1"
    push_port: int = 8787
    state_dir: str = os.path.expanduser("~/.claude-telegram")
    rate_limit_per_hour: int = 60
    edit_min_interval_s: float = 1.2
    max_telegram_chars: int = 3900

    @classmethod
    def from_env(cls) -> "Config":
        def req(name: str) -> str:
            v = os.environ.get(name)
            if not v:
                raise ConfigError(f"{name} is required")
            return v

        try:
            allowed = int(req("ALLOWED_USER_ID"))
        except ValueError as e:
            raise ConfigError(f"ALLOWED_USER_ID must be an integer: {e}")

        return cls(
            telegram_bot_token=req("TELEGRAM_BOT_TOKEN"),
            allowed_user_id=allowed,
            default_chat_id=req("DEFAULT_CHAT_ID"),
            push_token=req("PUSH_TOKEN"),
        )
