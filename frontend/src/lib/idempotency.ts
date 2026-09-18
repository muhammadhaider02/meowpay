"use client";

/**
 * Idempotency keys, owned by the intent rather than by a React component.
 *
 * The key used to live in a `useRef` inside each form, which tied its lifetime
 * to a mounted component. Anything that unmounted the form minted a new one:
 * switching between the send and top-up forms, the dashboard falling back to its
 * error screen, or a reload. That is a double spend, and it is the exact one the
 * header exists to prevent: an attempt whose response was lost, followed by a
 * retry the backend cannot recognise as a retry.
 *
 * So the key lives in `sessionStorage`, under one slot per kind of movement.
 * Surviving a reload is the point, not a side effect. It is scoped to the tab,
 * so a second tab is a second intent, which is correct: two tabs really are two
 * people as far as the sender can tell.
 *
 * It is rotated in exactly two places, and both mean "the intent this key stood
 * for is finished":
 *
 *   - a settlement, including a replay, because the movement is now on the ledger
 *   - `idempotency_key_reused`, where the backend is telling us this key already
 *     names a different movement and the remedy is a new one
 *
 * It is deliberately NOT rotated on any other failure. A rejected transfer does
 * not consume its key, so retrying the same intent after topping up has to reuse
 * it, and a request that timed out may well have settled.
 */

export type MovementSlot = "transfer" | "deposit";

const PREFIX = "meowpay:idempotency:";

/**
 * A tab-lifetime fallback for when `sessionStorage` is unavailable.
 *
 * Private browsing, blocked site data and sandboxed frames all make the
 * accessor throw rather than return null. Falling back to memory keeps the key
 * stable for as long as the page lives, which still covers the unmount cases
 * that motivated this file even though it no longer survives a reload.
 */
const fallback = new Map<string, string>();

function read(slot: string): string | null {
  try {
    return window.sessionStorage.getItem(slot);
  } catch {
    return fallback.get(slot) ?? null;
  }
}

function write(slot: string, value: string): void {
  try {
    window.sessionStorage.setItem(slot, value);
  } catch {
    fallback.set(slot, value);
  }
}

function clear(slot: string): void {
  try {
    window.sessionStorage.removeItem(slot);
  } catch {
    fallback.delete(slot);
  }
}

/**
 * Generated once per intent.
 *
 * `crypto.randomUUID` is 36 characters, comfortably inside the 8 to 255 the
 * database enforces in `ck_transfers_idempotency_key_shape`. It is absent on
 * insecure origins, which is every http host that is not localhost, so there is
 * a fallback rather than a white screen: a key only has to be unique per cat,
 * and these are never a secret.
 */
export function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

/** The key for this intent, minting one only if the slot is empty. */
export function keyFor(slot: MovementSlot): string {
  const name = PREFIX + slot;
  const existing = read(name);
  if (existing) return existing;

  const minted = newIdempotencyKey();
  write(name, minted);
  return minted;
}

/** The intent is over. The next submit is a new one and gets a new key. */
export function rotate(slot: MovementSlot): void {
  clear(PREFIX + slot);
}
