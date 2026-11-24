from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://bench:bench@localhost:5432/bench"

    # Models are "provider:model" specs, e.g. "anthropic:claude-sonnet-5-5",
    # "groq:openai/gpt-oss-120b", "gemini:gemini-3.5-flash", "sarvam:glm5.3-flash", "openai:gpt-6-sol". A bare model name uses llm_provider.
    llm_provider: str = "anthropic"
    agent_model: str = "anthropic:claude-sonnet-5-5"
    user_model: str = "anthropic:claude-sonnet-5-5"
    judge_model: str = "anthropic:claude-sonnet-5-5"
    agent_temperature: float | None = 0.0  # "none" to omit (for models that reject it)
    user_temperature: float | None = 0.7

    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    gemini_api_key: str | None = None
    sarvam_api_key: str | None = None
    sarvam_base_url: str = "https://api.sarvam.ai/v2"
    sarvam_reasoning_effort: str = ""  # "" = each model's default; none | low | medium | high
    sarvam_max_tokens: int = 4096  # thinking counts against this budget
    inr_per_usd: float = 94.0  # to convert INR-priced models (Sarvam) into USD costs
    google_api_key: str | None = None  # accepted as an alias for GEMINI_API_KEY

    max_concurrency: int = 4  # episodes run in parallel

    # Rate limiting per provider (0 = off). Groq free tier: GROQ_RPM=30, GROQ_TPM=8000.
    # Quotas are tracked per model. Concurrency caps simultaneous requests to a provider.
    groq_rpm: int = 0
    groq_tpm: int = 0
    groq_max_concurrency: int = 2
    gemini_rpm: int = 0
    gemini_tpm: int = 0
    gemini_max_concurrency: int = 4
    sarvam_rpm: int = 0
    sarvam_tpm: int = 0
    sarvam_max_concurrency: int = 4
    openai_rpm: int = 0
    openai_tpm: int = 0
    openai_max_concurrency: int = 0
    rate_limit_max_wait_s: float = 600  # give up on a call after this much total waiting
    max_user_turns: int = 30
    max_agent_steps_per_turn: int = 12
    auto_analyze: bool = True  # run the failure-analysis judge after each failed episode
    # Score process as well as outcome: a write without authentication, on another customer's order, or without the
    # customer's explicit confirmation fails the episode even if the final DB state matches gold.
    score_process: bool = True
    # Hallucination detection (app/grounding): the deterministic claim check runs on every episode; the LLM
    # verifier (one call per episode) runs when GROUNDING_MODEL is set and its provider has a key ("none" = off).
    grounding_check: bool = True
    # Off by default until a verifier passes validation: in a 30-episode trial, sarvam:sarvam-105b (thinking off)
    # flagged mostly correct claims and broke its output format on 11 of 30 episodes.
    grounding_model: str = "none"
    # Sarvam thinking level for the verifier only. "low" used 6K+ tokens of thinking per episode and often ran out of
    # budget before answering, so it's off by default.
    grounding_reasoning_effort: str = "none"
    grounding_max_tokens: int = 8192
    # Second pass: re-check each flagged sentence with a narrower keep/drop question (cuts false flags).
    grounding_second_pass: bool = True
    # "low" kept 24/26 real hand-labelled hallucinations vs 13/26 with "none" (2026-11-23); one sentence in, two lines
    # out, so thinking fits in the budget here (unlike the first pass)
    grounding_second_pass_effort: str = "low"
    # Verifier used *at runtime* by the grounded_llm agent variant (independent of GROUNDING_MODEL, which only
    # controls the offline post-episode check).
    grounded_verifier_model: str = "sarvam:sarvam-105b"

    # Red-teaming (app/redteam): who writes scenarios, and who checks they're solvable. The attacker shouldn't be an
    # agent under attack (default targets: glm5.3-flash, deepseekv4.1-flash). The referee only judges whether the gold
    # outcome is the unique policy-correct one; it's DeepSeek Flash by the user's choice, which shares a model with a
    # target, so its verdicts are backed by deterministic checks and by any agent actually solving the scenario.
    redteam_attacker_model: str = "sarvam:glm5.3"
    redteam_referee_model: str = "sarvam:deepseekv4.1-flash-1017"
    redteam_max_tokens: int = 12000
    redteam_attacker_effort: str = "low"  # GLM 5.3's default thinking exhausted the budget (0/6 calls parsed)

    cors_origins: str = "http://localhost:3000"

    @property
    def gemini_key(self) -> str | None:
        return self.gemini_api_key or self.google_api_key

    @field_validator("agent_temperature", "user_temperature", mode="before")
    @classmethod
    def _none_temp(cls, v):
        if isinstance(v, str) and v.strip().lower() in ("", "none", "null"):
            return None
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
