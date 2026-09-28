import { readFileSync } from "node:fs"
import { Either } from "effect"
import { decodeReality } from "../domain/BackendReality.js"
import { decodeCatalog } from "../domain/PlatformGraph.js"
import { contentDigest } from "./Digest.js"

const catalog = Either.getOrThrow(decodeCatalog(JSON.parse(readFileSync(new URL("../../config/platform-catalog.json", import.meta.url), "utf8"))))
export const backendReality = Either.getOrThrow(decodeReality(
  JSON.parse(readFileSync(new URL("../../config/backend-reality.json", import.meta.url), "utf8")), catalog))
export const backendRealityDigest = contentDigest(backendReality)
