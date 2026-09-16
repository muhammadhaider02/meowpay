"""Values the schema and the service both depend on."""

import uuid
from typing import Final

# The account every deposit is funded from. Treats are never conjured: a deposit
# moves them off the treasury, which is why every entry in the ledger sums to
# zero globally rather than only per transfer.
#
# All zeros for two reasons. It is self-evidently a sentinel rather than a real
# cat, and it always sorts first, so in any lock set containing the treasury it
# is acquired first and can never sit in the middle of a wait cycle.
TREASURY_CAT_ID: Final = uuid.UUID("00000000-0000-0000-0000-000000000000")
TREASURY_HANDLE: Final = "meowpay_treasury"

# The one definition of a handle. models.py builds its CHECK constraint from
# this and the onboarding endpoint validates against it, so the Python mirror and
# the SQL mirror cannot drift. Two copies would eventually disagree, and the
# symptom is a user error arriving as a 500 from a constraint violation.
#
# Match it with re.fullmatch and never re.match with anchors: Python's `$` also
# matches before a trailing newline and Postgres's does not, so "dahlia\n" would
# pass the Python check and then be refused by the database.
HANDLE_REGEX: Final = r"[a-z0-9_]{3,32}"

# Refused at onboarding, and refused as `handle_invalid` rather than
# `handle_taken` so nothing leaks about which rows exist. Without this, anyone
# could register meowpay_support and phish from a name that looks official.
RESERVED_HANDLE_PREFIX: Final = "meowpay_"

# The one place the API version is written down. A directory per version is
# how you pay for running two at once, and there is one.
API_V1_PREFIX: Final = "/api/v1"

# Balances are BIGINT, but JavaScript numbers lose precision past 2^53 - 1, so a
# balance that a browser cannot represent exactly is worse than useless. Cap the
# amount well below that.
MAX_AMOUNT: Final = 1_000_000_000_000

# Number.MAX_SAFE_INTEGER. Past this a browser cannot represent a balance
# exactly, so the database refuses to hold one.
JS_SAFE_INTEGER: Final = 9_007_199_254_740_991
