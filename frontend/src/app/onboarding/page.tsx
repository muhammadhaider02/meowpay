"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { ApiError, api } from "@/lib/api";
import { getSupabase } from "@/lib/supabase";

/**
 * Claim a handle for the signed-in identity.
 *
 * Reached from the session gate on a `403 cat_not_onboarded`, which is what a
 * verified token with no cat row returns. The endpoint is safe to call on every
 * load: the same identity asking for the same handle is a 200, not a 409.
 */
export default function OnboardingPage() {
  const router = useRouter();
  const [handle, setHandle] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    void (async () => {
      const { data } = await getSupabase().auth.getClaims();
      if (!data?.claims) {
        router.replace("/sign-in");
        return;
      }
      setChecked(true);
    })();
  }, [router]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);

    try {
      await api.onboard(handle.trim(), displayName.trim() || handle.trim());
      router.replace("/");
    } catch (cause) {
      // This identity already owns a cat under a different handle. Onboarding
      // is not a rename endpoint, so the right move is to go and use it.
      if (cause instanceof ApiError && cause.code === "cat_already_exists") {
        router.replace("/");
        return;
      }
      setError(cause instanceof Error ? cause.message : "Something went wrong.");
      setBusy(false);
    }
  }

  if (!checked) return null;

  return (
    <main>
      <div className="center">
        <div className="masthead">
          <span className="brand">MeowPay</span>
        </div>

        <div className="card">
          <h1>Pick a handle</h1>
          <p className="muted">
            This is how other cats find you when they send treats. Three to thirty two
            characters, lowercase letters, numbers and underscores.
          </p>

          {error ? (
            <div className="notice error" role="alert">
              {error}
            </div>
          ) : null}

          <form onSubmit={submit}>
            <div className="field">
              <label htmlFor="handle">Handle</label>
              <input
                id="handle"
                required
                minLength={3}
                maxLength={32}
                value={handle}
                onChange={(event) => setHandle(event.target.value)}
                placeholder="dahlia"
              />
            </div>

            <div className="field">
              <label htmlFor="display-name">Display name</label>
              <input
                id="display-name"
                maxLength={64}
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                placeholder="Dahlia"
              />
            </div>

            <button type="submit" disabled={busy}>
              {busy ? "Claiming..." : "Claim it"}
            </button>
          </form>
        </div>
      </div>
    </main>
  );
}
