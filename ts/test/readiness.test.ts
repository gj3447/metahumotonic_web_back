import { HttpApiBuilder } from "@effect/platform"
import { ConfigProvider, Effect, Layer, Redacted } from "effect"
import { describe, expect, it } from "vitest"
import { configFromEnv, type AppConfig } from "../src/Config.js"
import { configOf, webHandlerLayer } from "../src/server/Composition.js"
import { PlatformInventoryLive } from "../src/ports/PlatformInventoryPostgres.js"

const config = Effect.runSync(configFromEnv.pipe(Effect.withConfigProvider(ConfigProvider.fromMap(new Map()))))
describe("native dependency readiness", () => {
  it.each([
    ["required but missing URL", { MHB_PLATFORM_DATABASE_REQUIRED: "true" }],
    ["configured unavailable PostgreSQL", { MHB_PLATFORM_DATABASE_URL: "postgresql://unused:unused@127.0.0.1:1/platform_test" }]
  ])("keeps PostgreSQL %s fail-closed without affecting public learning", async (_label, env) => {
    const inventoryStore = PlatformInventoryLive.pipe(Layer.provide(Layer.setConfigProvider(ConfigProvider.fromMap(new Map(Object.entries(env))))))
    const web = HttpApiBuilder.toWebHandler(webHandlerLayer({ config: configOf(config), inventoryStore }))
    try {
      const response = await web.handler(new Request("http://localhost/ready"))
      expect(response.status).toBe(503)
      expect(await response.json()).toMatchObject({ platform_postgres_required: true, platform_postgres_live: false })
      expect((await web.handler(new Request("http://localhost/health"))).status).toBe(200)
      expect((await web.handler(new Request("http://localhost/api/public/v1/hub"))).status).toBe(200)
    } finally { await web.dispose() }
  })
  it.each([
    ["durable feedback without Mongo", { feedbackRequireDurable: true }, "mongo"],
    ["configured Mongo that cannot answer", { mongoUri: Redacted.make("mongodb://127.0.0.1:1") }, "mongo"],
    ["configured Redis that cannot answer", { redisUrl: Redacted.make("redis://127.0.0.1:1") }, "redis"]
  ] as const)("refuses readiness for %s while retaining process liveness", async (_name, patch, dependency) => {
    const web = HttpApiBuilder.toWebHandler(webHandlerLayer({ config: configOf({ ...config, ...patch } as AppConfig) }))
    try {
      const response = await web.handler(new Request("http://localhost/ready"))
      expect(response.status).toBe(503)
      expect(await response.json()).toMatchObject({ status: "not_ready", degraded: true, [`${dependency}_required`]: true, [`${dependency}_live`]: false })
      expect((await web.handler(new Request("http://localhost/health"))).status).toBe(200)
      expect((await web.handler(new Request("http://localhost/api/public/v1/hub"))).status).toBe(200)
    } finally { await web.dispose() }
  }, 10000)
  it("allows the explicitly unconfigured local-development profile", async () => {
    const web = HttpApiBuilder.toWebHandler(webHandlerLayer({ config: configOf(config) }))
    try {
      const response = await web.handler(new Request("http://localhost/ready"))
      expect(response.status).toBe(200)
      expect(await response.json()).toMatchObject({ mongo_required: false, mongo_live: false, redis_required: false, redis_live: false })
    } finally { await web.dispose() }
  })
})
