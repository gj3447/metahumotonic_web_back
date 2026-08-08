-- Minimal reactive moderation for the public community wiki.
-- Reports remain durable outbox work; only an authenticated operator command
-- can change a page's public visibility.

ALTER TABLE wiki_pages
    ADD COLUMN head_actor_id TEXT;

UPDATE wiki_pages AS page
SET head_actor_id = head.actor_id
FROM (
    SELECT DISTINCT ON (page_id) page_id, actor_id
    FROM wiki_events
    WHERE event_type IN ('page_created', 'revision_committed')
    ORDER BY page_id, event_index DESC
) AS head
WHERE head.page_id = page.page_id;

ALTER TABLE wiki_pages
    ALTER COLUMN head_actor_id SET NOT NULL,
    ADD COLUMN moderation_status TEXT NOT NULL DEFAULT 'visible',
    ADD CONSTRAINT wiki_pages_moderation_status_check
        CHECK (moderation_status IN ('visible', 'quarantined'));

CREATE INDEX wiki_pages_public_updated_idx
    ON wiki_pages (updated_at DESC, page_id DESC)
    WHERE moderation_status = 'visible';
