// Operator-only fixed-target collector. It never accepts URLs, commands,
// target IDs, headers, or catalog paths from CLI arguments.
import { createHash } from "node:crypto"
import { readFile, writeFile } from "node:fs/promises"
import { Effect, Either, Schema } from "effect"
import { FixedProbeFetchLive, FixedProbeRequest, collectFixedProbeBatch, fixedProbeBatchBytes, isSafeFixedProbeIngestOrigin, parseFixedProbeBatchBytes } from "../dist/src/platform/FixedProbeCollector.js"

const args = process.argv.slice(2)
const value = (flag) => {
  const index = args.indexOf(flag)
  if (index < 0 || !args[index + 1]) throw new Error(`missing ${flag}`)
  return args[index + 1]
}
const has = (flag) => args.includes(flag)
if (has("--help") || has("-h")) {
  console.log("plan: node scripts/collect-fixed-observations.mjs --run-id ID --observed-at ISO --output NEW_BATCH.json [--ttl-seconds 900]\nwrite: node scripts/collect-fixed-observations.mjs --write --input BATCH.json")
  process.exit(0)
}
const sha256 = (bytes) => `sha256:${createHash("sha256").update(bytes).digest("hex")}`

if (!has("--write")) {
  if (has("--input")) throw new Error("--input is only valid with --write")
  const ttl = has("--ttl-seconds") ? Number(value("--ttl-seconds")) : 900
  const parsed = Schema.decodeUnknownEither(FixedProbeRequest)({ runId: value("--run-id"), observedAt: value("--observed-at"), ttlSeconds: ttl })
  if (Either.isLeft(parsed)) throw new Error("invalid run id, observed time, or TTL")
  const outputPath = value("--output")
  const output = await Effect.runPromise(collectFixedProbeBatch(parsed.right).pipe(Effect.provide(FixedProbeFetchLive)))
  const bytes = fixedProbeBatchBytes(output.batch)
  // New-only, owner-selected plan file. Existing approved plans are immutable.
  await writeFile(outputPath, bytes, { flag: "wx", mode: 0o600 })
  console.log(JSON.stringify({ mode: "plan", file: outputPath, sha256: sha256(bytes), receiptId: output.batch.receiptId, observationCount: output.batch.observations.length, results: output.results }, null, 2))
  process.exit(0)
}

if (has("--run-id") || has("--observed-at") || has("--ttl-seconds") || has("--output")) throw new Error("--write accepts only --input; it never probes again")
const bytes = await readFile(value("--input"))
const batch = parseFixedProbeBatchBytes(bytes)
const origin = process.env.MHB_PLATFORM_INGEST_ORIGIN
const key = process.env.MHB_PLATFORM_WRITE_KEY
if (!origin || !key) throw new Error("--write requires MHB_PLATFORM_INGEST_ORIGIN and MHB_PLATFORM_WRITE_KEY; neither value is printed")
if (!isSafeFixedProbeIngestOrigin(origin)) throw new Error("ingest origin must be HTTPS or loopback HTTP without credentials or path")
const url = new URL(origin)
// There is intentionally one POST attempt. If its response is uncertain, run
// this same --write --input command again: `bytes` are never regenerated.
const response = await fetch(new URL("/api/platform/v1/observations", url), { method: "POST", headers: { "content-type": "application/json", "x-api-key": key }, body: bytes, signal: AbortSignal.timeout(10_000) })
if (!response.ok) throw new Error(`ingest failed with HTTP ${response.status}; retry this exact --write --input file only after checking the receipt endpoint`)
const receipt = await response.json()
if (!receipt || receipt.receiptId !== batch.receiptId) throw new Error("ingest response did not match the saved receipt identity")
console.log(JSON.stringify({ mode: "write", sha256: sha256(bytes), receipt: { receiptId: receipt.receiptId, insertedCount: receipt.insertedCount, replayed: receipt.replayed, definitionDigest: receipt.definitionDigest } }, null, 2))
