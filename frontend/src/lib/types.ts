/**
 * The wire shapes, mirroring backend/src/meowpay/api/schemas.py.
 *
 * Hand written rather than generated. The API has seven endpoints and a
 * generator would be more machinery than the surface earns, but the mirror is
 * real: if a field is renamed in schemas.py nothing here fails to compile, it
 * just arrives undefined at runtime. docs/api.md is the contract both sides
 * are copying from.
 */

export type MovementKind = "transfer" | "deposit";

/**
 * The ceiling the ledger enforces, mirrored here only to turn a doomed request
 * into a readable message.
 *
 * `ck_transfers_amount_within_cap` is the authority and the API answers
 * `amount_out_of_range`, so this is a convenience and never a second rule. It
 * also keeps an absurd figure from reaching `JSON.stringify`, which renders
 * anything at or above 1e21 in exponential notation that Python then parses as
 * a float, turning a clear `amount_out_of_range` into a generic
 * `validation_error`.
 */
export const MAX_AMOUNT = 1_000_000_000_000;

export interface Cat {
  id: string;
  handle: string;
  display_name: string;
}

/** `GET /api/v1/me`. The only endpoint that reports a balance. */
export interface Me extends Cat {
  balance: number;
}

/**
 * A settled movement, and the receipt for it.
 *
 * `balance_after` is the balance the instant this movement settled. On a replay
 * that is a HISTORICAL value, so it must never be painted as the current
 * balance. Refetch `GET /api/v1/me` instead.
 */
export interface Movement {
  id: string;
  kind: MovementKind;
  amount: number;
  idempotency_key: string;
  balance_after: number;
  created_at: string;
  replayed: boolean;
}

/** One line of the ledger, from the signed-in cat's point of view. */
export interface Entry {
  id: number;
  transfer_id: string;
  kind: MovementKind;
  /** Signed: negative debits this cat, positive credits it. */
  amount: number;
  balance_after: number;
  counterparty_handle: string;
  counterparty_display_name: string;
  created_at: string;
}

export interface EntryPage {
  entries: Entry[];
  /** Null on the last page. Pass it back as `before`, never construct it. */
  next_before: number | null;
}

/**
 * Every error the API returns, including a 404 on a mistyped path and a 500.
 *
 * Branch on `code`, never on the HTTP status. A status can change without
 * breaking a client; these strings will not.
 */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    request_id: string | null;
  };
}
