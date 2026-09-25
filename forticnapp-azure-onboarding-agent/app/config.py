"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip().removeprefix("export ").strip()
        # skip comments, blanks and TOML section headers like [default]
        if not line or line.startswith(("#", "[")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _load_lacework_toml() -> None:
    """Fallback: the lacework CLI profile (~/.lacework.toml, profile LW_PROFILE or 'default')."""
    path = Path(os.getenv("LW_CONFIG", str(Path.home() / ".lacework.toml")))
    if not path.is_file():
        return
    import tomllib
    try:
        profile = tomllib.loads(path.read_text()).get(os.getenv("LW_PROFILE", "default"), {})
    except (tomllib.TOMLDecodeError, OSError):
        return
    for key in ("account", "subaccount", "api_key", "api_secret"):
        if profile.get(key):
            os.environ.setdefault(key, str(profile[key]))


ROOT = Path(__file__).resolve().parent.parent
_load_dotenv(ROOT / ".env")
_load_lacework_toml()


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default


@dataclass(frozen=True)
class Settings:
    # --- Bifrost LLM gateway -------------------------------------------------
    # LLM_API=anthropic -> POST {base}/v1/messages      (Bifrost /anthropic route)
    # LLM_API=openai    -> POST {base}/v1/chat/completions
    backend: str = _env("BACKEND", default="bifrost").lower()   # label only: bifrost | llamacpp | vllm
    llm_api: str = _env("LLM_API", default="anthropic").lower()
    llm_base_url: str = _env("BIFROST_BASE_URL", "ANTHROPIC_BASE_URL",
                             default="https://bifrost.fabriclab.ca/anthropic").rstrip("/")
    llm_api_key: str = _env("BIFROST_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
    default_model: str = _env("DEFAULT_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                              default="qwen3.8-27b-anthropic")
    model_filter: str = _env("BIFROST_MODEL_FILTER")        # optional regex for the picker
    llm_temperature: float = float(_env("LLM_TEMPERATURE", default="0.2"))
    llm_max_tokens: int = int(_env("LLM_MAX_TOKENS", default="4096"))
    llm_timeout: float = float(_env("LLM_TIMEOUT", default="180"))
    max_agent_steps: int = int(_env("MAX_AGENT_STEPS", default="8"))

    # --- FortiCNAPP (Lacework) API v2 ----------------------------------------
    # Accepts lacework-CLI style keys (account / api_key / api_secret) or LW_* env vars.
    lw_account: str = _env("LW_ACCOUNT", "account")           # "demo" -> demo.lacework.net
    lw_subaccount: str = _env("LW_SUBACCOUNT", "subaccount")  # optional Account-Name header
    lw_api_key: str = _env("LW_API_KEY", "api_key")           # keyId, e.g. FORTINET_XXXX
    lw_api_secret: str = _env("LW_API_SECRET", "api_secret")  # secret (never sent to the LLM)

    # --- Behaviour -----------------------------------------------------------
    # Only env vars with this prefix can be referenced as secret_ref="env:<NAME>"
    secret_env_prefix: str = _env("SECRET_ENV_PREFIX", default="FCNAPP_SECRET_")
    allow_create: bool = _env("ALLOW_CREATE", default="true").lower() == "true"
    tool_result_max_chars: int = int(_env("TOOL_RESULT_MAX_CHARS", default="16000"))

    skill_path: Path = field(default=ROOT / "SKILL.md")
    preflight_script: Path = field(default=ROOT / "scripts" / "forticnapp-azure-preflight.sh")

    @property
    def lw_base_url(self) -> str:
        if self.lw_account.startswith("http://"):   # local mock / testing only
            return self.lw_account.rstrip("/")
        acct = self.lw_account.strip().removeprefix("https://").rstrip("/")
        if acct and "." not in acct:
            acct = f"{acct}.lacework.net"
        return f"https://{acct}" if acct else ""

    @property
    def fallback_models(self) -> list[str]:
        names = [self.default_model] + [os.getenv(n, "") for n in (
            "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL")]
        return list(dict.fromkeys(n for n in names if n))


settings = Settings()
