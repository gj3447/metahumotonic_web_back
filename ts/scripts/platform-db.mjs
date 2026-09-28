import { readFile } from "node:fs/promises"
import { createHash } from "node:crypto"
import { createPlatformPool, importPlatformCatalog, migratePlatform } from "../dist/src/ports/PlatformInventoryPostgres.js"
import { contentDigest } from "../dist/src/platform/Digest.js"

// Explicit admin operation. No .env loading, automatic production discovery or DDL on HTTP startup.
const command = process.argv[2]
if (!["migrate", "import", "readback", "inspect"].includes(command) || !process.env.MHB_PLATFORM_DATABASE_URL) {
  throw new Error("Set MHB_PLATFORM_DATABASE_URL explicitly; usage: platform-db.mjs migrate | import | readback [catalog.json]")
}
let pool
try {
  pool = createPlatformPool(process.env.MHB_PLATFORM_DATABASE_URL)
  const catalog = command === "migrate" ? null : JSON.parse(await readFile(process.argv[3] || new URL("../dist/config/platform-catalog.json", import.meta.url), "utf8"))
  const expected = async () => ({ catalogDigest: contentDigest(catalog), migrationChecksum: createHash("sha256").update(await readFile(new URL("../dist/config/migrations/001-platform.sql", import.meta.url))).digest("hex"), assets: catalog.nodes.length, receipts: 1, observations: (catalog.observations ?? []).length })
  const result = command === "migrate" ? await migratePlatform(pool) : command === "import" ? await importPlatformCatalog(pool, catalog) : command === "inspect" ? await expected() : await (async () => {
    const values = await expected(), digest = values.catalogDigest
    const [migration, version, assets, receipts, observations] = await Promise.all([
      pool.query("SELECT version,checksum FROM mhb_platform.schema_migrations ORDER BY version"),
      pool.query("SELECT count(*)::int AS count FROM mhb_platform.catalog_versions WHERE digest=$1", [digest]),
      pool.query("SELECT count(*)::int AS count FROM mhb_platform.asset_versions WHERE catalog_digest=$1", [digest]),
      pool.query("SELECT count(*)::int AS count FROM mhb_platform.ingest_receipts WHERE catalog_digest=$1", [digest]),
      pool.query("SELECT count(*)::int AS count FROM mhb_platform.observations WHERE catalog_digest=$1", [digest]),
    ])
    const result = { ...values, migrationChecksum: migration.rows[0]?.checksum, assets: assets.rows[0]?.count, receipts: receipts.rows[0]?.count, observations: observations.rows[0]?.count }
    if (migration.rows.length !== 1 || Number(version.rows[0]?.count) !== 1 || JSON.stringify(result) !== JSON.stringify(values)) throw new Error("migration or catalog readback mismatch")
    return result
  })()
  console.log(JSON.stringify({ operation: command, status: "PASS", result }))
} catch {
  console.error(JSON.stringify({ operation: command, status: "FAIL", reason: "Check the dedicated database, migration checksum and catalog; credentials are not logged." }))
  process.exitCode = 1
} finally { if (pool) await pool.end() }
