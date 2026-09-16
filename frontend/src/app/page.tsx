"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { DepositForm } from "@/components/DepositForm";
import { Receipt } from "@/components/Receipt";
import { SendForm } from "@/components/SendForm";
import { Statement } from "@/components/Statement";
import { signOut, useSession } from "@/lib/session";
import type { Movement } from "@/lib/types";

export default function DashboardPage() {
  const router = useRouter();
  const { me, status, error, refreshError, waking, refresh } = useSession();
  const [tab, setTab] = useState<"send" | "deposit">("send");
  const [receipt, setReceipt] = useState<Movement | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  async function leave() {
    await signOut();
    router.replace("/sign-in");
  }

  /**
   * Called when a movement settles.
   *
   * The balance is refetched rather than taken from `movement.balance_after`.
   * That field is the balance the instant the movement settled, and on a replay
   * it is a historical value, so painting it as the current balance would make
   * a repeated request look like the balance had gone backwards.
   */
  function settled(movement: Movement) {
    setReceipt(movement);
    setReloadToken((token) => token + 1);
    void refresh();
  }

  if (status === "loading") {
    return (
      <main>
        <p className="muted">
          {waking ? "Waking the API up, this takes about a minute on a cold start." : "Loading..."}
        </p>
      </main>
    );
  }

  if (status === "error" || !me) {
    return (
      <main>
        <div className="notice error" role="alert">
          {error ?? "Could not load your account."}
        </div>
        <div className="row">
          <button type="button" onClick={() => void refresh()}>
            Try again
          </button>
          {/* A rejected token cannot be recovered by retrying, so the way out
              has to be on this screen rather than only on the dashboard the
              viewer can no longer reach. */}
          <button type="button" className="secondary" onClick={() => void leave()}>
            Sign out
          </button>
        </div>
      </main>
    );
  }

  return (
    <main>
      <div className="masthead">
        <span className="brand">MeowPay</span>
        <span className="muted">
          {me.display_name} @{me.handle}{" "}
          <button type="button" className="link" onClick={() => void leave()}>
            Sign out
          </button>
        </span>
      </div>

      <div className="card">
        <span className="muted">Balance</span>
        <p className="balance">{me.balance.toLocaleString()}</p>
        <span className="muted">treats</span>
      </div>

      {/* Non-fatal. The movement settled and the receipt below is real; only
          the balance above may be out of date. */}
      {refreshError ? (
        <div className="notice info" role="status">
          The balance could not be refreshed just now, so it may be out of date.{" "}
          <button type="button" className="link" onClick={() => void refresh()}>
            Refresh
          </button>
        </div>
      ) : null}

      {receipt ? <Receipt movement={receipt} onDone={() => setReceipt(null)} /> : null}

      <div className="card">
        <div className="tabs">
          <button type="button" aria-pressed={tab === "send"} onClick={() => setTab("send")}>
            Send treats
          </button>
          <button
            type="button"
            aria-pressed={tab === "deposit"}
            onClick={() => setTab("deposit")}
          >
            Top up
          </button>
        </div>

        {tab === "send" ? (
          <SendForm onSettled={settled} />
        ) : (
          <DepositForm onSettled={settled} />
        )}
      </div>

      <div className="card">
        <h2>Statement</h2>
        <Statement reloadToken={reloadToken} />
      </div>
    </main>
  );
}
