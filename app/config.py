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
    # comma-separated backup Bolt URIs, tried in order when neo4j_uri fails
    neo4j_fallback_uris: str = ""
    neo4j_user: str = "neo4j"
    # Credentials are never supplied by defaults; live mode must receive one
    # through MHB_NEO4J_PASSWORD or another deployment secret surface.
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    neo4j_live: bool = False  # opt-in; default off → snapshot fallback (CI / offline)

    # --- KG Cypher proxy (read/write split for external clients) ---
    # Community Neo4j has no RBAC, so read/write separation is enforced HERE:
    # the read key runs in a Neo4j READ transaction (server rejects writes),
    # the write key runs in a WRITE transaction. Empty key → that endpoint is
    # disabled (503), so the proxy is opt-in and stays off in CI/offline.
    # The write key is a superset: it is also accepted on /api/kg/read.
    kg_read_key: str = ""
    kg_write_key: str = ""
    # hard ceiling on rows returned per proxy query (defense against a client
    # draining a 90k-node label in one request)
    kg_proxy_max_rows: int = 1000

    # --- MongoDB (feedback intake) ---
    mongo_uri: str = ""  # empty → in-memory store (no infra needed)
    mongo_db: str = "metahumotonic"
    mongo_feedback_collection: str = "web_feedback"
    # auto-expire stored feedback after N days (TTL index); 0 disables
    feedback_ttl_days: int = 365
    # Production can reject a submission instead of falsely acknowledging an
    # in-memory fallback that would disappear on restart.
    feedback_require_durable: bool = False
    # Optional operator inbox key. Empty keeps GET /internal/feedback disabled.
    feedback_admin_key: str = ""

    # --- KG stats cache (PROM16 C1: avoid count(n) full scan per request) ---
    stats_cache_ttl_seconds: int = 120
    # research feeds are near-static (PROM-cycle outputs) → cache longer
    research_cache_ttl_seconds: int = 300
    # per-query server-side budget so a slow KG fails soft fast (not a 30s hang)
    kg_query_timeout_seconds: float = 10.0
    # bound the LRU caches so attacker-controlled cache keys (?cycle/?domain/
    # ?offset) can't grow memory without bound
    cache_max_entries: int = 512
    # hard ceiling on ?offset so deep pagination can't be used to mint unbounded
    # distinct cache keys / scan deep into a 12k+ label
    research_max_offset: int = 10000

    # --- Redis (PROM16 C2: distributed rate limit) ---
    # empty → in-process limiter (survives single-replica but resets on restart)
    redis_url: str = ""

    # --- Observability (PROM16 C6) ---
    metrics_enabled: bool = True       # expose Prometheus /metrics
    log_json: bool = True              # structured JSON logs (structlog)

    # --- Turnstile bot defense (PROM16 A3S3/A3S4, OQ3) ---
    # empty → disabled (current behavior). Set the Cloudflare Turnstile secret
    # to require + verify a cf-turnstile-response token on feedback.
    turnstile_secret: str = ""
    turnstile_hostname: str = "metahumotonic.com"
    turnstile_action: str = "feedback_submit"
    turnstile_fail_open: bool = False

    # --- CORS ---
    # comma-separated origins allowed to call this API from the browser
    cors_origins: str = "https://metahumotonic.com,http://localhost:4321"

    # --- Feedback rate limit (per client IP) ---
    feedback_max_per_window: int = 5
    feedback_window_seconds: int = 600
    # PROM16 C2/A3S2 + full-verify: only trust forwarded client-IP headers when
    # explicitly behind a trusted proxy. Default OFF so a directly-exposed
    # instance can't be IP-spoofed; the k8s deployment sets MHB_TRUST_PROXY=true.
    trust_proxy: bool = False

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
