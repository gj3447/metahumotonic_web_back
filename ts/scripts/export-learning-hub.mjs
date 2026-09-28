import { readFile, writeFile } from "node:fs/promises"
import { Either } from "effect"
import { resolve } from "node:path"
import { decodeHub, projectHub } from "../dist/src/domain/LearningHub.js"

const output = process.argv[2]
if (!output) throw new Error("Usage after build: node scripts/export-learning-hub.mjs /path/to/frontend/src/data/learning-hub.json [--check]")
const source = JSON.parse(await readFile(new URL("../config/learning-hub.json", import.meta.url), "utf8"))
const artifact = JSON.stringify(projectHub(Either.getOrThrow(decodeHub(source))), null, 2) + "\n"
if (process.argv[3] === "--check") {
  if (await readFile(resolve(output), "utf8") !== artifact) throw new Error("frontend learning hub differs from validated backend publication")
  console.log("learning hub publication matches")
} else {
  await writeFile(resolve(output), artifact)
  console.log(`exported validated public learning hub to ${resolve(output)}`)
}
