// Operator-only fixed-target collector. It never accepts URLs, commands,
// target IDs, headers, or catalog paths from CLI arguments.
import { Effect, Either, Schema } from "effect"
import { FixedProbeFetchLive, FixedProbeRequest, ProbeFetchTag, collectFixedProbeBatch } from "../dist/src/platform/FixedProbeCollector.js"

const args = process.argv.slice(2)
const value = (flag) => {
  const index = args.indexOf(flag)
  if (index < 0 || !args[index + 1]) throw new Error(`missing ${flag}`)
  return args[index + 1]
}
if (args.includes("--help") || args.includes("-h")) {
  console.log("usage: node scripts/collect-fixed-observations.mjs --run-id ID --observed-at ISO [--ttl-seconds 900] [--dry-run|--write]")
  process.exit(0)
}
const dryRun = !args.includes("--write")
if (args.includes("--write") && args.includes("--dry-run")) throw new Error("choose either --dry-run or --write")
const ttl = args.includes("--ttl-seconds") ? Number(value("--ttl-seconds")) : 900
const parsed = Schema.decodeUnknownEither(FixedProbeRequest)({ runId: value("--run-id"), observedAt: value("--observed-at"), ttlSeconds: ttl })
if (Either.isLeft(parsed)) throw new Error("invalid run id, observed time, or TTL")
const output = await Effect.runPromise(collectFixedProbeBatch(parsed.right).pipe(Effect.provide(FixedProbeFetchLive)))
if (dryRun) {
  console.log(JSON.stringify({ mode: "dry-run", batch: output.batch, results: output.results }, null, 2))
  process.exit(0)
}
const origin = process.env.MHB_PLATFORM_INGEST_ORIGIN
const key = process.env.MHB_PLATFORM_WRITE_KEY
if (!origin || !key) throw new Error("--write requires MHB_PLATFORM_INGEST_ORIGIN and MHB_PLATFORM_WRITE_KEY; neither value is printed")
const url = new URL(origin)
if (!/^https?:$/.test(url.protocol) || url.username || url.password || url.pathname !== "/" || url.search || url.hash) throw new Error("invalid operator-managed ingest origin")
const response = await fetch(new URL("/api/platform/v1/observations", url), { method: "POST", headers: { "content-type": "application/json", "x-api-key": key }, body: JSON.stringify(output.batch), signal: AbortSignal.timeout(10_000) })
if (!response.ok) throw new Error(`ingest failed with HTTP ${response.status}; rerun the exact same --run-id and --observed-at only after checking the receipt endpoint`)
const receipt = await response.json()
console.log(JSON.stringify({ mode: "write", receipt: { receiptId: receipt.receiptId, insertedCount: receipt.insertedCount, replayed: receipt.replayed, definitionDigest: receipt.definitionDigest }, results: output.results }, null, 2))
