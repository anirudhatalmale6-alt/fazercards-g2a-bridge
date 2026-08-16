"""Runtime configuration.

Every secret is read from the environment (or a .env file that is never
committed).  Nothing here has a credential as a default value -- if a secret is
missing the process refuses to talk to that API instead of falling back to
something wrong.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # ---------------------------------------------------------------- general
    environment: str = "development"
    log_level: str = "INFO"
    # Anything written to the api_calls table is scrubbed of these header names.
    secret_headers: tuple[str, ...] = (
        "x-api-key",
        "authorization",
        "proxy-authorization",
    )

    # --------------------------------------------------------------- database
    database_url: str = "postgresql+psycopg://bridge:bridge@db:5432/bridge"
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # ------------------------------------------------------------ FazerCards
    fazercards_base_url: str = "https://api.fzr.cards"
    fazercards_api_key: SecretStr | None = None
    # FazerCards publishes no hard number; 5 req/s is polite and well inside it.
    fazercards_rate_per_second: float = 5.0
    fazercards_burst: int = 10

    # -------------------------------------------------------------------- G2A
    g2a_base_url: str = "https://api.g2a.com"
    g2a_client_id: SecretStr | None = None
    g2a_client_secret: SecretStr | None = None
    g2a_rate_per_second: float = 4.0
    g2a_burst: int = 8
    # Documented in the G2A changelog (3.367.0): price changes are capped per
    # product per hour.  We budget one below the cap so a manual change from the
    # panel does not push us over it.
    g2a_price_changes_per_product_per_hour: int = 5
    g2a_price_change_safety_margin: int = 1

    # ------------------------------------------------------------------ retry
    retry_max_attempts: int = 6
    retry_base_delay_seconds: float = 0.5
    retry_max_delay_seconds: float = 60.0
    retry_jitter: float = 0.25
    http_timeout_seconds: float = 30.0

    # ---------------------------------------------------------------- pricing
    # G2A seller account is settled in EUR, FazerCards quotes USD.
    store_currency: str = "EUR"
    supplier_currency: str = "USD"
    fx_rate_usd_eur: float | None = None  # None -> fetch from fx_provider
    fx_provider_url: str = "https://api.frankfurter.app/latest?from=USD&to=EUR"
    fx_max_age_hours: int = 24
    default_markup_percent: float = 15.0
    price_rounding: str = "0.01"
    # Never publish an offer below this, whatever the markup maths says.
    min_offer_price: float = 0.30

    # --------------------------------------------------------------- matching
    # Above `auto` we map without asking; between `review` and `auto` the pair
    # lands in the review queue; below `review` it is discarded.
    match_auto_accept_score: float = 0.92
    match_review_score: float = 0.70
    match_max_candidates: int = 10

    # ------------------------------------------------------------------- sync
    catalog_sync_minutes: int = 60
    price_stock_sync_minutes: int = 12
    order_poll_seconds: int = 30
    # A supplier item that vanishes from the feed is deactivated, not deleted,
    # so a flaky supplier page cannot wipe the catalogue.
    deactivate_missing_after_runs: int = 2

    @field_validator("price_rounding")
    @classmethod
    def _valid_rounding(cls, v: str) -> str:
        if v not in {"0.01", "0.05", "0.10", "0.99"}:
            raise ValueError("price_rounding must be one of 0.01, 0.05, 0.10, 0.99")
        return v

    # -------------------------------------------------------------- accessors
    def require_fazercards_key(self) -> str:
        if not self.fazercards_api_key:
            raise RuntimeError(
                "FAZERCARDS_API_KEY is not set -- refusing to call the supplier API"
            )
        return self.fazercards_api_key.get_secret_value()

    def require_g2a_credentials(self) -> tuple[str, str]:
        if not self.g2a_client_id or not self.g2a_client_secret:
            raise RuntimeError(
                "G2A_CLIENT_ID / G2A_CLIENT_SECRET are not set -- refusing to call the store API"
            )
        return (
            self.g2a_client_id.get_secret_value(),
            self.g2a_client_secret.get_secret_value(),
        )

    @property
    def effective_price_change_budget(self) -> int:
        return max(
            1,
            self.g2a_price_changes_per_product_per_hour
            - self.g2a_price_change_safety_margin,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """Test hook -- drops the cached Settings so env changes take effect."""
    get_settings.cache_clear()
    return get_settings()
