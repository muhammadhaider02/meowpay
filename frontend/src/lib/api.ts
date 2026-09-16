"use client";

import { getSupabase } from "@/lib/supabase";
import type { ApiErrorBody, Cat, EntryPage, Me, Movement } from "@/lib/types";

const BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/**
 * Render's free tier spins down after 15 minutes idle and takes around 50
 * seconds to wake. That is a real state, not a failure, so the budget is
 * generous and the UI is told when a request crosses into "this is a cold
 * start" territory rather than being left to render a spinner that reads as a
 * hang.
 */
const REQUEST_TIMEOUT_MS = 60_000;
const SLOW_REQUEST_MS = 2_500;

/** A rejection the API described. `code` is the contract; the status is not. */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly requestId: string | null;

  constructor(code: string, message: string, status: number, requestId: string | null) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.requestId = requestId;
  }
}

/** The network never answered. Distinct from a rejection the API described. */
export class NetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NetworkError";
  }
}

/**
 * The two ways the network can fail, as messages rather than inline strings,
 * because the difference between them is what the UI tells the cat to do.
 */
export const TIMED_OUT =
  "The API did not answer within a minute. It may be waking up, so try again.";
export const UNREACHABLE = "Could not reach the API. Check that it is running.";

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  /** Required by the money-moving routes, refused everywhere else. */
  idempotencyKey?: string;
  /** Fires once if the request is slow enough to look like a Render cold start. */
  onSlow?: () => void;
}

async function accessToken(): Promise<string | null> {
  // Read immediately before the request rather than captured at mount.
  // supabase-js refreshes in the background, and a token held in state
  // produces exactly the intermittent 401s that are miserable to debug.
  const { data } = await getSupabase().auth.getSession();
  return data.session?.access_token ?? null;
}

async function once<T>(path: string, options: RequestOptions): Promise<T> {
  const { method = "GET", body, idempotencyKey, onSlow } = options;

  const headers: Record<string, string> = { Accept: "application/json" };
  const token = await accessToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  // Set exactly once. Two Idempotency-Key headers on one request is a 422,
  // because which movement is being retried would otherwise be decided
  // silently by whichever value the framework happened to keep.
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  const slow = onSlow ? setTimeout(onSlow, SLOW_REQUEST_MS) : undefined;

  // The timers are cleared once the BODY has been read, not once the headers
  // arrive. Clearing them at the end of the fetch would leave `response.json()`
  // unguarded, so a response that delivers headers and then stalls would never
  // resolve and never reject: the form's `busy` flag would stay set and the only
  // way out would be a reload.
  try {
    let response: Response;
    try {
      response = await fetch(`${BASE}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        // No cookies. The token is a bearer header, which is also why the API
        // sets allow_credentials to false.
        credentials: "omit",
        cache: "no-store",
      });
    } catch {
      throw new NetworkError(controller.signal.aborted ? TIMED_OUT : UNREACHABLE);
    }

    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      if (controller.signal.aborted) throw new NetworkError(TIMED_OUT);
      throw new ApiError(
        "invalid_response",
        `The API answered ${response.status} with something that is not JSON.`,
        response.status,
        response.headers.get("X-Request-ID"),
      );
    }

    // 200 and 201 are both success on the money routes: 201 settled something,
    // 200 replayed one. A client that branches on 201 breaks on every retry.
    if (response.ok) return payload as T;

    const described = payload as Partial<ApiErrorBody>;
    const error = described?.error;
    throw new ApiError(
      error?.code ?? "unknown_error",
      error?.message ?? `The API answered ${response.status}.`,
      response.status,
      error?.request_id ?? response.headers.get("X-Request-ID"),
    );
  } finally {
    clearTimeout(timeout);
    if (slow) clearTimeout(slow);
  }
}

/**
 * One retry, and only for an expired token.
 *
 * This is safe only because every money-moving request carries a stable
 * idempotency key. `getSession()` refreshes on the second call, so the retry
 * goes out with a fresh token and the same key: if the first attempt settled,
 * the retry replays it and returns 200 rather than moving treats twice.
 *
 * Nothing else is retried. A 503 `ledger_busy` is retryable too, but that is
 * the user's decision to make with a button rather than something to do behind
 * their back while the treats may already have moved.
 */
async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  try {
    return await once<T>(path, options);
  } catch (error) {
    if (error instanceof ApiError && error.code === "token_expired") {
      return await once<T>(path, options);
    }
    throw error;
  }
}

export const api = {
  me: (onSlow?: () => void) => request<Me>("/api/v1/me", { onSlow }),

  directory: () => request<Cat[]>("/api/v1/cats"),

  onboard: (handle: string, displayName: string) =>
    request<Cat>("/api/v1/cats", {
      method: "POST",
      body: { handle, display_name: displayName },
    }),

  entries: (before?: number | null, limit = 20) => {
    const query = new URLSearchParams({ limit: String(limit) });
    if (before != null) query.set("before", String(before));
    return request<EntryPage>(`/api/v1/me/entries?${query.toString()}`);
  },

  transfer: (toHandle: string, amount: number, idempotencyKey: string) =>
    request<Movement>("/api/v1/transfers", {
      method: "POST",
      body: { to_handle: toHandle, amount },
      idempotencyKey,
    }),

  deposit: (amount: number, idempotencyKey: string) =>
    request<Movement>("/api/v1/deposits", {
      method: "POST",
      body: { amount },
      idempotencyKey,
    }),
};
