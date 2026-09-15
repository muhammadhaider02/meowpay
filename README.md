<div align="center">

# MeowPay

**MEOW. MEOW. MEOW.**

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://python.org)
[![uv](https://img.shields.io/badge/uv-Package_Manager-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Supabase](https://img.shields.io/badge/Database-Supabase-3FCF8E?logo=supabase&logoColor=white)](https://supabase.com)

A digital wallet for cats. Humans top it up, cats send each other treats.

</div>

---

One vertical slice of a money-movement product: a cat signs in, sees a balance
and sends treats to another cat. A FastAPI service over Postgres with an
append-only double-entry ledger, row-level locking and idempotent writes, so a
transfer settles exactly once or not at all.
