import { cp } from "node:fs/promises"
await cp(new URL("../config/", import.meta.url), new URL("../dist/config/", import.meta.url), { recursive: true })
