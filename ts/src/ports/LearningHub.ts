import { Context, Effect, Either, Layer } from "effect"
import { readFile } from "node:fs/promises"
import { decodeHub, projectHub } from "../domain/LearningHub.js"

export class LearningHubTag extends Context.Tag("LearningHub")<LearningHubTag, ReturnType<typeof projectHub>>() {}
export const LearningHubLive = Layer.effect(LearningHubTag, Effect.tryPromise(async () => {
  // A release-owned public file, independent of private KG and platform configuration.
  const raw = await readFile(new URL("../../config/learning-hub.json", import.meta.url), "utf8")
  if (Buffer.byteLength(raw) > 1_048_576) throw new Error("public learning hub exceeds its publication budget")
  return projectHub(Either.getOrThrow(decodeHub(JSON.parse(raw))))
}).pipe(Effect.orDie))
