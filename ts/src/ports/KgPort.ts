/**
 * The knowledge-graph port — the interface every read surface talks to.
 *
 * `app/kg.py` was one 614-line class that owned the driver, the cache, the
 * breaker, the Cypher, and the fallback constants all at once. Here those are
 * four separable things: this file declares *what* a KG can be asked, and the
 * layers below decide *how* (live Bolt, or the canonical snapshot).
 *
 * A handler that reads the KG gets `KgPort` in its `R` channel. It cannot
 * reach a driver it was not given, and a test supplies a different layer
 * rather than monkey-patching a module global.
 */
import { Context, Duration, Effect, Layer, Redacted } from "effect"
import neo4j, { type Driver, type QueryResult } from "neo4j-driver"
import { AppConfigTag } from "../Config.js"
import {
  ConsensusRecord,
  DomainRecord,
  FindingRecord,
  GraphNeighbor,
  LessonRecord,
  NodeNeighbors,
  PaperRecord,
  RecentItem,
  ResearchSummary,
  SkillRecord,
  StatsContract
} from "../domain/Contracts.js"
import { KgQueryFailed, Unavailable } from "../domain/Errors.js"
import * as Breaker from "./Breaker.js"
import * as Cache from "./Cache.js"
import * as Cypher from "./Cypher.js"
import {
  DOMAINS_COUNT,
  DOMAINS_FALLBACK,
  RESEARCH_SUMMARY_FALLBACK,
  SKILLS_COUNT,
  SKILLS_FALLBACK,
  STATS_FALLBACK
} from "./Snapshot.js"

export type CypherRow = Record<string, unknown>

export interface ListOptions {
  readonly limit: number
  readonly offset: number
}

/**
 * Every read surface, and nothing else.
 *
 * Note the error channels: the *curated* surfaces (`stats`, `domains`,
 * `skills`, research feeds) cannot fail — an outage degrades to the snapshot,
 * which is the documented behaviour. Only the raw Cypher proxy can fail, and
 * it fails with two distinct tags so "your query is wrong" is never reported
 * as "the database is down".
 */
export interface KgPort {
  readonly stats: Effect.Effect<StatsContract>
  readonly domains: Effect.Effect<ReadonlyArray<DomainRecord>>
  readonly skills: Effect.Effect<ReadonlyArray<SkillRecord>>

  readonly researchSummary: Effect.Effect<ResearchSummary>
  readonly findings: (
    opts: ListOptions & { readonly cycle: string }
  ) => Effect.Effect<ReadonlyArray<FindingRecord>>
  readonly lessons: (opts: ListOptions) => Effect.Effect<ReadonlyArray<LessonRecord>>
  readonly papers: (
    opts: ListOptions & { readonly domain: string }
  ) => Effect.Effect<ReadonlyArray<PaperRecord>>
  readonly consensus: (opts: ListOptions) => Effect.Effect<ReadonlyArray<ConsensusRecord>>
  readonly recent: (opts: { readonly limit: number }) => Effect.Effect<ReadonlyArray<RecentItem>>
  readonly neighbors: (opts: {
    readonly name: string
    readonly limit: number
  }) => Effect.Effect<NodeNeighbors>

  /** Raw parameterized Cypher for the read/write proxy. */
  readonly run: (
    query: string,
    params: Record<string, unknown>,
    mode: "read" | "write"
  ) => Effect.Effect<ReadonlyArray<CypherRow>, KgQueryFailed | Unavailable>

  /** Is the live driver actually usable right now? Feeds `/ready`. */
  readonly ping: Effect.Effect<boolean>

  /** `live` when the last curated read came from Bolt, `snapshot` otherwise. */
  readonly mode: Effect.Effect<"live" | "snapshot">
}

export class KgPortTag extends Context.Tag("KgPort")<KgPortTag, KgPort>() {}

// ---------------------------------------------------------------------------
// Snapshot layer — zero infra. This is what CI and offline development use,
// and what the live layer falls back to when Bolt is unreachable.
// ---------------------------------------------------------------------------

const snapshotPort: KgPort = {
  stats: Effect.succeed(STATS_FALLBACK),
  domains: Effect.succeed(DOMAINS_FALLBACK),
  skills: Effect.succeed(SKILLS_FALLBACK),
  researchSummary: Effect.succeed(RESEARCH_SUMMARY_FALLBACK),
  findings: () => Effect.succeed([]),
  lessons: () => Effect.succeed([]),
  papers: () => Effect.succeed([]),
  consensus: () => Effect.succeed([]),
  recent: () => Effect.succeed([]),
  neighbors: ({ name }) => Effect.succeed(notFoundNeighbors(name)),
  run: () =>
    Effect.fail(
      new Unavailable({ reason: "KG proxy is disabled: no live Neo4j connection configured" })
    ),
  ping: Effect.succeed(false),
  mode: Effect.succeed("snapshot" as const)
}

export const KgPortSnapshot = Layer.succeed(KgPortTag, snapshotPort)

// ---------------------------------------------------------------------------
// Live layer — Bolt, with the snapshot as the degradation target.
// ---------------------------------------------------------------------------

/** Neo4j integers arrive as `{low, high}`; dates as temporal objects. Flatten
 *  both to JSON scalars so a row is safe to serialize straight to the client. */
const toJsonScalar = (value: unknown): unknown => {
  if (value === null || value === undefined) return null
  if (neo4j.isInt(value)) {
    const asInt = value as { toNumber: () => number; inSafeRange: () => boolean; toString: () => string }
    return asInt.inSafeRange() ? asInt.toNumber() : asInt.toString()
  }
  if (Array.isArray(value)) return value.map(toJsonScalar)
  if (typeof value === "object") {
    const obj = value as Record<string, unknown>
    // Neo4j Node / Relationship / temporal types all expose a useful toString
    if ("properties" in obj) {
      return Object.fromEntries(
        Object.entries(obj["properties"] as Record<string, unknown>).map(([k, v]) => [
          k,
          toJsonScalar(v)
        ])
      )
    }
    if (typeof (obj as { toString?: unknown }).toString === "function" && obj.constructor !== Object) {
      return String(obj)
    }
    return Object.fromEntries(Object.entries(obj).map(([k, v]) => [k, toJsonScalar(v)]))
  }
  return value
}

/**
 * JSON has one number type; Cypher does not.
 *
 * `{"limit": 100}` arrives as a JS number, which the driver sends as a Float,
 * and Neo4j then rejects it with `LIMIT: '100.0' is not a valid value`. The
 * Python client never hit this because Python `int` maps to a Cypher Integer
 * on its own. Integral numbers are therefore promoted here, recursively, so a
 * proxy caller can write ordinary JSON.
 */
export const coerceParams = (value: unknown): unknown => {
  if (typeof value === "number") {
    return Number.isInteger(value) ? neo4j.int(value) : value
  }
  if (Array.isArray(value)) return value.map(coerceParams)
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([k, v]) => [k, coerceParams(v)])
    )
  }
  return value
}

const num = (v: unknown, fallback = 0): number => {
  const s = toJsonScalar(v)
  return typeof s === "number" ? s : typeof s === "string" ? Number(s) || fallback : fallback
}

const str = (v: unknown, fallback = ""): string => {
  const s = toJsonScalar(v)
  return typeof s === "string" ? s : s === null ? fallback : String(s)
}

// --- row decoders -----------------------------------------------------------
// Pure `CypherRow -> Record` functions. Free of Effect, config, and the driver,
// so the row-shape rules can be unit-tested against hand-written rows without
// a database anywhere in sight.

const decodeFinding = (r: CypherRow): FindingRecord =>
  new FindingRecord({
    name: str(r["name"]),
    finding: str(r["finding"]),
    axis: str(r["axis"]),
    subAxis: str(r["subAxis"]),
    confidence: str(r["confidence"]),
    cycleId: str(r["cycleId"]),
    verified: typeof r["verified"] === "boolean" ? r["verified"] : null,
    lakatosMechanism: str(r["lakatosMechanism"]),
    citationUrl: str(r["citationUrl"]),
    createdAt: str(r["createdAt"])
  })

const decodeLesson = (r: CypherRow): LessonRecord =>
  new LessonRecord({
    name: str(r["name"]),
    problem: str(r["problem"]),
    solution: str(r["solution"]),
    wrongAssumption: str(r["wrongAssumption"]),
    truth: str(r["truth"]),
    category: str(r["category"]),
    severity: str(r["severity"]),
    lakatosMechanism: str(r["lakatosMechanism"]),
    createdAt: str(r["createdAt"])
  })

const decodePaper = (r: CypherRow): PaperRecord =>
  new PaperRecord({
    title: str(r["title"]),
    author: str(r["author"]),
    year: r["year"] === null || r["year"] === undefined ? null : num(r["year"]),
    journal: str(r["journal"]),
    doi: str(r["doi"]),
    domain: str(r["domain"]),
    coreThesis: str(r["coreThesis"]),
    status: str(r["status"])
  })

const decodeConsensus = (r: CypherRow): ConsensusRecord =>
  new ConsensusRecord({
    name: str(r["name"]),
    summary: str(r["summary"]),
    createdAt: str(r["createdAt"])
  })

export const notFoundNeighbors = (name: string): NodeNeighbors =>
  new NodeNeighbors({ name, found: false, degree: 0, neighbors: [], truncated: false })

/** `NEIGHBORS` returns a single row holding `degree` + a pre-capped list. */
export const decodeNeighbors = (name: string, rows: ReadonlyArray<CypherRow>): NodeNeighbors => {
  const r = rows[0]
  // `[]` means node-not-found and `undefined` means the query degraded — both
  // are "not found, no 500", which is what the Python does too.
  if (r === undefined) return notFoundNeighbors(name)

  const degree = num(r["degree"])
  const raw = toJsonScalar(r["neighbors"])
  const list = Array.isArray(raw) ? raw : []
  const neighbors = list.map((x) => {
    const o = (x ?? {}) as Record<string, unknown>
    return new GraphNeighbor({
      direction: str(o["direction"], "out") === "in" ? "in" : "out",
      type: str(o["type"]),
      name: str(o["name"]),
      labels: Array.isArray(o["labels"]) ? (o["labels"] as ReadonlyArray<unknown>).map(String) : []
    })
  })
  return new NodeNeighbors({
    name,
    found: true,
    degree,
    neighbors,
    truncated: degree > neighbors.length
  })
}

/**
 * The unified newest-first feed. Pure, so the ordering rule — newest first,
 * blank timestamps sink to the bottom — is testable on its own.
 */
export const mergeRecent = (input: {
  readonly findings: ReadonlyArray<FindingRecord>
  readonly lessons: ReadonlyArray<LessonRecord>
  readonly consensus: ReadonlyArray<ConsensusRecord>
  readonly limit: number
}): ReadonlyArray<RecentItem> => {
  const items: Array<RecentItem> = []

  for (const f of input.findings) {
    const title = `${f.axis} · ${f.subAxis}`.replace(/^[\s·]+|[\s·]+$/g, "")
    items.push(
      new RecentItem({
        type: "finding",
        name: f.name,
        title: title === "" ? "ResearchFinding" : title,
        summary: f.finding,
        createdAt: f.createdAt
      })
    )
  }

  for (const l of input.lessons) {
    items.push(
      new RecentItem({
        type: "lesson",
        name: l.name,
        title: l.problem === "" ? l.name : l.problem,
        summary: l.truth !== "" ? `${l.wrongAssumption} → ${l.truth}` : l.solution,
        createdAt: l.createdAt
      })
    )
  }

  for (const c of input.consensus) {
    items.push(
      new RecentItem({
        type: "consensus",
        name: c.name,
        title: c.name,
        summary: c.summary,
        createdAt: c.createdAt
      })
    )
  }

  return items
    .slice()
    .sort((a, b) => (b.createdAt ?? "").localeCompare(a.createdAt ?? ""))
    .slice(0, input.limit)
}

/**
 * Acquire a driver AND prove it can reach the server.
 *
 * `neo4j.driver()` constructs successfully against a host that does not exist
 * — verified empirically — so an earlier version of this file, which only
 * caught construction errors, made `neo4jFallbackUris` dead configuration:
 * `Effect.firstSuccessOf` always "succeeded" on `uris[0]`. During a Bolt
 * outage this service degraded to the snapshot while the Python failed over.
 *
 * `verifyConnectivity()` is what makes the fallback real. A driver that fails
 * it is closed here rather than leaked to the next candidate.
 */
const CONNECT_TIMEOUT_MILLIS = 5_000

/**
 * Race the connectivity probe against a timer INSIDE the promise.
 *
 * Two traps, both found by pointing the primary at a blackholed address
 * (192.0.2.1 — packets dropped, no RST) and watching startup never finish:
 *
 *  1. the driver's own `connectionTimeout` does not bound
 *     `verifyConnectivity()` in that case, and
 *  2. wrapping it in `Effect.timeoutFail` does not help either, because this
 *     runs inside `Effect.acquireRelease`, whose acquire is UNINTERRUPTIBLE.
 *
 * So the deadline has to be enforced by the promise itself.
 */
const withDeadline = async <A>(work: Promise<A>, millis: number, what: string): Promise<A> => {
  let timer: NodeJS.Timeout | undefined
  try {
    return await Promise.race([
      work,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${what} timed out after ${millis}ms`)), millis)
      })
    ])
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}

const acquireDriver = (
  uri: string,
  user: string,
  password: string
): Effect.Effect<Driver, Unavailable, never> =>
  Effect.tryPromise({
    try: async () => {
      const driver = neo4j.driver(uri, neo4j.auth.basic(user, password), {
        connectionTimeout: CONNECT_TIMEOUT_MILLIS,
        connectionAcquisitionTimeout: CONNECT_TIMEOUT_MILLIS,
        maxTransactionRetryTime: CONNECT_TIMEOUT_MILLIS
      })
      try {
        await withDeadline(driver.verifyConnectivity(), CONNECT_TIMEOUT_MILLIS, `neo4j ${uri}`)
        return driver
      } catch (cause) {
        // Close the rejected candidate rather than leaking it to the next one.
        await driver.close().catch(() => {})
        throw cause
      }
    },
    catch: (cause) => new Unavailable({ reason: `neo4j ${uri} unreachable: ${String(cause)}` })
  })

export const KgPortLive: Layer.Layer<KgPortTag, never, AppConfigTag> = Layer.scoped(
  KgPortTag,
  Effect.gen(function* () {
    const cfg = yield* AppConfigTag
    const breaker = yield* Breaker.make()

    // Bounded TTL + single-flight, matching the Python's 120s/300s.
    // Without this every request in live mode is a Bolt round-trip; the
    // settings were parsed but unused in the first version of this port.
    const statsCache = yield* Cache.make<unknown>({
      ttlSeconds: cfg.statsCacheTtlSeconds,
      maxEntries: cfg.cacheMaxEntries
    })
    const researchCache = yield* Cache.make<unknown>({
      ttlSeconds: cfg.researchCacheTtlSeconds,
      maxEntries: cfg.cacheMaxEntries
    })

    // Not opted in → the live layer *is* the snapshot layer. Same object, so
    // there is exactly one degradation path rather than two that can drift.
    if (!cfg.neo4jLive) return snapshotPort

    const uris = [cfg.neo4jUri, ...cfg.neo4jFallbackUris]
    const password = Redacted.value(cfg.neo4jPassword)
    const timeout = Duration.seconds(cfg.kgQueryTimeoutSeconds)

    const driver = yield* Effect.acquireRelease(
      Effect.firstSuccessOf(uris.map((uri) => acquireDriver(uri, cfg.neo4jUser, password))).pipe(
        Effect.catchAll(() => Effect.succeed(null as Driver | null))
      ),
      (d) => (d === null ? Effect.void : Effect.promise(() => d.close()).pipe(Effect.orDie))
    )

    if (driver === null) return snapshotPort

    const execute = (
      query: string,
      params: Record<string, unknown>,
      mode: "read" | "write"
    ): Effect.Effect<ReadonlyArray<CypherRow>, KgQueryFailed> =>
      Effect.tryPromise({
        try: async () => {
          const session = driver.session({
            database: cfg.neo4jDatabase,
            defaultAccessMode: mode === "read" ? neo4j.session.READ : neo4j.session.WRITE
          })
          try {
            const work = (tx: { run: (q: string, p: Record<string, unknown>) => Promise<QueryResult> }) =>
              tx.run(query, params)
            const result: QueryResult =
              mode === "read"
                ? await session.executeRead(work)
                : await session.executeWrite(work)
            return result.records.map((rec) => {
              const row: CypherRow = {}
              for (const key of rec.keys) row[String(key)] = toJsonScalar(rec.get(key))
              return row
            })
          } finally {
            await session.close()
          }
        },
        catch: (cause) => new KgQueryFailed({ reason: String(cause) })
      }).pipe(Effect.timeoutFail({
        duration: timeout,
        onTimeout: () =>
          new KgQueryFailed({ reason: `query exceeded ${cfg.kgQueryTimeoutSeconds}s budget` })
      }))

    /**
     * A curated read: cached, breaker-guarded, degrading to `fallback`.
     *
     * Cache OUTSIDE the breaker so a served-from-cache value costs nothing
     * even while the breaker is open, and single-flight means N concurrent
     * misses produce one Bolt query rather than N.
     */
    const cachedCurated = <A>(
      cache: Cache.TtlCache<unknown>,
      key: string,
      query: string,
      params: Record<string, unknown>,
      decode: (rows: ReadonlyArray<CypherRow>) => A,
      fallback: A
    ): Effect.Effect<A> =>
      cache.get(key, curated(query, params, decode, fallback)) as Effect.Effect<A>

    const curated = <A>(
      query: string,
      params: Record<string, unknown>,
      decode: (rows: ReadonlyArray<CypherRow>) => A,
      fallback: A
    ): Effect.Effect<A> =>
      Breaker.guard(
        breaker,
        execute(query, params, "read").pipe(
          // Degrading to the snapshot is correct, but doing it *silently* is
          // how a broken query hides for weeks behind an empty list.
          Effect.tapError((e) => Effect.logWarning(`kg: degraded to snapshot — ${e.reason}`)),
          Effect.map(decode)
        ),
        Effect.succeed(fallback)
      )

    // Named up front because `recent` is *derived* from these three rather
    // than being its own query — same as `KG.get_recent` in the Python.
    const findingsOf: KgPort["findings"] = ({ limit, offset, cycle }) =>
      cachedCurated(
        researchCache,
        `findings:${limit}:${offset}:${cycle}`,
        Cypher.FINDINGS,
        { limit: neo4j.int(limit), offset: neo4j.int(offset), cycle },
        (rows) => rows.map(decodeFinding),
        []
      )

    const lessonsOf: KgPort["lessons"] = ({ limit, offset }) =>
      cachedCurated(
        researchCache,
        `lessons:${limit}:${offset}`,
        Cypher.LESSONS,
        { limit: neo4j.int(limit), offset: neo4j.int(offset) },
        (rows) => rows.map(decodeLesson),
        []
      )

    const papersOf: KgPort["papers"] = ({ limit, offset, domain }) =>
      cachedCurated(
        researchCache,
        `papers:${limit}:${offset}:${domain}`,
        Cypher.PAPERS,
        { limit: neo4j.int(limit), offset: neo4j.int(offset), domain },
        (rows) => rows.map(decodePaper),
        []
      )

    const consensusOf: KgPort["consensus"] = ({ limit }) =>
      cachedCurated(
        researchCache,
        `consensus:${limit}`,
        Cypher.CONSENSUS,
        { limit: neo4j.int(limit) },
        (rows) => rows.map(decodeConsensus),
        []
      )

    return {
      stats: cachedCurated(
        statsCache,
        "stats",
        Cypher.STATS,
        {},
        (rows) => {
          const r = rows[0]
          if (r === undefined) return STATS_FALLBACK
          return new StatsContract({
            nodes: num(r["nodes"]),
            rels: num(r["rels"]),
            labels: num(r["labels"]),
            relTypes: num(r["relTypes"]),
            // curated-surface counts, not a Neo4j count — see Snapshot.ts
            domains: DOMAINS_COUNT,
            skills: SKILLS_COUNT
          })
        },
        STATS_FALLBACK
      ),

      domains: cachedCurated(
        statsCache,
        "domains",
        Cypher.DOMAINS,
        {},
        (rows) =>
          rows.map(
            (r) =>
              ({
                name: str(r["name"]),
                displayName: str(r["displayName"]),
                nodeCount: num(r["nodeCount"]),
                description: str(r["description"])
              })
          ),
        DOMAINS_FALLBACK
      ),

      // The skills surface is curated in code, not queried — same in both modes.
      skills: Effect.succeed(SKILLS_FALLBACK),

      researchSummary: cachedCurated(
        researchCache,
        "summary",
        Cypher.RESEARCH_SUMMARY,
        {},
        (rows) => {
          const r = rows[0]
          if (r === undefined) return RESEARCH_SUMMARY_FALLBACK
          return new ResearchSummary({
            findings: num(r["findings"]),
            lessons: num(r["lessons"]),
            papers: num(r["papers"]),
            validations: num(r["validations"]),
            consensus: num(r["consensus"]),
            decisions: num(r["decisions"]),
            apostles: num(r["apostles"]),
            domains: num(r["domains"]),
            source: "live"
          })
        },
        RESEARCH_SUMMARY_FALLBACK
      ),

      findings: findingsOf,
      lessons: lessonsOf,
      papers: papersOf,
      consensus: consensusOf,

      // Derived, not queried — exactly as in `KG.get_recent`: merge the three
      // (already breaker-guarded) feeds so the unified feed costs no extra
      // round-trip beyond what the dedicated endpoints already do.
      recent: ({ limit }) =>
        Effect.gen(function* () {
          const per = Math.max(8, Math.min(limit, 40))
          const [findings, lessons, consensus] = yield* Effect.all(
            [
              findingsOf({ limit: per, offset: 0, cycle: "" }),
              lessonsOf({ limit: per, offset: 0 }),
              consensusOf({ limit: Math.min(per, 25), offset: 0 })
            ],
            { concurrency: 3 }
          )
          return mergeRecent({ findings, lessons, consensus, limit })
        }),

      neighbors: ({ name, limit }) =>
        cachedCurated(
          researchCache,
          `neighbors:${name}:${limit}`,
          Cypher.NEIGHBORS,
          { name, limit: neo4j.int(limit) },
          (rows) => decodeNeighbors(name, rows),
          notFoundNeighbors(name)
        ),

      run: (query, params, mode) =>
        execute(query, coerceParams(params) as Record<string, unknown>, mode),

      ping: execute("RETURN 1 AS ok", {}, "read").pipe(
        Effect.map(() => true),
        Effect.catchAll(() => Effect.succeed(false))
      ),

      mode: Effect.succeed("live" as const)
    } satisfies KgPort
  })
)
