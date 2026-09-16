"use client";

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { Entry } from "@/lib/types";

/**
 * The signed-in cat's ledger lines, newest first.
 *
 * Paginated by keyset: `next_before` comes back from the API and is passed
 * straight back, never constructed here. That matters more than the index does,
 * because this is a live money feed and an offset would skip or repeat rows
 * whenever a movement landed between two requests.
 *
 * `reloadToken` changes whenever a movement settles on this page, which resets
 * the list to the first page rather than appending to a stale one.
 */
export function Statement({ reloadToken }: { reloadToken: number }) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadFirstPage = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await api.entries();
      setEntries(page.entries);
      setCursor(page.next_before);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load the statement.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadFirstPage();
  }, [loadFirstPage, reloadToken]);

  async function loadMore() {
    if (cursor == null) return;
    setLoading(true);
    try {
      const page = await api.entries(cursor);
      setEntries((current) => [...current, ...page.entries]);
      setCursor(page.next_before);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load more.");
    } finally {
      setLoading(false);
    }
  }

  if (error) {
    return (
      <div className="notice error" role="alert">
        {error}
      </div>
    );
  }

  if (!entries.length) {
    return <p className="muted">{loading ? "Loading..." : "Nothing here yet."}</p>;
  }

  return (
    <>
      <ul className="entries">
        {entries.map((entry) => {
          const credit = entry.amount > 0;
          return (
            <li key={entry.id}>
              <span className="stack">
                <span>
                  {entry.kind === "deposit"
                    ? "Top up"
                    : credit
                      ? `From ${entry.counterparty_display_name}`
                      : `To ${entry.counterparty_display_name}`}
                </span>
                <span className="muted">
                  {new Date(entry.created_at).toLocaleString()}
                </span>
              </span>
              <span className="stack" style={{ textAlign: "right" }}>
                <span className={credit ? "amount credit" : "amount debit"}>
                  {credit ? "+" : ""}
                  {entry.amount.toLocaleString()}
                </span>
                <span className="muted">{entry.balance_after.toLocaleString()} after</span>
              </span>
            </li>
          );
        })}
      </ul>

      {cursor != null ? (
        <p style={{ marginTop: 14, marginBottom: 0 }}>
          <button type="button" className="secondary" onClick={loadMore} disabled={loading}>
            {loading ? "Loading..." : "Load older"}
          </button>
        </p>
      ) : null}
    </>
  );
}
