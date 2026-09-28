import { createHash } from "node:crypto"

/** JSONB may reorder object keys. Digests depend on values, never key order. */
export const canonicalJson = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`
  if (value !== null && typeof value === "object") return `{${Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)
    .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`).join(",")}}`
  const encoded = JSON.stringify(value)
  if (encoded === undefined) throw new Error("value is not JSON")
  return encoded
}
export const contentDigest = (value: unknown) => createHash("sha256").update(canonicalJson(value)).digest("hex")
