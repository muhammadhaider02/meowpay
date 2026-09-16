import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// `node` rather than a DOM environment on purpose. Nothing under test touches
// the document: the request client is where every rule about retries, statuses
// and idempotency lives, and keeping it renderer-free is what lets it be tested
// without a browser.
export default defineConfig({
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
