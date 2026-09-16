/**
 * The key store, whose whole job is to outlive a React component.
 *
 * The bug this file exists to prevent: a key held in a `useRef` is minted again
 * whenever the form unmounts, and switching between the send and top-up tabs
 * unmounts it. An attempt whose response was lost, followed by a tab switch and
 * a resubmit, then reaches the backend under a key it has never seen and settles
 * a second time.
 *
 * Runs in the node environment with a hand-built `Storage`, rather than pulling
 * in a DOM implementation for one interface with three methods. The stub is
 * faithful on the parts that matter: string values, null for a miss, and an
 * accessor that can throw the way a blocked one really does.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function inMemoryStorage(): Storage {
  const entries = new Map<string, string>();
  return {
    getItem: (key: string) => entries.get(key) ?? null,
    setItem: (key: string, value: string) => void entries.set(key, value),
    removeItem: (key: string) => void entries.delete(key),
    clear: () => entries.clear(),
    key: () => null,
    length: 0,
  } as unknown as Storage;
}

/** A store that denies everything, as private browsing does. */
function deniedStorage(): Storage {
  const deny = () => {
    throw new Error("The operation is insecure.");
  };
  return { getItem: deny, setItem: deny, removeItem: deny } as unknown as Storage;
}

async function freshModule(storage: Storage) {
  vi.stubGlobal("window", { sessionStorage: storage });
  vi.resetModules();
  return await import("@/lib/idempotency");
}

beforeEach(() => {
  vi.stubGlobal("window", { sessionStorage: inMemoryStorage() });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("a key's lifetime", () => {
  it("is the same key on every read until something rotates it", async () => {
    // Standing in for a tab switch: the component is gone and back, and the
    // intent it was serving is not finished.
    const { keyFor } = await import("@/lib/idempotency");

    const first = keyFor("transfer");

    expect(keyFor("transfer")).toBe(first);
    expect(keyFor("transfer")).toBe(first);
  });

  it("survives the module being reloaded, which is what a page reload is", async () => {
    const storage = inMemoryStorage();
    const before = (await freshModule(storage)).keyFor("transfer");

    const reloaded = await freshModule(storage);

    expect(reloaded.keyFor("transfer")).toBe(before);
  });

  it("changes once the intent is over", async () => {
    const { keyFor, rotate } = await import("@/lib/idempotency");

    const first = keyFor("transfer");
    rotate("transfer");

    expect(keyFor("transfer")).not.toBe(first);
  });

  it("keeps sending and topping up on separate keys", async () => {
    // They are different movements. Sharing one key would make a top up look
    // like a replay of a send.
    const { keyFor } = await import("@/lib/idempotency");

    expect(keyFor("transfer")).not.toBe(keyFor("deposit"));
  });

  it("rotating one does not disturb the other", async () => {
    const { keyFor, rotate } = await import("@/lib/idempotency");
    const deposit = keyFor("deposit");
    keyFor("transfer");

    rotate("transfer");

    expect(keyFor("deposit")).toBe(deposit);
  });
});

describe("the generated key", () => {
  it("fits the length the database enforces", async () => {
    // ck_transfers_idempotency_key_shape is length BETWEEN 8 AND 255. A key
    // outside it is a 422 the cat can do nothing about.
    const { newIdempotencyKey } = await import("@/lib/idempotency");
    const key = newIdempotencyKey();

    expect(key.length).toBeGreaterThanOrEqual(8);
    expect(key.length).toBeLessThanOrEqual(255);
  });

  it("does not repeat", async () => {
    const { newIdempotencyKey } = await import("@/lib/idempotency");

    expect(new Set(Array.from({ length: 50 }, newIdempotencyKey)).size).toBe(50);
  });

  it("still produces a usable key where crypto.randomUUID is missing", async () => {
    // Absent on every insecure origin that is not localhost. A throw here would
    // white-screen the dashboard rather than degrade.
    const { newIdempotencyKey } = await import("@/lib/idempotency");
    vi.stubGlobal("crypto", {});

    const key = newIdempotencyKey();

    expect(key.length).toBeGreaterThanOrEqual(8);
    expect(key).not.toBe(newIdempotencyKey());
  });
});

describe("when the store is unavailable", () => {
  it("still holds a key steady for the life of the page", async () => {
    // Losing persistence across a reload is acceptable here. Minting a new key
    // on every read is not, because that is the double spend again.
    const { keyFor } = await freshModule(deniedStorage());

    const first = keyFor("transfer");

    expect(keyFor("transfer")).toBe(first);
  });

  it("can still be rotated when the intent is over", async () => {
    const { keyFor, rotate } = await freshModule(deniedStorage());

    const first = keyFor("deposit");
    rotate("deposit");

    expect(keyFor("deposit")).not.toBe(first);
  });
});
