import { readFile, writeFile } from "node:fs/promises"
import { Either } from "effect"
import { decodeCatalog } from "../dist/src/domain/PlatformGraph.js"
import { platformJsonLd } from "../dist/src/domain/PlatformInventory.js"

if (!process.argv[2]) throw new Error("Usage: node scripts/export-platform.mjs NEW_OUTPUT.jsonld")
const catalog = Either.getOrThrow(decodeCatalog(JSON.parse(await readFile(new URL("../config/platform-catalog.json", import.meta.url), "utf8"))))
await writeFile(process.argv[2], JSON.stringify(platformJsonLd(catalog), null, 2) + "\n", { flag: "wx" })
console.log(JSON.stringify({ format: "jsonld-1.1", nodes: catalog.nodes.length, relationships: catalog.edges.length, observations: catalog.observations?.length ?? 0 }))
