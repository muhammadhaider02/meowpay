"""Router aggregation.

Every versioned endpoint registers on `api_router`, which carries the version as
a prefix. There is no `v1/` directory: a directory per version is how you pay for
running two versions at once, and there is one. When a second version arrives,
`API_V1_PREFIX` gains a sibling and this file gains a router, which is a smaller
change than unpicking a directory layout.

Health is deliberately not here. It is mounted unversioned at the root in
`app.py`, so a liveness probe does not move when the API version does.
"""

from fastapi import APIRouter

from meowpay.api.routes.cats import router as cats_router
from meowpay.api.routes.me import router as me_router
from meowpay.api.routes.movements import router as movements_router
from meowpay.constants import API_V1_PREFIX

api_router = APIRouter(prefix=API_V1_PREFIX)
api_router.include_router(cats_router)
api_router.include_router(me_router)
# No prefix of its own: /transfers and /deposits are siblings of /cats rather
# than children of a shared noun, because the resource being created is the
# movement and the two kinds settle through one path in the ledger.
api_router.include_router(movements_router)
