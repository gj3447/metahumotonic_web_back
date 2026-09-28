import { readFile } from "node:fs/promises"
import { createPlatformPool, importPlatformCatalog, migratePlatform } from "../dist/src/ports/PlatformInventoryPostgres.js"
import { contentDigest } from "../dist/src/platform/Digest.js"

// Explicit admin operation. No .env loading, automatic production discovery or DDL on HTTP startup.
const command = process.argv[2]
if (!["migrate", "import", "readback"].includes(command) || !process.env.MHB_PLATFORM_DATABASE_URL) {
  throw new Error("Set MHB_PLATFORM_DATABASE_URL explicitly; usage: platform-db.mjs migrate | import | readback [catalog.json]")
}
let pool
try {
  pool = createPlatformPool(process.env.MHB_PLATFORM_DATABASE_URL)
  const catalog = command === "migrate" ? null : JSON.parse(await readFile(process.argv[3] || new URL("../dist/config/platform-catalog.json", import.meta.url), "utf8"))
  const result = command === "migrate" ? await migratePlatform(pool) : command === "import" ? await importPlatformCatalog(pool, catalog) : await (async () => {
    const digest = contentDigest(catalog)
    const [migration, version, assets, receipts, observations] = await Promise.all([
      pool.query("SELECT version,checksum FROM mhb_platform.schema_migrations ORDER BY version"),
      pool.query("SELECT count(*)::int AS count FROM mhb_platform.catalog_versions WHERE digest=$1", [digest]),
      pool.query("SELECT count(*)::int AS count FROM mhb_platform.asset_versions WHERE catalog_digest=$1", [digest]),
      pool.query("SELECT count(*)::int AS count FROM mhb_platform.ingest_receipts WHERE catalog_digest=$1", [digest]),
      pool.query("SELECT count(*)::int AS count FROM mhb_platform.observations WHERE catalog_digest=$1", [digest]),
    ])
    if (migration.rows.length !== 1 || Number(version.rows[0]?.count) !== 1) throw new Error("migration or catalog digest is not present")
    return { catalogDigest: digest, migration: migration.rows[0], assets: assets.rows[0]?.count, receipts: receipts.rows[0]?.count, observations: observations.rows[0]?.count }
  })()
  console.log(JSON.stringify({ operation: command, status: "PASS", result }))
} catch {
  console.error(JSON.stringify({ operation: command, status: "FAIL", reason: "Check the dedicated database, migration checksum and catalog; credentials are not logged." }))
  process.exitCode = 1
} finally { if (pool) await pool.end() }
