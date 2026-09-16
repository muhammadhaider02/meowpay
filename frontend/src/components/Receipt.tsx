"use client";

import type { Movement } from "@/lib/types";

/**
 * The receipt for a settled movement.
 *
 * `balance_after` is shown as "balance at the time" rather than "your balance"
 * on purpose. On a replay it is the value from the original ledger line, which
 * may be weeks old, and the page reads the live balance from `GET /api/v1/me`
 * separately.
 */
export function Receipt({ movement, onDone }: { movement: Movement; onDone: () => void }) {
  const settled = new Date(movement.created_at);

  return (
    <div className="receipt">
      <h2>
        {movement.kind === "deposit" ? "Topped up" : "Treats sent"}
        {movement.replayed ? " (already done)" : ""}
      </h2>

      {movement.replayed ? (
        <p className="muted">
          This request had already been settled, so nothing moved a second time. The figures
          below are from when it originally settled.
        </p>
      ) : null}

      <dl>
        <dt>Amount</dt>
        <dd>{movement.amount.toLocaleString()} treats</dd>

        <dt>Balance at the time</dt>
        <dd>{movement.balance_after.toLocaleString()}</dd>

        <dt>Settled</dt>
        <dd>{settled.toLocaleString()}</dd>

        <dt>Reference</dt>
        <dd>{movement.id}</dd>
      </dl>

      <p style={{ marginTop: 14, marginBottom: 0 }}>
        <button type="button" className="secondary" onClick={onDone}>
          Done
        </button>
      </p>
    </div>
  );
}
