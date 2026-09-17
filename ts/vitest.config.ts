import { defineConfig } from "vitest/config"

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    globals: false,
    // Bounded default for the 2 CPU / 128 PID development worker.
    maxWorkers: 1,
    fileParallelism: false,
    testTimeout: 20_000
  }
})
