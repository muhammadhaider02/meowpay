"""The refusals that exist to catch a misconfigured deployment.

No database and no network. Every function here is string handling, which is the
whole reason the boot-time check can run before anything is dialled.

These are the loudest paths in `config.py` and the only ones a deployment
actually walks into, so they are the ones that have to be exercised: a refusal
nothing covers is a comment with a `raise` in front of it.
"""

from __future__ import annotations

import pytest

from meowpay import config

# Shaped like a real Supabase session pooler string. Every case below starts
# from this and changes exactly one thing, so a failure names its own cause.
GOOD = "postgresql://postgres.abcdefghijklmnop:s3cr3t@aws-0-eu-west-2.pooler.supabase.com:5432/postgres"


def _set(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", url)


# -- the three refusals ----------------------------------------------------


def test_the_transaction_pooler_port_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """One digit from the right answer, and all three symptoms are silent.

    Prepared statements become intermittent errors under load only, session
    settings stop holding, and the concurrency suite's one-backend-per-thread
    assertion fails. None of that points back at the port.
    """
    _set(monkeypatch, GOOD.replace(":5432/", ":6543/"))

    with pytest.raises(RuntimeError, match="transaction pooler"):
        config.database_url()


def test_an_sslmode_that_permits_plaintext_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The password and every balance travel over this link."""
    for weak in ("disable", "allow", "prefer"):
        _set(monkeypatch, f"{GOOD}?sslmode={weak}")

        with pytest.raises(RuntimeError, match="unencrypted"):
            config.database_url()


def test_a_repeated_sslmode_cannot_walk_past_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicated key parses as a tuple, not a string.

    Left alone it matches neither the refusal nor the missing case, so the URL
    sails through with no `require` added and libpq quietly honours the last
    value. This is the shape a hand-edited connection string takes.
    """
    _set(monkeypatch, f"{GOOD}?sslmode=require&sslmode=disable")

    with pytest.raises(RuntimeError, match="unencrypted"):
        config.database_url()


def test_sslmode_is_compared_without_regard_to_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Disable` disables exactly as thoroughly as `disable`."""
    _set(monkeypatch, f"{GOOD}?sslmode=DISABLE")

    with pytest.raises(RuntimeError, match="unencrypted"):
        config.database_url()


def test_a_bare_postgres_username_against_the_pooler_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supavisor answers this with 'Tenant or user not found', which names
    neither the username nor the project and is not diagnosable."""
    _set(monkeypatch, GOOD.replace("postgres.abcdefghijklmnop:", "postgres:"))

    with pytest.raises(RuntimeError, match="postgres.<project-ref>"):
        config.database_url()


def test_a_bare_username_is_allowed_away_from_the_pooler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule is about Supavisor, not about usernames.

    A plain local Postgres has no tenant to name, so refusing `postgres` there
    would reject a URL that works.
    """
    _set(monkeypatch, "postgresql://postgres:pw@db.example.com:5432/postgres")

    assert "postgres:" in config.database_url()


# -- the two silent rewrites -----------------------------------------------


def test_a_bare_scheme_is_rewritten_rather_than_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dashboard hands out `postgresql://`, so refusing it would reject the
    thing everyone pastes. SQLAlchemy would otherwise reach for psycopg2, which
    is not installed, and the ModuleNotFoundError reads as a broken install."""
    _set(monkeypatch, GOOD)

    assert config.database_url().startswith("postgresql+psycopg://")


def test_a_missing_sslmode_is_added_rather_than_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding `require` where there is nothing can only tighten."""
    _set(monkeypatch, GOOD)

    assert "sslmode=require" in config.database_url()


# -- the summary that goes in a log ----------------------------------------


def test_the_database_summary_carries_no_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """This string exists to be logged, so the credential must not be in it.

    The boot line is written to whatever the platform aggregates, and a password
    that reaches a log aggregator is there for ever.
    """
    _set(monkeypatch, GOOD)

    summary = config.database_summary()

    assert "s3cr3t" not in summary
    assert "aws-0-eu-west-2.pooler.supabase.com" in summary
    assert summary.endswith("/postgres")


def test_the_database_summary_survives_a_password_full_of_punctuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generated password contains anything, including the characters a
    regex-based redaction would use as landmarks.

    This one decodes to `p@ss:word/@`, which carries an at sign, a colon and a
    slash: the three separators anything splitting the rendered URL would key on.
    """
    _set(monkeypatch, GOOD.replace(":s3cr3t@", ":p%40ss%3Aword%2F%40@"))

    summary = config.database_summary()

    assert "word" not in summary
    assert "%40" not in summary

    # Masking the rendered URL rather than building from the components would
    # pass everything above, because SQLAlchemy renders the hidden password as
    # three asterisks. This is what pins the difference.
    assert "*" not in summary


# -- the other two the deploy depends on -----------------------------------


def test_a_missing_variable_names_itself_and_says_how_to_fix_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        config.database_url()


def test_an_empty_variable_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank line in `.env`, or an environment variable set to nothing in a
    dashboard, should behave the same as an absent one rather than parsing."""
    monkeypatch.setenv("DATABASE_URL", "")

    with pytest.raises(RuntimeError, match="is not set"):
        config.database_url()


def test_a_wildcard_cors_origin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """`*` would let any page on the internet call the API with a token it
    persuaded a browser to send."""
    monkeypatch.setenv("CORS_ORIGINS", "https://meowpay.vercel.app,*")

    with pytest.raises(RuntimeError):
        config.cors_origins()


def test_cors_origins_splits_and_trims(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dashboard field is pasted by a human, so spaces around the commas are
    the normal case rather than the exception."""
    monkeypatch.setenv("CORS_ORIGINS", " https://a.example.com , https://b.example.com ")

    assert config.cors_origins() == ["https://a.example.com", "https://b.example.com"]
