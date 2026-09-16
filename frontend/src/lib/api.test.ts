/**
 * The request client, which is the only part of the browser that can cause a
 * double spend.
 *
 * Everything asserted here is a rule the ledger depends on the caller keeping:
 * that a retry reuses its key and carries a fresh token, that a replay is a
 * success and not an error, that the body says what the cat typed. The backend
 * enforces what it can, but it cannot tell a retry that reused its key from one
 * that invented a new one, so these are the tests for the half that lives out
 * here.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getSession = vi.fn();

vi.mock("@/lib/supabase", () => ({
  getSupabase: () => ({ auth: { getSession } }),
}));

const { ApiError, NetworkError, TIMED_OUT, UNREACHABLE, api } = await import("@/lib/api");

const BASE = "http://localhost:8000";

/** A response the API would actually produce. */
function respond(status: number, body: unknown, headers: Record<string, string> = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
    json: async () => body,
  } as unknown as Response;
}

function envelope(code: string, message = "nope", requestId: string | null = null) {
  return { error: { code, message, request_id: requestId } };
}

const MOVEMENT = {
  id: "6b1e0000-0000-0000-0000-000000000000",
  kind: "transfer",
  amount: 120,
  idempotency_key: "key-12345678",
  balance_after: 380,
  created_at: "2026-09-16T10:31:22.481Z",
  replayed: false,
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  getSession.mockReset();
  getSession.mockResolvedValue({ data: { session: { access_token: "token-one" } } });
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function callUrl(index = 0): string {
  return fetchMock.mock.calls[index]?.[0] as string;
}

function callOptions(index = 0) {
  return fetchMock.mock.calls[index]?.[1] as RequestInit & {
    headers: Record<string, string>;
  };
}

/** What actually went on the wire, parsed back. */
function sentBody(index = 0): unknown {
  return JSON.parse(callOptions(index).body as string);
}

describe("what goes on the wire", () => {
  it("sends a transfer as a POST to /transfers with the typed handle and amount", async () => {
    fetchMock.mockResolvedValue(respond(201, MOVEMENT));

    await api.transfer("milo", 120, "key-12345678");

    expect(callUrl()).toBe(`${BASE}/api/v1/transfers`);
    expect(callOptions().method).toBe("POST");
    expect(sentBody()).toEqual({ to_handle: "milo", amount: 120 });
  });

  it("sends a deposit as a POST to /deposits, with no recipient field at all", async () => {
    // A target here would let anyone mint treats into anyone else's account, so
    // the absence is the contract and not an omission.
    fetchMock.mockResolvedValue(respond(201, { ...MOVEMENT, kind: "deposit" }));

    await api.deposit(500, "topup-12345678");

    expect(callUrl()).toBe(`${BASE}/api/v1/deposits`);
    expect(callOptions().method).toBe("POST");
    expect(sentBody()).toEqual({ amount: 500 });
  });

  it("sends onboarding with the snake_case field the backend forbids extras around", async () => {
    // OnboardRequest is extra="forbid", so displayName rather than display_name
    // is a 422 and not a silently ignored field.
    fetchMock.mockResolvedValue(respond(201, { id: "x", handle: "dahlia", display_name: "D" }));

    await api.onboard("dahlia", "Dahlia");

    expect(callUrl()).toBe(`${BASE}/api/v1/cats`);
    expect(callOptions().method).toBe("POST");
    expect(sentBody()).toEqual({ handle: "dahlia", display_name: "Dahlia" });
  });

  it("reads with GET and no body", async () => {
    fetchMock.mockResolvedValue(respond(200, []));

    await api.directory();

    expect(callUrl()).toBe(`${BASE}/api/v1/cats`);
    expect(callOptions().method).toBe("GET");
    expect(callOptions().body).toBeUndefined();
  });

  it("declares JSON on a body, which the backend requires to parse one", async () => {
    fetchMock.mockResolvedValue(respond(201, MOVEMENT));

    await api.transfer("milo", 120, "key-12345678");

    expect(callOptions().headers["Content-Type"]).toBe("application/json");
  });

  it("never serves a balance or a statement from cache", async () => {
    fetchMock.mockResolvedValue(respond(200, {}));

    await api.me();

    expect(callOptions().cache).toBe("no-store");
  });
});

describe("the token", () => {
  it("travels as a bearer header", async () => {
    fetchMock.mockResolvedValue(respond(200, { balance: 10 }));

    await api.me();

    expect(callOptions().headers["Authorization"]).toBe("Bearer token-one");
  });

  it("is read again for every request rather than captured once", async () => {
    fetchMock.mockResolvedValue(respond(200, {}));

    await api.me();
    getSession.mockResolvedValue({ data: { session: { access_token: "token-two" } } });
    await api.me();

    expect(getSession).toHaveBeenCalledTimes(2);
    expect(callOptions(0).headers["Authorization"]).toBe("Bearer token-one");
    expect(callOptions(1).headers["Authorization"]).toBe("Bearer token-two");
  });

  it("is simply absent when nobody is signed in", async () => {
    getSession.mockResolvedValue({ data: { session: null } });
    fetchMock.mockResolvedValue(respond(401, envelope("unauthenticated")));

    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(callOptions().headers["Authorization"]).toBeUndefined();
  });

  it("never sends cookies, because the API refuses credentialed requests", async () => {
    fetchMock.mockResolvedValue(respond(200, {}));

    await api.me();

    expect(callOptions().credentials).toBe("omit");
  });
});

describe("the idempotency key", () => {
  it("is sent exactly once on a transfer, with the value the caller supplied", async () => {
    fetchMock.mockResolvedValue(respond(201, MOVEMENT));

    await api.transfer("milo", 120, "key-12345678");

    expect(callOptions().headers["Idempotency-Key"]).toBe("key-12345678");
  });

  it("is sent on a deposit too", async () => {
    fetchMock.mockResolvedValue(respond(201, { ...MOVEMENT, kind: "deposit" }));

    await api.deposit(500, "topup-12345678");

    expect(callOptions().headers["Idempotency-Key"]).toBe("topup-12345678");
  });

  it("is absent from reads, which have no movement to make idempotent", async () => {
    fetchMock.mockResolvedValue(respond(200, []));

    await api.directory();

    expect(callOptions().headers["Idempotency-Key"]).toBeUndefined();
  });
});

describe("success", () => {
  it("accepts 201, which is a movement that settled", async () => {
    fetchMock.mockResolvedValue(respond(201, MOVEMENT));

    await expect(api.transfer("milo", 120, "key-12345678")).resolves.toMatchObject({
      replayed: false,
    });
  });

  it("accepts 200 with replayed true, which is a retry that moved nothing", async () => {
    fetchMock.mockResolvedValue(respond(200, { ...MOVEMENT, replayed: true }));

    await expect(api.transfer("milo", 120, "key-12345678")).resolves.toMatchObject({
      replayed: true,
    });
  });

  it("does not treat a redirect as success", async () => {
    // Guards `response.ok` against being loosened to a status comparison.
    fetchMock.mockResolvedValue(respond(302, envelope("not_found")));

    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
  });
});

describe("rejections", () => {
  it("carries the code, which is what callers branch on", async () => {
    fetchMock.mockResolvedValue(
      respond(422, envelope("insufficient_funds", "Balance is 50 treats, which is short of 500.")),
    );

    await expect(api.transfer("milo", 500, "key-12345678")).rejects.toMatchObject({
      code: "insufficient_funds",
      status: 422,
      message: "Balance is 50 treats, which is short of 500.",
    });
  });

  it("prefers the request id in the envelope", async () => {
    fetchMock.mockResolvedValue(
      respond(500, envelope("internal_error", "boom", "from-envelope"), {
        "x-request-id": "from-header",
      }),
    );

    await expect(api.me()).rejects.toMatchObject({ requestId: "from-envelope" });
  });

  it("falls back to the header when the envelope carries no id", async () => {
    fetchMock.mockResolvedValue(
      respond(500, envelope("internal_error", "boom", null), { "x-request-id": "from-header" }),
    );

    await expect(api.me()).rejects.toMatchObject({ requestId: "from-header" });
  });

  it("turns a body that is not JSON into a typed error, keeping the request id", async () => {
    // A 502 from a proxy is exactly the case worth tracing, so the id must
    // survive the parse failure.
    fetchMock.mockResolvedValue({
      ok: false,
      status: 502,
      headers: { get: (name: string) => (name.toLowerCase() === "x-request-id" ? "trace-me" : null) },
      json: async () => {
        throw new SyntaxError("Unexpected token <");
      },
    } as unknown as Response);

    await expect(api.me()).rejects.toMatchObject({
      code: "invalid_response",
      status: 502,
      requestId: "trace-me",
    });
  });

  it("distinguishes the network failing from the API rejecting", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    const failure = await api.me().catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(NetworkError);
    expect(failure).not.toBeInstanceOf(ApiError);
    expect((failure as Error).message).toBe(UNREACHABLE);
  });
});

describe("giving up", () => {
  /** A fetch that never answers until its signal aborts. */
  function hangs() {
    return (_url: string, options: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        options.signal?.addEventListener("abort", () =>
          reject(new DOMException("Aborted", "AbortError")),
        );
      });
  }

  it("aborts a request that never answers, and says it may be waking", async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(hangs());

    const pending = api.me().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(61_000);
    const failure = (await pending) as Error;

    expect(failure).toBeInstanceOf(NetworkError);
    expect(failure.message).toBe(TIMED_OUT);
  });

  it("still gives up when the headers arrive but the body stalls", async () => {
    // The budget has to cover reading the body. Cancelling the timer once the
    // headers land leaves a half-delivered response hanging for ever, and the
    // only way out of that is a reload.
    vi.useFakeTimers();
    fetchMock.mockImplementation((_url: string, options: RequestInit) =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => null },
        json: () =>
          new Promise((_resolve, reject) => {
            options.signal?.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      } as unknown as Response),
    );

    const pending = api.me().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(61_000);
    const failure = (await pending) as Error;

    expect(failure).toBeInstanceOf(NetworkError);
    expect(failure.message).toBe(TIMED_OUT);
  });
});

describe("the retry", () => {
  it("reuses the same idempotency key, which is the only reason it is safe", async () => {
    // If the first attempt settled and its response was lost, the retry must
    // replay it. A fresh key here would make the retry a second movement, and
    // that is the double spend this whole mechanism exists to prevent.
    fetchMock
      .mockResolvedValueOnce(respond(401, envelope("token_expired")))
      .mockResolvedValueOnce(respond(200, { ...MOVEMENT, replayed: true }));

    await api.transfer("milo", 120, "key-12345678");

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(callOptions(0).headers["Idempotency-Key"]).toBe("key-12345678");
    expect(callOptions(1).headers["Idempotency-Key"]).toBe("key-12345678");
  });

  it("carries a freshly read token, or it could only fail again", async () => {
    // The point of retrying an expired token is that supabase-js refreshes in
    // between. Reading the token once and reusing it would make the second
    // attempt a guaranteed second 401.
    getSession
      .mockResolvedValueOnce({ data: { session: { access_token: "stale" } } })
      .mockResolvedValueOnce({ data: { session: { access_token: "refreshed" } } });
    fetchMock
      .mockResolvedValueOnce(respond(401, envelope("token_expired")))
      .mockResolvedValueOnce(respond(200, MOVEMENT));

    await api.transfer("milo", 120, "key-12345678");

    expect(getSession).toHaveBeenCalledTimes(2);
    expect(callOptions(0).headers["Authorization"]).toBe("Bearer stale");
    expect(callOptions(1).headers["Authorization"]).toBe("Bearer refreshed");
  });

  it("happens at most once, so an expired token cannot loop", async () => {
    fetchMock.mockResolvedValue(respond(401, envelope("token_expired")));

    await expect(api.me()).rejects.toMatchObject({ code: "token_expired" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not happen for anything else, including a retryable ledger_busy", async () => {
    // ledger_busy really is retryable, but retrying it behind the cat's back
    // means retrying something that may already have moved treats. That is a
    // button, not a reflex.
    fetchMock.mockResolvedValue(respond(503, envelope("ledger_busy")));

    await expect(api.transfer("milo", 120, "key-12345678")).rejects.toMatchObject({
      code: "ledger_busy",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("the statement query", () => {
  it("omits the cursor on the first page", async () => {
    fetchMock.mockResolvedValue(respond(200, { entries: [], next_before: null }));

    await api.entries();

    expect(callUrl()).toBe(`${BASE}/api/v1/me/entries?limit=20`);
  });

  it("passes the cursor back exactly as the API gave it", async () => {
    fetchMock.mockResolvedValue(respond(200, { entries: [], next_before: null }));

    await api.entries(411, 50);

    expect(callUrl()).toBe(`${BASE}/api/v1/me/entries?limit=50&before=411`);
  });

  it("keeps a falsy cursor rather than treating it as absent", async () => {
    fetchMock.mockResolvedValue(respond(200, { entries: [], next_before: null }));

    await api.entries(0);

    expect(callUrl()).toBe(`${BASE}/api/v1/me/entries?limit=20&before=0`);
  });
});

describe("a slow API", () => {
  it("reports a cold start before it gives up on one", async () => {
    // Render spins down after 15 minutes and takes about 50 seconds to wake, so
    // the UI needs to say so rather than show a spinner that reads as a hang.
    vi.useFakeTimers();
    const onSlow = vi.fn();
    fetchMock.mockImplementation(() => new Promise(() => {}));

    void api.me(onSlow);
    await vi.advanceTimersByTimeAsync(2_600);

    expect(onSlow).toHaveBeenCalledOnce();
  });

  it("does not cry cold start on a request that answered promptly", async () => {
    vi.useFakeTimers();
    const onSlow = vi.fn();
    fetchMock.mockResolvedValue(respond(200, {}));

    await api.me(onSlow);
    await vi.advanceTimersByTimeAsync(10_000);

    expect(onSlow).not.toHaveBeenCalled();
  });
});
