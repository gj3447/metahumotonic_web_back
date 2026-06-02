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

    # --- CORS ---
    # comma-separated origins allowed to call this API from the browser
    cors_origins: str = "https://metahumotonic.com,http://localhost:4321"

    # --- Feedback rate limit (per client IP) ---
    feedback_max_per_window: int = 5
    feedback_window_seconds: int = 600

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
