import { readFile, writeFile } from "node:fs/promises"
import { Either } from "effect"
import { decodeReality, realityJsonLd } from "../dist/src/domain/BackendReality.js"
import { decodeCatalog } from "../dist/src/domain/PlatformGraph.js"

const destination = process.argv[2]
if (!destination) throw new Error("usage: node scripts/export-backend-reality.mjs OUTPUT.jsonld [EVALUATED_AT_ISO]")
const evaluatedAt = process.argv[3] ? Date.parse(process.argv[3]) : Date.now()
if (!Number.isFinite(evaluatedAt)) throw new Error("invalid evaluation time")
const readJson = async (relative) => JSON.parse(await readFile(new URL(relative, import.meta.url), "utf8"))
const catalog = Either.getOrThrow(decodeCatalog(await readJson("../config/platform-catalog.json")))
const reality = Either.getOrThrow(decodeReality(await readJson("../config/backend-reality.json"), catalog))
await writeFile(destination, JSON.stringify(realityJsonLd(reality, evaluatedAt), null, 2) + "\n")
