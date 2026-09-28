import { readFile, writeFile } from "node:fs/promises"
import { resolve } from "node:path"
import { pathToFileURL } from "node:url"
import { createHash } from "node:crypto"
import { Either } from "effect"
import { decodeCatalog, propertyGraph } from "../dist/src/domain/PlatformGraph.js"

const root = process.argv[2]
if (!root) throw new Error("Usage: node scripts/check-usl-interop.mjs /path/to/built/USL [receipt.json]")
const adapterPath = resolve(root, "dist/src/integrations/property-graph.js")
const { adaptPropertyGraph } = await import(pathToFileURL(adapterPath).href)
const catalog = Either.getOrThrow(decodeCatalog(JSON.parse(await readFile(new URL("../config/platform-catalog.json", import.meta.url), "utf8"))))
const input = JSON.stringify(propertyGraph(catalog))
const result = adaptPropertyGraph(input, { namespace: "metahumotonic.platform" })
if (result._tag === "Left") throw result.left
const receipt = {
  schema: "metahumotonic/usl-interop-check@1", observedAt: new Date().toISOString(), status: "PASS",
  scope: "Native property-graph adaptation only. No resolver I/O, KG writes, authority admission or runtime conformance.",
  adapter: result.right.source.adapter, sourceDigest: result.right.source.digest,
  adapterFileSha256: createHash("sha256").update(await readFile(adapterPath)).digest("hex"),
  nodes: Object.keys(result.right.identities.resources).length,
  relationships: Object.keys(result.right.identities.links).length,
  graphSha256: createHash("sha256").update(input).digest("hex")
}
if (process.argv[3]) await writeFile(resolve(process.argv[3]), JSON.stringify(receipt, null, 2) + "\n", { flag: "wx" })
console.log(JSON.stringify(receipt, null, 2))
