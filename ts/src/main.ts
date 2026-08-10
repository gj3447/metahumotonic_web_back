/**
 * Entrypoint.
 *
 *   npm run dev     # watch mode
 *   npm start       # once
 *
 * The Python equivalent is `uvicorn app.main:app`. The difference worth
 * noticing: nothing here is a global. `HttpLive` is a value describing the
 * whole service — server, handlers, ports, config — and `NodeRuntime.runMain`
 * runs it with structured shutdown, so SIGINT closes the Neo4j driver through
 * the same scope that opened it.
 */
import { NodeHttpServer, NodeRuntime } from "@effect/platform-node"
import { Config, Effect, Layer, Logger } from "effect"
import { createServer } from "node:http"
import { serveLayer } from "./server/Composition.js"

const ServerLive = Layer.unwrapEffect(
  Effect.gen(function* () {
    const port = yield* Config.integer("MHB_PORT").pipe(Config.withDefault(8000))
    const host = yield* Config.string("MHB_HOST").pipe(Config.withDefault("0.0.0.0"))
    return NodeHttpServer.layer(createServer, { port, host })
  })
)

/** `MHB_LOG_JSON=true` (the default) mirrors the structlog output shape. */
const LoggerLive = Layer.unwrapEffect(
  Effect.gen(function* () {
    const json = yield* Config.boolean("MHB_LOG_JSON").pipe(Config.withDefault(true))
    return json ? Logger.replace(Logger.defaultLogger, Logger.jsonLogger) : Layer.empty
  })
)

const MainLive = serveLayer().pipe(Layer.provide(ServerLive), Layer.provide(LoggerLive))

NodeRuntime.runMain(Layer.launch(MainLive))
