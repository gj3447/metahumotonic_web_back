CREATE TABLE IF NOT EXISTS wiki_pages (
    page_id             UUID PRIMARY KEY,
    slug                TEXT NOT NULL,
    title               TEXT NOT NULL,
    content             TEXT NOT NULL,
    head_revision_id    UUID NOT NULL,
    content_hash        CHAR(64) NOT NULL,
    stream_version      BIGINT NOT NULL CHECK (stream_version > 0),
    authority           TEXT NOT NULL DEFAULT 'community' CHECK (authority = 'community'),
    review_status       TEXT NOT NULL DEFAULT 'unreviewed'
                        CHECK (review_status IN ('unreviewed', 'submitted')),
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    CONSTRAINT wiki_pages_slug_key UNIQUE (slug),
    CONSTRAINT wiki_pages_title_size CHECK (char_length(title) BETWEEN 1 AND 200),
    CONSTRAINT wiki_pages_content_size CHECK (char_length(content) BETWEEN 1 AND 100000)
);

CREATE TABLE IF NOT EXISTS wiki_events (
    page_id         UUID NOT NULL,
    event_index     BIGINT NOT NULL CHECK (event_index > 0),
    event_id        TEXT NOT NULL UNIQUE,
    command_id      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    schema_version  INTEGER NOT NULL CHECK (schema_version = 1),
    occurred_at     TIMESTAMPTZ NOT NULL,
    actor_id        TEXT NOT NULL,
    data            JSONB NOT NULL,
    PRIMARY KEY (page_id, event_index),
    FOREIGN KEY (page_id) REFERENCES wiki_pages(page_id) DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS wiki_events_recent_idx
    ON wiki_events (occurred_at DESC, event_id DESC);
CREATE INDEX IF NOT EXISTS wiki_events_revision_idx
    ON wiki_events (page_id, ((data->>'revision_id')))
    WHERE event_type IN ('page_created', 'revision_committed');

CREATE TABLE IF NOT EXISTS wiki_command_receipts (
    command_id      TEXT PRIMARY KEY,
    fingerprint     CHAR(64) NOT NULL,
    page_id         UUID NOT NULL,
    stream_version  BIGINT NOT NULL,
    event_ids       JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS wiki_outbox (
    effect_id       TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL,
    effect_type     TEXT NOT NULL CHECK (
                        effect_type IN ('review_requested', 'moderation_report_requested')
                    ),
    payload         JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'delivered', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL,
    delivered_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS wiki_outbox_delivery_idx
    ON wiki_outbox (status, COALESCE(next_attempt_at, created_at), created_at)
    WHERE status IN ('pending', 'failed');
