CREATE TABLE mhb_platform.catalog_versions (
  digest text PRIMARY KEY CHECK (digest ~ '^[a-f0-9]{64}$'),
  observed_at timestamptz NOT NULL,
  imported_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  document jsonb NOT NULL CHECK (jsonb_typeof(document) = 'object')
);
CREATE TABLE mhb_platform.asset_versions (
  catalog_digest text NOT NULL REFERENCES mhb_platform.catalog_versions(digest),
  id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('program','repository','service','mcp-server','datastore','host','deployment')),
  category text,
  owner_repository_id text,
  document jsonb NOT NULL,
  PRIMARY KEY (catalog_digest, id),
  CHECK (document->>'id' = id AND document->>'kind' = kind),
  FOREIGN KEY (catalog_digest, owner_repository_id) REFERENCES mhb_platform.asset_versions(catalog_digest,id) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX asset_owner ON mhb_platform.asset_versions(catalog_digest,owner_repository_id,kind);
CREATE TABLE mhb_platform.ingest_receipts (
  id text PRIMARY KEY,
  payload_digest text NOT NULL CHECK (payload_digest ~ '^[a-f0-9]{64}$'),
  catalog_digest text NOT NULL REFERENCES mhb_platform.catalog_versions(digest),
  received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  observation_count integer NOT NULL CHECK (observation_count >= 0),
  inserted_count integer NOT NULL CHECK (inserted_count BETWEEN 0 AND observation_count)
);
CREATE TABLE mhb_platform.observations (
  sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  id text PRIMARY KEY,
  payload_digest text NOT NULL CHECK (payload_digest ~ '^[a-f0-9]{64}$'),
  subject_id text NOT NULL,
  catalog_digest text NOT NULL,
  receipt_id text NOT NULL REFERENCES mhb_platform.ingest_receipts(id),
  check_kind text NOT NULL CHECK (check_kind IN ('process','container-health','readiness','http','backup')),
  outcome text NOT NULL CHECK (outcome IN ('healthy','running','degraded','failed','reachable','unknown')),
  observed_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL CHECK (expires_at > observed_at),
  document jsonb NOT NULL,
  FOREIGN KEY (catalog_digest, subject_id) REFERENCES mhb_platform.asset_versions(catalog_digest,id),
  CHECK (document->>'id' = id AND document->>'subjectId' = subject_id
    AND document->>'check' = check_kind AND document->>'outcome' = outcome)
);
CREATE INDEX observation_latest ON mhb_platform.observations(subject_id,check_kind,observed_at DESC,id DESC);
CREATE INDEX observation_history ON mhb_platform.observations(subject_id,sequence DESC);
CREATE FUNCTION mhb_platform.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'platform history is append-only' USING ERRCODE = '55000'; END;
$$;
CREATE TRIGGER immutable_catalog BEFORE UPDATE OR DELETE ON mhb_platform.catalog_versions FOR EACH ROW EXECUTE FUNCTION mhb_platform.reject_mutation();
CREATE TRIGGER immutable_asset BEFORE UPDATE OR DELETE ON mhb_platform.asset_versions FOR EACH ROW EXECUTE FUNCTION mhb_platform.reject_mutation();
CREATE TRIGGER immutable_receipt BEFORE UPDATE OR DELETE ON mhb_platform.ingest_receipts FOR EACH ROW EXECUTE FUNCTION mhb_platform.reject_mutation();
CREATE TRIGGER immutable_observation BEFORE UPDATE OR DELETE ON mhb_platform.observations FOR EACH ROW EXECUTE FUNCTION mhb_platform.reject_mutation();
