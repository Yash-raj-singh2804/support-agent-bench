"""Database engine and the two groups of tables.

* ``store_md`` - the mock store (users, orders, ...). These tables have no fixed
  schema; every episode gets its own Postgres schema cloned from ``seed`` and
  queries are routed there with ``schema_translate_map``.
* ``app_md`` - bookkeeping for the benchmark itself (runs, messages), always in
  ``public``.
"""

import time
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine

from .config import get_settings

Json = JSON().with_variant(JSONB(), "postgresql")
Money = Numeric(10, 2)

# --------------------------------------------------------------------------- store
store_md = MetaData()

users = Table(
    "users", store_md,
    Column("user_id", String, primary_key=True),
    Column("first_name", String, nullable=False),
    Column("last_name", String, nullable=False),
    Column("email", String, nullable=False, unique=True),
    Column("zip", String, nullable=False),
    Column("address", Json, nullable=False),
    Column("tier", String, nullable=False, default="standard"),  # standard | plus | premium
)

payment_methods = Table(
    "payment_methods", store_md,
    Column("payment_method_id", String, primary_key=True),
    Column("user_id", String, ForeignKey("users.user_id"), nullable=False),
    Column("source", String, nullable=False),  # credit_card | paypal | gift_card
    Column("brand", String),
    Column("last_four", String),
    Column("balance", Money),  # gift cards only
)

products = Table(
    "products", store_md,
    Column("product_id", String, primary_key=True),
    Column("name", String, nullable=False),
)

variants = Table(
    "variants", store_md,
    Column("item_id", String, primary_key=True),
    Column("product_id", String, ForeignKey("products.product_id"), nullable=False),
    Column("options", Json, nullable=False),
    Column("price", Money, nullable=False),
    Column("available", Boolean, nullable=False),
)

orders = Table(
    "orders", store_md,
    Column("order_id", String, primary_key=True),
    Column("user_id", String, ForeignKey("users.user_id"), nullable=False),
    Column("status", String, nullable=False),
    Column("address", Json, nullable=False),
    Column("placed_on", String, nullable=False),
    Column("delivered_on", String),
    Column("cancel_reason", String),
    Column("shipping_method", String, nullable=False, default="standard"),  # standard | express
)

order_items = Table(
    "order_items", store_md,
    Column("order_id", String, ForeignKey("orders.order_id"), primary_key=True),
    Column("line_no", Integer, primary_key=True),
    Column("item_id", String, nullable=False),
    Column("product_id", String, nullable=False),
    Column("name", String, nullable=False),
    Column("options", Json, nullable=False),
    Column("price", Money, nullable=False),
)

transactions = Table(
    "transactions", store_md,
    Column("tx_id", String, primary_key=True),
    Column("order_id", String, ForeignKey("orders.order_id"), nullable=False),
    Column("kind", String, nullable=False),  # payment | refund
    Column("amount", Money, nullable=False),
    Column("payment_method_id", String, nullable=False),
)

order_requests = Table(
    "order_requests", store_md,
    Column("request_id", String, primary_key=True),
    Column("order_id", String, ForeignKey("orders.order_id"), nullable=False),
    Column("kind", String, nullable=False),  # return | exchange
    Column("item_ids", Json, nullable=False),
    Column("new_item_ids", Json),
    Column("payment_method_id", String, nullable=False),
    Column("amount", Money, nullable=False),  # refund (return) or price difference (exchange)
    Column("status", String, nullable=False),
)

shipments = Table(
    "shipments", store_md,
    Column("order_id", String, ForeignKey("orders.order_id"), primary_key=True),
    Column("carrier", String, nullable=False),
    Column("tracking_number", String, nullable=False),
    Column("status", String, nullable=False),  # in_transit | delivered | lost | reshipped
    Column("shipped_on", String),
    Column("estimated_delivery", String),
)

claims = Table(  # damaged / defective items and lost packages
    "claims", store_md,
    Column("claim_id", String, primary_key=True),
    Column("order_id", String, ForeignKey("orders.order_id"), nullable=False),
    Column("kind", String, nullable=False),  # damaged | lost
    Column("item_ids", Json, nullable=False),
    Column("resolution", String, nullable=False),  # refund | replacement | reship
    Column("amount", Money, nullable=False),
    Column("payment_method_id", String),
)

store_credits = Table(
    "store_credits", store_md,
    Column("credit_id", String, primary_key=True),
    Column("user_id", String, ForeignKey("users.user_id"), nullable=False),
    Column("order_id", String),
    Column("reason", String, nullable=False),  # late_delivery | price_adjustment
    Column("amount", Money, nullable=False),
    Column("payment_method_id", String, nullable=False),
    Column("item_id", String),
)

support_tickets = Table(  # created by transfers to human agents
    "support_tickets", store_md,
    Column("ticket_id", String, primary_key=True),
    Column("user_id", String),
    Column("reason", String, nullable=False),
)

STORE_TABLES = [users, payment_methods, products, variants, orders, order_items, transactions, order_requests,
                shipments, claims, store_credits, support_tickets]

# --------------------------------------------------------------------------- app
app_md = MetaData(schema="public")

runs = Table(
    "runs", app_md,
    Column("id", String, primary_key=True),
    Column("kind", String, nullable=False),  # eval | playground
    Column("task_id", String),
    Column("batch_id", String, index=True),
    Column("experiment_id", String, index=True),
    Column("variant", Json),  # AgentConfig as dict
    Column("variant_label", String),
    Column("trial", Integer, default=0),
    Column("status", String, nullable=False),  # queued | running | done | error
    Column("reward", Float),
    Column("db_match", Boolean),
    Column("outputs_ok", Boolean),
    Column("process_ok", Boolean),  # null when process scoring was off (SCORE_PROCESS=false or older runs)
    Column("grounded_ok", Boolean),  # bait tasks only: no unsupported values / forbidden claims; else null
    Column("hallucinations", Json),  # bait tasks only: what was flagged
    Column("grounding_revisions", Integer),  # grounded variant: drafts sent back for revision
    Column("grounding_log", Json),  # grounded variant: each withheld draft and why
    Column("missing_outputs", Json),
    Column("agent_hash", String),
    Column("gold_hash", String),
    Column("diff", Json),
    Column("schema_name", String),
    Column("agent_model", String),
    Column("user_model", String),
    Column("end_reason", String),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("llm_calls", Integer),
    Column("duration_s", Float),
    Column("customer_tokens", Integer),
    Column("judge_tokens", Integer),
    Column("agent_cost_usd", Float),
    Column("cost_usd", Float),  # all roles; null when a model has no price
    Column("signals", Json),   # deterministic failure-analysis signals (all runs)
    Column("analysis", Json),  # LLM-judge failure classification (failed runs)
    Column("grounding", Json),  # hallucination check: deterministic claims + LLM verifier (all runs)
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)),
    Column("finished_at", DateTime(timezone=True)),
)

run_messages = Table(
    "run_messages", app_md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, ForeignKey("public.runs.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("idx", Integer, nullable=False),
    Column("role", String, nullable=False),  # user | assistant | tool
    Column("content", Text),
    Column("tool_calls", Json),
    Column("tool_call_id", String),
    Column("tool_name", String),
    Column("created_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)),
)

llm_calls = Table(
    "llm_calls", app_md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, ForeignKey("public.runs.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("seq", Integer, nullable=False),
    Column("turn", Integer, nullable=False),  # customer turn the call belongs to (0 = before first message)
    Column("role", String, nullable=False),  # agent | customer | judge
    Column("model", String, nullable=False),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("cached_tokens", Integer),
    Column("reasoning_tokens", Integer),
    Column("latency_ms", Float),  # API time, excluding rate-limit waits
    Column("wait_ms", Float),  # time spent throttled / backing off after 429s
    Column("tool_calls", Integer),
    Column("n_messages", Integer),
    Column("with_tools", Boolean),
    Column("cost_usd", Float),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)),
)

# --------------------------------------------------------------------------- engine
_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True, pool_size=10, max_overflow=20)
    return _engine


def wait_for_db(timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while True:
        try:
            with get_engine().connect() as c:
                c.execute(text("SELECT 1"))
            return
        except Exception:
            if time.time() > deadline:
                raise
            time.sleep(1)


@contextmanager
def store_tx(schema: str):
    """A transaction whose store-table queries hit ``schema``."""
    with get_engine().begin() as conn:
        yield conn.execution_options(schema_translate_map={None: schema})


def init_app_tables() -> None:
    app_md.create_all(get_engine())
    # lightweight forward migration for databases created by older versions
    dialect = get_engine().dialect
    with get_engine().begin() as c:
        for table in (runs, llm_calls):
            for col in table.columns:
                if col.primary_key:
                    continue
                ddl = col.type.compile(dialect=dialect)
                c.execute(text(f'ALTER TABLE public.{table.name} ADD COLUMN IF NOT EXISTS "{col.name}" {ddl}'))


def scoped(conn: Connection, schema: str) -> Connection:
    return conn.execution_options(schema_translate_map={None: schema})
