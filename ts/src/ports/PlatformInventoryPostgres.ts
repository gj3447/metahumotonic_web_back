import { Config, Effect, Either, Layer, Redacted, Schema } from "effect"
import { createHash } from "node:crypto"
import { readFile } from "node:fs/promises"
import { Pool, type PoolClient } from "pg"
import { BadRequest, Conflict, NotFound, Unavailable } from "../domain/Errors.js"
import { decodeCatalog, PlatformObservation, type PlatformCatalog } from "../domain/PlatformGraph.js"
import { IngestReceipt, ObservationBatch, ObservationHistory, observationProblems } from "../domain/ObservationIngest.js"
import { PlatformConfigTag, type PlatformConfig } from "../platform/Config.js"
import { contentDigest } from "../platform/Digest.js"
import { PlatformInventoryStoreTag, type PlatformInventoryStore } from "./PlatformInventoryStore.js"

const unavailable = () => new Unavailable({ reason: "platform_storage_unavailable" })
const attempt = <A>(operation: () => Promise<A>) => Effect.tryPromise({ try: operation, catch: (error) =>
  error instanceof BadRequest || error instanceof Conflict || error instanceof NotFound ? error : unavailable() })
const readAttempt = <A>(operation: () => Promise<A>) => Effect.tryPromise({ try: operation, catch: unavailable })
const decode = <A, I>(schema: Schema.Schema<A, I>, input: unknown): A => Schema.decodeUnknownSync(schema, { onExcessProperty: "error" })(input)
const migration = async () => {
  const sql = await readFile(new URL("../../config/migrations/001-platform.sql", import.meta.url), "utf8")
  return { sql, digest: createHash("sha256").update(sql).digest("hex") }
}
export const createPlatformPool = (connectionString: string) => {
  const url = new URL(connectionString)
  if (!["postgres:", "postgresql:"].includes(url.protocol) || !url.hostname || url.pathname.length < 2) throw new Error("invalid platform PostgreSQL configuration")
  const pool = new Pool({ connectionString, max: 5, connectionTimeoutMillis: 2000, idleTimeoutMillis: 10000,
    statement_timeout: 5000, query_timeout: 6000, lock_timeout: 2000, idle_in_transaction_session_timeout: 5000,
    application_name: "metahumotonic-platform" })
  // Idle connection errors must not become unhandled EventEmitter exceptions.
  pool.on("error", () => {})
  return pool
}
const transaction = async <A>(pool: Pool, operation: (client: PoolClient) => Promise<A>): Promise<A> => {
  const client = await pool.connect()
  let broken = false
  try {
    await client.query("BEGIN")
    // One bounded writer per platform database; protects receipt replay and IDs across replicas.
    await client.query("SELECT pg_advisory_xact_lock(746238, 1)")
    const result = await operation(client)
    await client.query("COMMIT")
    return result
  } catch (error) {
    try { await client.query("ROLLBACK") } catch { broken = true }
    throw error
  } finally { client.release(broken) }
}
const verifyMigration = async (client: Pick<Pool, "query"> | PoolClient) => {
  const expected = await migration()
  const result = await client.query("SELECT version, checksum FROM mhb_platform.schema_migrations ORDER BY version")
  if (result.rows.length !== 1 || result.rows[0]?.version !== 1 || result.rows[0]?.checksum !== expected.digest) throw unavailable()
}

/** Explicit administrative CLI only. HTTP startup never runs DDL or imports. */
export const migratePlatform = async (pool: Pool) => transaction(pool, async (client) => {
  const expected = await migration()
  await client.query("CREATE SCHEMA IF NOT EXISTS mhb_platform")
  await client.query("CREATE TABLE IF NOT EXISTS mhb_platform.schema_migrations (version integer PRIMARY KEY, checksum text NOT NULL)")
  const applied = await client.query("SELECT version, checksum FROM mhb_platform.schema_migrations ORDER BY version")
  if (applied.rows.length) { await verifyMigration(client); return { applied: false, version: 1, checksum: expected.digest } }
  await client.query(expected.sql)
  await client.query("INSERT INTO mhb_platform.schema_migrations(version,checksum) VALUES(1,$1)", [expected.digest])
  return { applied: true, version: 1, checksum: expected.digest }
})

const receiptRow = (row: Record<string, unknown>): IngestReceipt => decode(IngestReceipt, {
  receiptId: row.id, digest: row.payload_digest, definitionDigest: row.catalog_digest,
  receivedAt: (row.received_at as Date).toISOString(), observationCount: row.observation_count, insertedCount: row.inserted_count
})
const ingest = async (client: PoolClient, catalog: PlatformCatalog, definitionDigest: string,
  batch: ObservationBatch, now: number) => {
  // Bind retries to submitted values, even if a later deployment pins a new
  // definition. The original receipt retains the version that accepted it.
  const digest = contentDigest(batch)
  const previous = await client.query("SELECT * FROM mhb_platform.ingest_receipts WHERE id=$1", [batch.receiptId])
  if (previous.rows.length) {
    if (previous.rows[0].payload_digest !== digest) throw new Conflict({ reason: "receipt_id_conflict" })
    return { ...receiptRow(previous.rows[0]), replayed: true }
  }
  const problems = observationProblems(catalog, batch.observations, now)
  if (problems.length) throw new BadRequest({ reason: problems.join("; ") })
  const rows = batch.observations.map((document) => ({ document, digest: contentDigest(document) }))
  const existing = await client.query("SELECT id, payload_digest FROM mhb_platform.observations WHERE id=ANY($1::text[])", [batch.observations.map((o) => o.id)])
  const byId = new Map(existing.rows.map((row) => [row.id, row.payload_digest]))
  for (const row of rows) if (byId.has(row.document.id) && byId.get(row.document.id) !== row.digest) throw new Conflict({ reason: "observation_id_conflict" })
  const fresh = rows.filter((row) => !byId.has(row.document.id))
  const saved = await client.query(`INSERT INTO mhb_platform.ingest_receipts(id,payload_digest,catalog_digest,observation_count,inserted_count)
    VALUES($1,$2,$3,$4,$5) RETURNING *`, [batch.receiptId, digest, definitionDigest, rows.length, fresh.length])
  await client.query(`INSERT INTO mhb_platform.observations(id,payload_digest,subject_id,catalog_digest,receipt_id,check_kind,outcome,observed_at,expires_at,document)
    SELECT x->'document'->>'id',x->>'digest',x->'document'->>'subjectId',$2,$3,x->'document'->>'check',x->'document'->>'outcome',
      (x->'document'->>'observedAt')::timestamptz,(x->'document'->>'expiresAt')::timestamptz,x->'document'
    FROM jsonb_array_elements($1::jsonb) x`, [JSON.stringify(fresh), definitionDigest, batch.receiptId])
  return { ...receiptRow(saved.rows[0]), replayed: false }
}

export const importPlatformCatalog = async (pool: Pool, input: unknown) => {
  const catalog = Either.getOrThrow(decodeCatalog(input)), digest = contentDigest(catalog)
  return transaction(pool, async (client) => {
    await verifyMigration(client)
    await client.query("INSERT INTO mhb_platform.catalog_versions(digest,observed_at,document) VALUES($1,$2,$3::jsonb) ON CONFLICT DO NOTHING",
      [digest, catalog.observedAt, JSON.stringify(catalog)])
    await client.query(`INSERT INTO mhb_platform.asset_versions(catalog_digest,id,kind,category,owner_repository_id,document)
      SELECT $1,x->>'id',x->>'kind',x->>'category',x->>'ownerRepositoryId',x FROM jsonb_array_elements($2::jsonb) x ON CONFLICT DO NOTHING`,
      [digest, JSON.stringify(catalog.nodes)])
    return ingest(client, catalog, digest, { receiptId: `catalog:${digest}`, observations: catalog.observations ?? [] }, Date.now())
  })
}

export const makePostgresInventory = (pool: Pool, cfg: Pick<PlatformConfig, "catalog">): PlatformInventoryStore => {
  const definitionDigest = contentDigest(cfg.catalog)
  const definition = async (client: Pick<Pool, "query"> | PoolClient) => {
    await verifyMigration(client)
    const result = await client.query("SELECT document FROM mhb_platform.catalog_versions WHERE digest=$1", [definitionDigest])
    if (result.rows.length !== 1) throw unavailable()
    const graph = Either.getOrThrow(decodeCatalog(result.rows[0].document))
    if (contentDigest(graph) !== definitionDigest) throw unavailable()
    return graph
  }
  const read = readAttempt(async () => {
    const graph = await definition(pool)
    const latest = await pool.query(`SELECT DISTINCT ON (o.subject_id,o.check_kind) o.document
      FROM mhb_platform.observations o JOIN mhb_platform.asset_versions a ON a.id=o.subject_id AND a.catalog_digest=$1
      ORDER BY o.subject_id,o.check_kind,o.observed_at DESC,o.id DESC`, [definitionDigest])
    const observations = latest.rows.map((row) => decode(PlatformObservation, row.document))
    const observedAt = new Date(Math.max(Date.parse(graph.observedAt), ...observations.map((o) => Date.parse(o.observedAt)))).toISOString()
    const catalog = Either.getOrThrow(decodeCatalog({ ...graph, observedAt, observations }))
    return { catalog, digest: contentDigest(catalog), definitionDigest, source: "postgres" as const }
  })
  return {
    read,
    readiness: read.pipe(Effect.timeoutOption("2 seconds"), Effect.map((value) => ({ platform_postgres_required: true, platform_postgres_live: value._tag === "Some" })),
      Effect.catchAll(() => Effect.succeed({ platform_postgres_required: true, platform_postgres_live: false }))),
    append: (input, now) => attempt(async () => {
      const parsed = Schema.decodeUnknownEither(ObservationBatch, { onExcessProperty: "error" })(input)
      if (Either.isLeft(parsed)) throw new BadRequest({ reason: "invalid observation batch" })
      return transaction(pool, async (client) => ingest(client, await definition(client), definitionDigest, parsed.right, now))
    }).pipe(Effect.catchTag("NotFound", () => Effect.fail(unavailable()))),
    receipt: (id) => attempt(async () => {
      await verifyMigration(pool)
      const result = await pool.query("SELECT * FROM mhb_platform.ingest_receipts WHERE id=$1", [id])
      if (!result.rows.length) throw new NotFound({ reason: "receipt not found" })
      return receiptRow(result.rows[0])
    }).pipe(Effect.catchTags({ BadRequest: () => Effect.fail(unavailable()), Conflict: () => Effect.fail(unavailable()) })),
    history: (query) => readAttempt(async () => {
      await verifyMigration(pool)
      const limit = Math.max(1, Math.min(100, query.limit ?? 25))
      const result = await pool.query(`SELECT o.sequence,o.receipt_id,o.document,r.received_at FROM mhb_platform.observations o
        JOIN mhb_platform.ingest_receipts r ON r.id=o.receipt_id
        WHERE ($1::text IS NULL OR o.subject_id=$1) AND ($2::bigint IS NULL OR o.sequence < $2)
        ORDER BY o.sequence DESC LIMIT $3`, [query.subjectId ?? null, query.before ?? null, limit + 1])
      const items = result.rows.slice(0, limit).map((row) => ({ sequence: Number(row.sequence), receiptId: row.receipt_id,
        receivedAt: row.received_at.toISOString(), observation: decode(PlatformObservation, row.document) }))
      if (items.some((item) => !Number.isSafeInteger(item.sequence))) throw unavailable()
      return decode(ObservationHistory, { source: "postgres", items, nextBefore: result.rows.length > limit ? items.at(-1)?.sequence : null })
    })
  }
}

export const PlatformInventoryLive = Layer.scoped(PlatformInventoryStoreTag, Effect.gen(function* () {
  const cfg = yield* PlatformConfigTag
  const uri = yield* Config.redacted("MHB_PLATFORM_DATABASE_URL").pipe(Config.withDefault(Redacted.make("")))
  const required = yield* Config.boolean("MHB_PLATFORM_DATABASE_REQUIRED").pipe(Config.withDefault(false))
  if (!Redacted.value(uri)) return {
    read: required ? Effect.fail(unavailable()) : Effect.succeed({ catalog: cfg.catalog, digest: cfg.catalogDigest, definitionDigest: cfg.catalogDigest, source: "snapshot" as const }),
    readiness: Effect.succeed({ platform_postgres_required: required, platform_postgres_live: false }),
    append: () => Effect.fail(unavailable()), receipt: () => Effect.fail(unavailable()), history: () => Effect.fail(unavailable())
  } satisfies PlatformInventoryStore
  const pool = yield* Effect.acquireRelease(
    Effect.try({ try: () => createPlatformPool(Redacted.value(uri)), catch: () => new Error("invalid platform PostgreSQL configuration") }),
    (pool) => Effect.promise(() => pool.end()).pipe(Effect.catchAllCause(() => Effect.void))
  )
  return makePostgresInventory(pool, cfg)
}).pipe(Effect.orDie))
