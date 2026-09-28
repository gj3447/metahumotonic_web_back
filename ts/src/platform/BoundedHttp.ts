export class BodyLimitError extends Error {}

/** A budget applies while consuming the body as well as while opening the socket. */
export const readBounded = async (body: ReadableStream<Uint8Array> | null, maximum: number): Promise<Uint8Array> => {
  if (body === null) return new Uint8Array()
  const reader = body.getReader()
  const chunks: Uint8Array[] = []
  let length = 0
  try {
    for (;;) {
      const next = await reader.read()
      if (next.done) break
      length += next.value.byteLength
      if (length > maximum) throw new BodyLimitError("body limit exceeded")
      chunks.push(next.value)
    }
  } finally { await reader.cancel().catch(() => undefined); reader.releaseLock() }
  return Buffer.concat(chunks, length)
}

/** Configured endpoints only. No redirects, caller credentials, or retry of an uncertain effect. */
export const boundedFetch = (timeoutMs: number, maximum: number, parentSignal?: AbortSignal): typeof fetch =>
  async (input, init) => {
    const signals = [AbortSignal.timeout(timeoutMs), ...(parentSignal ? [parentSignal] : []), ...(init?.signal ? [init.signal] : [])]
    const response = await fetch(input, { ...init, redirect: "error", signal: AbortSignal.any(signals) })
    const bytes = await readBounded(response.body, maximum)
    const headers = new Headers(response.headers)
    // fetch decompresses; do not present the old transfer metadata with decoded bytes.
    headers.delete("content-encoding"); headers.delete("content-length")
    return new Response([204, 205, 304].includes(response.status) ? null : Buffer.from(bytes), { status: response.status, statusText: response.statusText, headers })
  }
