"use client";

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

/**
 * The browser auth client. This is the only thing in the app that talks to
 * Supabase directly, and it talks to GoTrue and nothing else.
 *
 * It never reaches the database. The tables live in a private `meowpay` schema
 * that PostgREST does not expose, so `supabase.from("cats")` would fail even
 * with a valid session. Every balance and every movement comes from FastAPI,
 * which is the only writer of the ledger.
 */

function required(name: string, value: string | undefined): string {
  if (!value) {
    // Thrown at module load in development, which is the point. A missing
    // NEXT_PUBLIC_ value is baked in at build time, so a silent undefined here
    // becomes a deployed app that fails on the sign-in button instead.
    throw new Error(
      `${name} is not set. Copy frontend/.env.example to .env.local and fill it in.`,
    );
  }
  return value;
}

let client: SupabaseClient | undefined;

export function getSupabase(): SupabaseClient {
  if (!client) {
    client = createClient(
      required("NEXT_PUBLIC_SUPABASE_URL", process.env.NEXT_PUBLIC_SUPABASE_URL),
      required(
        "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY",
        process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY,
      ),
      { auth: { persistSession: true, autoRefreshToken: true } },
    );
  }
  return client;
}
