"""Runtime settings — env-driven, sensible offline defaults.

All external dependencies (Neo4j, Mongo) degrade gracefully: if unreachable or
unconfigured, the API serves snapshot/fallback values and stores feedback
in-memory so the service (and its tests) run with zero infra.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MHB_", env_file=".env", extra="ignore")

    # --- Neo4j (read-only KG queries) ---
    # bhgman KG: bolt://100.64.0.3:7687 (Tailscale) — see reference_neo4j_gds_vector_available
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4jpassword"
    neo4j_live: bool = False  # opt-in; default off → snapshot fallback (CI / offline)

    # --- MongoDB (feedback intake) ---
    mongo_uri: str = ""  # empty → in-memory store (no infra needed)
    mongo_db: str = "metahumotonic"
    mongo_feedback_collection: str = "web_feedback"
    # auto-expire stored feedback after N days (TTL index); 0 disables
    feedback_ttl_days: int = 365

    # --- KG stats cache (PROM16 C1: avoid count(n) full scan per request) ---
    stats_cache_ttl_seconds: int = 120

    # --- Redis (PROM16 C2: distributed rate limit) ---
    # empty → in-process limiter (survives single-replica but resets on restart)
    redis_url: str = ""

    # --- CORS ---
    # comma-separated origins allowed to call this API from the browser
    cors_origins: str = "https://metahumotonic.com,http://localhost:4321"

    # --- Feedback rate limit (per client IP) ---
    feedback_max_per_window: int = 5
    feedback_window_seconds: int = 600
    # PROM16 C2/A3S2: trust the reverse proxy's appended client IP (rightmost
    # X-Forwarded-For / CF-Connecting-IP) instead of the spoofable leftmost.
    trust_proxy: bool = True

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
