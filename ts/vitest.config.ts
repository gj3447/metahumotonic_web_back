import { defineConfig } from "vitest/config"

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    globals: false,
    maxWorkers: 1,
    fileParallelism: false,
    testTimeout: 20_000
  }
})
