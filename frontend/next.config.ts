import { PHASE_PRODUCTION_BUILD } from "next/constants";
import type { NextConfig } from "next";

/**
 * Every NEXT_PUBLIC_ value is compiled into the bundle, so a production build
 * that is missing one produces an artifact that is permanently wrong. None of
 * them fail the build on their own, and the failure modes get quieter the more
 * important the value is:
 *
 *   NEXT_PUBLIC_API_BASE_URL          silently becomes localhost, so every
 *                                     request from the deployed app goes
 *                                     nowhere, with no error and no log. On an
 *                                     https page the browser blocks it as mixed
 *                                     content before it is even sent.
 *   NEXT_PUBLIC_SUPABASE_URL          throws on the first click, not at boot,
 *   NEXT_PUBLIC_SUPABASE_PUBLISHABLE  because the client is built lazily.
 *
 * So the check lives here rather than at module scope in `lib/api.ts`. A throw
 * there does currently fail the build, because client components really are
 * evaluated during prerendering, but it would bind the guard to the import
 * graph: a later `export const dynamic = "force-dynamic"` stops prerendering
 * and the guard goes quiet with nothing to notice. It would also couple the
 * test suite to NODE_ENV, since the tests import that module at top level.
 *
 * This runs on every `next build`, before compilation, whatever the routes do.
 */
const REQUIRED = [
  "NEXT_PUBLIC_SUPABASE_URL",
  "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY",
  "NEXT_PUBLIC_API_BASE_URL",
] as const;

function assertConfigured(): void {
  const missing = REQUIRED.filter((name) => !process.env[name]);
  if (missing.length === 0) return;

  throw new Error(
    `Cannot build without ${missing.join(", ")}.\n\n` +
      "These are compiled into the bundle, so a build without them produces an " +
      "app that fails silently in the browser rather than one that fails here.\n" +
      "Locally: copy frontend/.env.example to frontend/.env.local.\n" +
      "On Vercel: add them under Settings, Environment Variables, scoped to " +
      "Production and Preview, then redeploy. Editing them without a redeploy " +
      "changes nothing, because the old values are already in the bundle.",
  );
}

const nextConfig: NextConfig = {
  reactStrictMode: true,
};

// Deliberately not a rewrite proxying /api to the backend. A same-origin
// rewrite would hide a misconfigured CORS_ORIGINS until someone opened the
// deployed app from a second domain.
export default (phase: string): NextConfig => {
  if (phase === PHASE_PRODUCTION_BUILD) assertConfigured();
  return nextConfig;
};
