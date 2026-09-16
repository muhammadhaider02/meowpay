"use client";

import { useEffect, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { keyFor, rotate } from "@/lib/idempotency";
import { MAX_AMOUNT, type Cat, type Movement } from "@/lib/types";

/**
 * Send treats to another cat.
 *
 * The idempotency key is deliberately not held in this component. It is owned
 * by `lib/idempotency`, because a key tied to a mounted component is minted
 * afresh by anything that unmounts one, and switching to the top-up tab does
 * exactly that. See that file for why the lifetime matters.
 */
export function SendForm({ onSettled }: { onSettled: (movement: Movement) => void }) {
  const [directory, setDirectory] = useState<Cat[] | null>(null);
  const [toHandle, setToHandle] = useState("");
  const [amount, setAmount] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // The disabled attribute is what stops a second click in practice, but it
  // relies on React having flushed the state update first. This does not.
  const inFlight = useRef(false);

  useEffect(() => {
    void (async () => {
      try {
        setDirectory(await api.directory());
      } catch {
        // A picker that cannot load is not fatal. The handle can still be typed.
        setDirectory([]);
      }
    })();
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (inFlight.current) return;
    setError(null);

    const treats = Number(amount);
    if (!Number.isInteger(treats) || treats < 1) {
      setError("Amount must be a whole number of treats, at least 1.");
      return;
    }
    if (treats > MAX_AMOUNT) {
      setError(`That is more than the ledger allows, which is ${MAX_AMOUNT.toLocaleString()}.`);
      return;
    }

    inFlight.current = true;
    setBusy(true);
    try {
      const movement = await api.transfer(toHandle.trim(), treats, keyFor("transfer"));

      // Settled, replay included, so this intent is over and the next send is a
      // new one. This is the only success path that rotates.
      rotate("transfer");
      setAmount("");
      onSettled(movement);
    } catch (cause) {
      // The backend is telling us this key already names a different movement,
      // and its remedy is a new key. Without rotating here the form would refuse
      // every subsequent send with the same error for the life of the tab.
      if (cause instanceof ApiError && cause.code === "idempotency_key_reused") {
        rotate("transfer");
        setError(
          "An earlier send used this reference for a different amount or recipient. " +
            "Check your statement, then try again.",
        );
      } else {
        setError(cause instanceof Error ? cause.message : "Something went wrong.");
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit}>
      {error ? (
        <div className="notice error" role="alert">
          {error}
        </div>
      ) : null}

      <div className="row">
        <div className="field">
          <label htmlFor="to-handle">To</label>
          <input
            id="to-handle"
            required
            list="cat-directory"
            value={toHandle}
            onChange={(event) => setToHandle(event.target.value)}
            placeholder="milo"
          />
          <datalist id="cat-directory">
            {(directory ?? []).map((cat) => (
              <option key={cat.id} value={cat.handle}>
                {cat.display_name}
              </option>
            ))}
          </datalist>
        </div>

        <div className="field">
          <label htmlFor="send-amount">Treats</label>
          <input
            id="send-amount"
            inputMode="numeric"
            required
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
            placeholder="120"
          />
        </div>
      </div>

      <button type="submit" disabled={busy}>
        {busy ? "Sending..." : "Send treats"}
      </button>
    </form>
  );
}
