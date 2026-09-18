"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { ApiError, api } from "@/lib/api";
import { useWindowFocus } from "@/lib/focus";
import { getSupabase } from "@/lib/supabase";
import type { Me } from "@/lib/types";

type Status = "loading" | "ready" | "error";

interface Session {
  me: Me | null;
  status: Status;
  /** Fatal: there is no cat on screen and the page cannot be shown. */
  error: string | null;
  /** Non-fatal: a cat is on screen but the last refresh of it failed. */
  refreshError: string | null;
  /** True once a request has been slow enough to look like a cold start. */
  waking: boolean;
  refresh: () => Promise<void>;
}

/**
 * Resolves the signed-in cat, or sends the viewer where they need to go.
 *
 * Two gates, and they are different questions:
 *
 *   1. Is anyone signed in? `getClaims()` verifies the JWT locally against a
 *      cached JWKS. `getSession()` is not used for this, because it reads local
 *      storage without revalidating and its user object must not drive a
 *      redirect.
 *   2. Does that identity have a cat? Only the API knows. A valid token with no
 *      cat row is `403 cat_not_onboarded`, which is the signal to onboard.
 *
 * The second gate is why this is not middleware. `cat_not_onboarded` is
 * deliberately a 403 and not a 401: a 401 would mean re-authenticate, which
 * would mint an identical token and loop for ever.
 */
export function useSession(): Session {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [status, setStatus] = useState<Status>("loading");
  const [error, setError] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [waking, setWaking] = useState(false);

  // Every load carries a generation, and only the newest may commit. Two loads
  // really do overlap: a settlement refreshes while StrictMode's double mount
  // may still have one in flight, and without this the slower one wins and
  // paints a balance from before the movement. That is the same lie as reading
  // a replayed `balance_after`, which this file exists to avoid.
  const generation = useRef(0);

  const load = useCallback(
    async (background = false) => {
      const mine = ++generation.current;
      const current = (): boolean => mine === generation.current;

      if (!background) setError(null);
      setRefreshError(null);

      try {
        // The gate answers "is anyone signed in", which is a question for a
        // page with nothing on it yet. A background refresh already has a cat
        // rendered, and `api.me()` is authoritative on its own: it answers 401
        // if the token has gone bad, and `lib/api.ts` reads the token
        // immediately before the request rather than trusting a captured one.
        //
        // Skipping it on that path is not only a saved round trip. `getClaims`
        // verifies against a cached JWKS, and supabase-js refreshes the session
        // on the very event a focus refetch fires on, so the two contend and
        // this one loses. The symptom was a balance that stayed stale while the
        // statement, which goes straight to the API, came back current.
        //
        // Inside the try, not above it. This rejects when the JWKS cannot be
        // fetched, which is the `auth_unavailable` case; outside the try that
        // became an unhandled rejection and the page sat on "Loading" for ever
        // with no way back.
        if (!background) {
          const { data } = await getSupabase().auth.getClaims();
          if (!data?.claims) {
            router.replace("/sign-in");
            return;
          }
        }

        const cat = await api.me(() => {
          if (current()) setWaking(true);
        });
        if (!current()) return;

        setMe(cat);
        setStatus("ready");
      } catch (cause) {
        if (!current()) return;

        if (cause instanceof ApiError && cause.code === "cat_not_onboarded") {
          router.replace("/onboarding");
          return;
        }

        const message = cause instanceof Error ? cause.message : "Something went wrong.";

        // A refresh that fails after a movement settled must not take the page
        // down. The treats moved, the receipt is on screen, and replacing all of
        // it with an error would report a successful send as a failure and
        // invite the cat to send again.
        if (background && me) {
          setRefreshError(message);
          return;
        }

        // Anything else stays on screen. `auth_unavailable` in particular must
        // not sign the viewer out: a Supabase blip would bounce every signed-in
        // user to the login screen and make the blip worse.
        setError(message);
        setStatus("error");
      } finally {
        if (current()) setWaking(false);
      }
    },
    [router, me],
  );

  useEffect(() => {
    void load(false);
    // Deliberately once per mount, and deliberately not exhaustive. `load`
    // changes identity whenever `me` does, so depending on it here would
    // refetch the cat every time the cat landed. There is no eslint in this
    // project, so there is no disable comment either: a directive naming a rule
    // nothing runs is a note that looks like configuration.
  }, []);

  const refresh = useCallback(() => load(true), [load]);

  // A receiving cat has no way to learn that treats arrived, so the balance is
  // refetched whenever this window comes back to the front. Nothing runs until
  // the first load has produced a cat, because a background failure with none
  // on screen would be reported as fatal rather than as a stale figure.
  useWindowFocus(() => {
    if (me === null) return;
    void refresh();
  });

  return { me, status, error, refreshError, waking, refresh };
}

export async function signOut(): Promise<void> {
  await getSupabase().auth.signOut();
}
