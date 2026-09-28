import { Context, Effect } from "effect"
import type { PlatformCatalog } from "../domain/PlatformGraph.js"
import type { ObservationBatch, IngestReceipt, ObservationHistory } from "../domain/ObservationIngest.js"
import { BadRequest, Conflict, NotFound, Unavailable } from "../domain/Errors.js"

export interface CatalogView {
  readonly catalog: PlatformCatalog
  readonly digest: string
  readonly definitionDigest: string
  readonly source: "snapshot" | "postgres"
}
export interface PlatformInventoryStore {
  readonly read: Effect.Effect<CatalogView, Unavailable>
  readonly readiness: Effect.Effect<{ readonly platform_postgres_required: boolean; readonly platform_postgres_live: boolean }>
  readonly append: (batch: ObservationBatch, now: number) => Effect.Effect<IngestReceipt & { readonly replayed: boolean }, BadRequest | Conflict | Unavailable>
  readonly receipt: (id: string) => Effect.Effect<IngestReceipt, NotFound | Unavailable>
  readonly history: (query: { readonly subjectId?: string | undefined; readonly before?: number | undefined; readonly limit?: number | undefined }) => Effect.Effect<ObservationHistory, Unavailable>
}
export class PlatformInventoryStoreTag extends Context.Tag("PlatformInventoryStore")<PlatformInventoryStoreTag, PlatformInventoryStore>() {}
