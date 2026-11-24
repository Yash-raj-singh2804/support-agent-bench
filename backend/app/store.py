"""Episode sandboxes: clone the seed schema, snapshot, hash and diff DB state."""

import hashlib
import json
import re
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import insert, select, text

from . import seed_data
from .db import STORE_TABLES, get_engine, scoped, store_md

SEED_SCHEMA = "seed"
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _check(schema: str) -> str:
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"bad schema name {schema!r}")
    return schema


def create_schema(schema: str, populate_from_seed_data: bool = False, clone_from: str | None = SEED_SCHEMA) -> str:
    _check(schema)
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        sc = scoped(conn, schema)
        store_md.create_all(sc)
        if populate_from_seed_data:
            for table_name, data in seed_data.rows():
                if data:
                    sc.execute(insert(store_md.tables[table_name]), data)
        elif clone_from:
            _check(clone_from)
            for t in STORE_TABLES:
                conn.execute(text(f'INSERT INTO "{schema}"."{t.name}" SELECT * FROM "{clone_from}"."{t.name}"'))
    return schema


def drop_schema(schema: str) -> None:
    _check(schema)
    if schema == SEED_SCHEMA:
        raise ValueError("refusing to drop seed")
    with get_engine().begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def rebuild_seed() -> None:
    create_schema(SEED_SCHEMA, populate_from_seed_data=True, clone_from=None)


def new_sandbox(prefix: str) -> str:
    return create_schema(f"{prefix}_{uuid.uuid4().hex[:12]}")


# --------------------------------------------------------------------------- snapshot / hash / diff
def _norm(v):
    if isinstance(v, Decimal):
        return format(v.quantize(Decimal("0.01")), "f")
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in sorted(v.items())}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def _pk(t, row) -> str:
    return "|".join(str(row[c.name]) for c in t.primary_key.columns)


def snapshot(schema: str) -> dict[str, dict[str, dict]]:
    """{table: {pk: row}} with normalized, JSON-serializable values."""
    out: dict[str, dict[str, dict]] = {}
    with get_engine().connect() as conn:
        sc = scoped(conn, _check(schema))
        for t in STORE_TABLES:
            rows = sc.execute(select(t).order_by(*t.primary_key.columns)).mappings().all()
            out[t.name] = {_pk(t, r): {k: _norm(v) for k, v in r.items()} for r in rows}
    return out


def state_hash(snap: dict) -> str:
    return hashlib.sha256(json.dumps(snap, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def diff(expected: dict, actual: dict) -> list[dict]:
    """Row-level differences: what ``actual`` has relative to ``expected``."""
    changes: list[dict] = []
    for table in sorted(set(expected) | set(actual)):
        e, a = expected.get(table, {}), actual.get(table, {})
        for pk in sorted(set(e) | set(a)):
            if pk not in a:
                changes.append({"table": table, "pk": pk, "op": "missing", "expected": e[pk], "actual": None})
            elif pk not in e:
                changes.append({"table": table, "pk": pk, "op": "extra", "expected": None, "actual": a[pk]})
            elif e[pk] != a[pk]:
                fields = {k: {"expected": e[pk].get(k), "actual": a[pk].get(k)}
                          for k in sorted(set(e[pk]) | set(a[pk])) if e[pk].get(k) != a[pk].get(k)}
                changes.append({"table": table, "pk": pk, "op": "changed", "fields": fields})
    return changes
