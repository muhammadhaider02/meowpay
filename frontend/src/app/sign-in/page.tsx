"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { getSupabase } from "@/lib/supabase";

/**
 * Sign in by email, not by handle.
 *
 * `meowpay-seed` creates each demo cat as {handle}@meowpay.test, because GoTrue
 * authenticates an email and a handle is a MeowPay concept it has never heard
 * of. A form that asked for a handle would be asking for the one thing that
 * cannot be used to sign in.
 */
export default function SignInPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"in" | "up">("in");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);

    const auth = getSupabase().auth;
    const credentials = { email: email.trim(), password };
    const { error: failure } =
      mode === "in"
        ? await auth.signInWithPassword(credentials)
        : await auth.signUp(credentials);

    setBusy(false);

    if (failure) {
      setError(failure.message);
      return;
    }

    // Email confirmation is off for the demo, so a sign-up lands signed in and
    // the onboarding gate on the next page is what asks for a handle.
    router.replace("/");
  }

  return (
    <main>
      <div className="center">
        <div className="masthead">
          <span className="brand">MeowPay</span>
        </div>

        <div className="card">
          <h1>{mode === "in" ? "Sign in" : "Create an account"}</h1>
          <p className="muted">
            {mode === "in"
              ? "Seeded cats sign in with their email, like dahlia@meowpay.test."
              : "Pick any email. Confirmation is off for the demo, so you land straight in."}
          </p>

          {error ? (
            <div className="notice error" role="alert">
              {error}
            </div>
          ) : null}

          <form onSubmit={submit}>
            <div className="field">
              <label htmlFor="email">Email</label>
              <input
                id="email"
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="dahlia@meowpay.test"
              />
            </div>

            <div className="field">
              <label htmlFor="password">Password</label>
              <input
                id="password"
                type="password"
                autoComplete={mode === "in" ? "current-password" : "new-password"}
                required
                minLength={6}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </div>

            <button type="submit" disabled={busy}>
              {busy ? "Working..." : mode === "in" ? "Sign in" : "Create account"}
            </button>
          </form>
        </div>

        <p className="muted">
          {mode === "in" ? "No account? " : "Already have one? "}
          <button
            type="button"
            className="link"
            onClick={() => {
              setMode(mode === "in" ? "up" : "in");
              setError(null);
            }}
          >
            {mode === "in" ? "Create one" : "Sign in"}
          </button>
        </p>
      </div>
    </main>
  );
}
