import { Effect, Fiber, Redacted, TestClock, TestContext } from "effect"
import { afterEach, describe, expect, it, vi } from "vitest"
import { acceptsVerification, makeTurnstileVerifier, SITEVERIFY_URL,
  TurnstileUnavailable, TurnstileBadResponse, type TurnstileSettings } from "../src/ports/TurnstileVerifier.js"

const cfg: TurnstileSettings = {
  turnstileSecret: Redacted.make("fixture-only-not-a-real-credential"),
  turnstileHostname: "metahumotonic.com", turnstileAction: "feedback_submit", turnstileFailOpen:false
}
const valid = {success:true,hostname:cfg.turnstileHostname,action:cfg.turnstileAction}
afterEach(() => vi.unstubAllGlobals())
const verify = (overrides: Partial<TurnstileSettings> = {}) =>
  Effect.runPromise(makeTurnstileVerifier({...cfg,...overrides}).verify("fixture-token"))

describe("Turnstile verifier", () => {
  it.each([null, [], true, "true", {}, {success:"true"}, {success:false},
    {...valid,hostname:"wrong.example"}, {...valid,action:"wrong"}])(
    "rejects malformed or mismatched response %#", body => {
      expect(acceptsVerification(body,cfg)).toBe(false)
    })
  it("accepts only an explicit successful response matching configured context", () => {
    expect(acceptsVerification(valid,cfg)).toBe(true)
    expect(acceptsVerification({success:true},{...cfg,turnstileHostname:"",turnstileAction:""})).toBe(true)
  })
  it("uses fixed HTTPS POST, a cancellation signal, and no redirects", async () => {
    const upstream=vi.fn(async () => new Response(JSON.stringify(valid), {status:200}))
    vi.stubGlobal("fetch",upstream)
    expect(await verify()).toBe(true)
    const args=upstream.mock.calls[0] as unknown as [string,RequestInit]
    expect(args[0]).toBe(SITEVERIFY_URL)
    expect(args[1].method).toBe("POST")
    expect(args[1].redirect).toBe("error")
    expect(args[1].signal).toBeInstanceOf(AbortSignal)
    const payload=new URLSearchParams(String(args[1].body))
    expect(payload.get("response")).toBe("fixture-token")
    expect(payload.get("secret")).toBe(Redacted.value(cfg.turnstileSecret))
    expect(payload.has("remoteip")).toBe(false)
  })
  it("does not cache verification success for a single-use token", async () => {
    const upstream=vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(valid),{status:200}))
      .mockResolvedValueOnce(new Response('{"success":false}',{status:200}))
    vi.stubGlobal("fetch",upstream)
    expect(await verify()).toBe(true); expect(await verify()).toBe(false)
    expect(upstream).toHaveBeenCalledTimes(2)
  })
  it.each([400,401,500,503])("rejects HTTP %i even with fail-open", async status => {
    vi.stubGlobal("fetch",vi.fn(async () => new Response(JSON.stringify(valid),{status})))
    expect(await verify({turnstileFailOpen:true})).toBe(false)
  })
  it.each(["not JSON", "x".repeat(16385)])("rejects invalid or oversized body %#", async body => {
    vi.stubGlobal("fetch",vi.fn(async () => new Response(body,{status:200})))
    expect(await verify({turnstileFailOpen:true})).toBe(false)
  })
  it("defaults closed for network failure, with explicit transport-only opt-in", async () => {
    const upstream=vi.fn(async () => {throw new Error("fixture-network-failure")})
    vi.stubGlobal("fetch",upstream)
    expect(await verify()).toBe(false)
    expect(await verify({turnstileFailOpen:true})).toBe(true)
  })
  it("does not call transport for disabled, missing, or oversized token", async () => {
    const exchange=vi.fn(() => Effect.succeed(valid))
    expect(await Effect.runPromise(makeTurnstileVerifier({...cfg,turnstileSecret:Redacted.make("")},exchange).verify(""))).toBe(true)
    const enabled=makeTurnstileVerifier({...cfg,turnstileFailOpen:true},exchange)
    expect(await Effect.runPromise(enabled.verify(""))).toBe(false)
    expect(await Effect.runPromise(enabled.verify("x".repeat(2049)))).toBe(false)
    expect(exchange).not.toHaveBeenCalled()
  })
  it("maps explicit transport and malformed errors separately", async () => {
    const open={...cfg,turnstileFailOpen:true}
    expect(await Effect.runPromise(makeTurnstileVerifier(open,()=>Effect.fail(new TurnstileUnavailable())).verify("x"))).toBe(true)
    expect(await Effect.runPromise(makeTurnstileVerifier(open,()=>Effect.fail(new TurnstileBadResponse())).verify("x"))).toBe(false)
  })
  it("enforces the five-second deadline with a virtual clock", async () => {
    const test=Effect.gen(function*(){
      const fiber=yield* Effect.fork(makeTurnstileVerifier(cfg,()=>Effect.never).verify("x"))
      yield* TestClock.adjust("5 seconds")
      return yield* Fiber.join(fiber)
    }).pipe(Effect.provide(TestContext.TestContext))
    expect(await Effect.runPromise(test)).toBe(false)
  })
  it("aborts an outstanding fetch when the effect is interrupted", async () => {
    let aborted=false
    let markStarted: () => void = () => {}
    const started=new Promise<void>((resolve) => {markStarted=resolve})
    vi.stubGlobal("fetch",vi.fn((_url:unknown,options:RequestInit) => new Promise<Response>((_resolve,reject) => {
      options.signal!.addEventListener("abort",()=>{aborted=true;reject(new Error("cancelled fixture"))},{once:true})
      markStarted()
    })))
    const fiber=Effect.runFork(makeTurnstileVerifier(cfg).verify("fixture"))
    await started
    await Effect.runPromise(Fiber.interrupt(fiber))
    expect(aborted).toBe(true)
  })
})
