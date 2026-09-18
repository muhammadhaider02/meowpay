"use client";

import { useEffect, useRef } from "react";

/**
 * Run `handler` when this window comes back to the front.
 *
 * Nothing pushes to this app. The ledger tables sit in a private schema
 * PostgREST does not expose, with row level security and no policies, so a
 * browser has no subscription it could open without undoing that. Polling would
 * buy a fake liveness at the cost of a request every few seconds against a free
 * tier that sleeps. Refetching on focus is honest about what it is: a figure
 * only has to be current at the moment someone is looking at it, and two
 * browsers side by side is how a transfer is actually watched.
 *
 * The handler is held in a ref so a caller may pass a fresh closure on every
 * render without the listeners being torn down and added again each time.
 */
export function useWindowFocus(handler: () => void): void {
  const latest = useRef(handler);
  latest.current = handler;

  useEffect(() => {
    const wake = (): void => {
      // `visibilitychange` fires on the way out as well as on the way back.
      if (document.visibilityState === "hidden") return;
      latest.current();
    };

    window.addEventListener("focus", wake);
    document.addEventListener("visibilitychange", wake);
    return () => {
      window.removeEventListener("focus", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }, []);
}
