import { readFile } from "node:fs/promises"
import { createPlatformPool, importPlatformCatalog, migratePlatform } from "../dist/src/ports/PlatformInventoryPostgres.js"

// Explicit admin operation. No .env loading, automatic production discovery or DDL on HTTP startup.
const command = process.argv[2]
if (!["migrate", "import"].includes(command) || !process.env.MHB_PLATFORM_DATABASE_URL) {
  throw new Error("Set MHB_PLATFORM_DATABASE_URL explicitly; usage: platform-db.mjs migrate | import [catalog.json]")
}
let pool
try {
  pool = createPlatformPool(process.env.MHB_PLATFORM_DATABASE_URL)
  const result = command === "migrate" ? await migratePlatform(pool) : await importPlatformCatalog(pool,
    JSON.parse(await readFile(process.argv[3] || new URL("../dist/config/platform-catalog.json", import.meta.url), "utf8")))
  console.log(JSON.stringify({ operation: command, status: "PASS", result }))
} catch {
  console.error(JSON.stringify({ operation: command, status: "FAIL", reason: "Check the dedicated database, migration checksum and catalog; credentials are not logged." }))
  process.exitCode = 1
} finally { if (pool) await pool.end() }
