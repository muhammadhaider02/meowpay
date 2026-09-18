"use client";

import { useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { keyFor, rotate } from "@/lib/idempotency";
import { MAX_AMOUNT, type Movement } from "@/lib/types";

/**
 * Top up from the treasury.
 *
 * There is no recipient field, and there is no payment step. A deposit credits
 * the token holder, and this endpoint stands in for a payment rail that is out
 * of scope. A fake card form would be a worse kind of dishonest than saying so.
 *
 * Same key ownership as SendForm, and for the same reason: the key belongs to
 * the intent and outlives this component.
 */
export function DepositForm({ onSettled }: { onSettled: (movement: Movement) => void }) {
  const [amount, setAmount] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const inFlight = useRef(false);

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
      const movement = await api.deposit(treats, keyFor("deposit"));
      rotate("deposit");
      setAmount("");
      onSettled(movement);
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === "idempotency_key_reused") {
        rotate("deposit");
        setError(
          "An earlier top up used this reference for a different amount. " +
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

      <p className="muted" style={{ marginTop: 0 }}>
        Stands in for the payment rail a human would use. The treats come from
        the treasury, whose balance goes negative by the same amount.
      </p>

      <div className="field">
        <label htmlFor="deposit-amount">Treats to add</label>
        <input
          id="deposit-amount"
          inputMode="numeric"
          required
          value={amount}
          onChange={(event) => setAmount(event.target.value)}
          placeholder="500"
        />
      </div>

      <button type="submit" disabled={busy}>
        {busy ? "Topping up..." : "Top up"}
      </button>
    </form>
  );
}
