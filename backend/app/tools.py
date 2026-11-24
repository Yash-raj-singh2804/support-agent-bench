"""Tools the support agent can call. Each runs in its own transaction against an
episode schema and returns a string (JSON on success, ``Error: ...`` on failure).

Tools enforce *hard* constraints (order status, stock, balances, ownership of a
payment method). *Soft* policy - authentication, explicit confirmation, the
30-day return window, refusing goodwill credits - is the agent's job, exactly
as in τ-bench.
"""

import ast
import json
import operator
from collections import Counter
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import func, insert, select, update

from .db import (claims, order_items, order_requests, orders, payment_methods, products, shipments, store_credits, store_tx,
                 support_tickets, transactions, users, variants)
from .seed_data import EXPRESS_FEE
from .store import _norm


class ToolError(Exception):
    pass


PENDING = ("pending", "pending (items modified)")
CANCEL_REASONS = ("no longer needed", "ordered by mistake")
CLAIM_RESOLUTIONS = ("refund", "replacement")
LOST_RESOLUTIONS = ("refund", "reship")
TRANSFER_REASONS = ("fraud_or_security", "legal_or_dispute", "high_value_claim", "out_of_scope", "customer_request")
MAX_STORE_CREDIT = Decimal("25.00")


def _j(obj: Any) -> str:
    return json.dumps(_norm(obj), indent=2)


def _one(conn, stmt, what: str):
    row = conn.execute(stmt).mappings().first()
    if row is None:
        raise ToolError(f"{what} not found")
    return dict(row)


def _order(conn, order_id: str, lock: bool = False):
    stmt = select(orders).where(orders.c.order_id == order_id)
    if lock:
        stmt = stmt.with_for_update()
    return _one(conn, stmt, "order")


def _items(conn, order_id: str):
    return [dict(r) for r in conn.execute(
        select(order_items).where(order_items.c.order_id == order_id).order_by(order_items.c.line_no)
    ).mappings()]


def _user_pm(conn, user_id: str, pm_id: str):
    pm = conn.execute(select(payment_methods).where(payment_methods.c.payment_method_id == pm_id)).mappings().first()
    if pm is None or pm["user_id"] != user_id:
        raise ToolError("payment method not found for this user")
    return dict(pm)


def _next_id(conn, table, col, order_id: str, prefix: str) -> str:
    n = conn.execute(select(func.count()).select_from(table).where(table.c.order_id == order_id)).scalar_one()
    return f"{order_id}-{prefix}{n + 1}"


def _add_tx(conn, order_id: str, kind: str, amount: Decimal, pm_id: str):
    tx_id = _next_id(conn, transactions, transactions.c.tx_id, order_id, "T")
    conn.execute(insert(transactions).values(tx_id=tx_id, order_id=order_id, kind=kind, amount=amount, payment_method_id=pm_id))


def _adjust_gift_card(conn, pm: dict, delta: Decimal):
    if pm["source"] != "gift_card":
        return
    conn.execute(update(payment_methods).where(payment_methods.c.payment_method_id == pm["payment_method_id"])
                 .values(balance=payment_methods.c.balance + delta))


def _net_paid(conn, order_id: str) -> dict[str, Decimal]:
    """Net amount currently paid per payment method (payments minus refunds)."""
    net: dict[str, Decimal] = {}
    for t in conn.execute(select(transactions).where(transactions.c.order_id == order_id).order_by(transactions.c.tx_id)).mappings():
        sign = 1 if t["kind"] == "payment" else -1
        net[t["payment_method_id"]] = net.get(t["payment_method_id"], Decimal("0")) + sign * t["amount"]
    return net


def _original_pm(conn, order_id: str) -> str:
    return conn.execute(select(transactions.c.payment_method_id).where(
        transactions.c.order_id == order_id, transactions.c.kind == "payment").order_by(transactions.c.tx_id)).scalars().first()


def _pm(conn, pm_id: str) -> dict:
    return dict(conn.execute(select(payment_methods).where(payment_methods.c.payment_method_id == pm_id)).mappings().one())


def _refund(conn, order_id: str, amount: Decimal, pm_id: str):
    _add_tx(conn, order_id, "refund", amount, pm_id)
    _adjust_gift_card(conn, _pm(conn, pm_id), amount)  # gift-card refunds are immediate


def _match_lines(lines: list[dict], item_ids: list[str]) -> list[dict]:
    """Pick one order line per requested item id (handles duplicates)."""
    if not item_ids:
        raise ToolError("item_ids must not be empty")
    pool = list(lines)
    picked = []
    for iid in item_ids:
        for ln in pool:
            if ln["item_id"] == iid:
                picked.append(ln)
                pool.remove(ln)
                break
        else:
            raise ToolError(f"item {iid} not found in order (or requested more times than ordered)")
    return picked


def _swap_targets(conn, lines: list[dict], new_item_ids: list[str]) -> list[dict]:
    if len(lines) != len(new_item_ids):
        raise ToolError("item_ids and new_item_ids must have the same length")
    out = []
    for ln, nid in zip(lines, new_item_ids):
        v = conn.execute(select(variants).where(variants.c.item_id == nid)).mappings().first()
        if v is None:
            raise ToolError(f"new item {nid} not found")
        if v["product_id"] != ln["product_id"]:
            raise ToolError(f"new item {nid} is not the same product as item {ln['item_id']}")
        if nid == ln["item_id"]:
            raise ToolError(f"new item {nid} is identical to the old item")
        if not v["available"]:
            raise ToolError(f"new item {nid} is not available")
        out.append(dict(v))
    return out


# --------------------------------------------------------------------------- read tools
def find_user_id_by_email(conn, email: str) -> str:
    r = conn.execute(select(users.c.user_id).where(func.lower(users.c.email) == email.strip().lower())).first()
    if r is None:
        raise ToolError("user not found")
    return r[0]


def find_user_id_by_name_zip(conn, first_name: str, last_name: str, zip: str) -> str:
    r = conn.execute(select(users.c.user_id).where(
        func.lower(users.c.first_name) == first_name.strip().lower(),
        func.lower(users.c.last_name) == last_name.strip().lower(),
        users.c.zip == zip.strip(),
    )).first()
    if r is None:
        raise ToolError("user not found")
    return r[0]


def get_user_details(conn, user_id: str) -> str:
    u = _one(conn, select(users).where(users.c.user_id == user_id), "user")
    pms = conn.execute(select(payment_methods).where(payment_methods.c.user_id == user_id)
                       .order_by(payment_methods.c.payment_method_id)).mappings().all()
    oids = conn.execute(select(orders.c.order_id).where(orders.c.user_id == user_id).order_by(orders.c.order_id)).scalars().all()
    u["name"] = {"first_name": u.pop("first_name"), "last_name": u.pop("last_name")}
    u["payment_methods"] = [{k: v for k, v in dict(p).items() if v is not None and k != "user_id"} for p in pms]
    u["orders"] = list(oids)
    return _j(u)


def get_order_details(conn, order_id: str) -> str:
    o = _order(conn, order_id)
    o["items"] = [{k: v for k, v in it.items() if k not in ("order_id",)} for it in _items(conn, order_id)]
    o["payment_history"] = [
        {k: v for k, v in dict(t).items() if k != "order_id"}
        for t in conn.execute(select(transactions).where(transactions.c.order_id == order_id).order_by(transactions.c.tx_id)).mappings()
    ]
    reqs = conn.execute(select(order_requests).where(order_requests.c.order_id == order_id)
                        .order_by(order_requests.c.request_id)).mappings().all()
    if reqs:
        o["requests"] = [{k: v for k, v in dict(r).items() if k != "order_id"} for r in reqs]
    cl = conn.execute(select(claims).where(claims.c.order_id == order_id).order_by(claims.c.claim_id)).mappings().all()
    if cl:
        o["claims"] = [{k: v for k, v in dict(r).items() if k != "order_id"} for r in cl]
    cr = conn.execute(select(store_credits).where(store_credits.c.order_id == order_id)
                      .order_by(store_credits.c.credit_id)).mappings().all()
    if cr:
        o["credits_and_adjustments"] = [{k: v for k, v in dict(r).items() if k not in ("order_id", "user_id")} for r in cr]
    return _j({k: v for k, v in o.items() if v is not None})


def list_all_product_types(conn) -> str:
    rows = conn.execute(select(products).order_by(products.c.name)).mappings().all()
    return _j({r["name"]: r["product_id"] for r in rows})


def get_product_details(conn, product_id: str) -> str:
    p = _one(conn, select(products).where(products.c.product_id == product_id), "product")
    vs = conn.execute(select(variants).where(variants.c.product_id == product_id).order_by(variants.c.item_id)).mappings().all()
    p["variants"] = {v["item_id"]: {"options": v["options"], "price": v["price"], "available": v["available"]} for v in vs}
    return _j(p)


# --------------------------------------------------------------------------- write tools
def cancel_pending_order(conn, order_id: str, reason: str) -> str:
    o = _order(conn, order_id, lock=True)
    if o["status"] not in PENDING:
        raise ToolError(f"order is '{o['status']}', only pending orders can be cancelled")
    if reason not in CANCEL_REASONS:
        raise ToolError(f"reason must be one of {list(CANCEL_REASONS)}")
    for pm_id, amt in sorted(_net_paid(conn, order_id).items()):
        if amt > 0:
            _refund(conn, order_id, amt, pm_id)
    conn.execute(update(orders).where(orders.c.order_id == order_id).values(status="cancelled", cancel_reason=reason))
    return get_order_details(conn, order_id)


def modify_pending_order_address(conn, order_id: str, address1: str, address2: str, city: str, state: str, country: str, zip: str) -> str:
    o = _order(conn, order_id, lock=True)
    if o["status"] not in PENDING:
        raise ToolError(f"order is '{o['status']}', only pending orders can be modified")
    addr = {"address1": address1, "address2": address2, "city": city, "state": state, "country": country, "zip": zip}
    conn.execute(update(orders).where(orders.c.order_id == order_id).values(address=addr))
    return get_order_details(conn, order_id)


def modify_pending_order_items(conn, order_id: str, item_ids: list[str], new_item_ids: list[str], payment_method_id: str) -> str:
    o = _order(conn, order_id, lock=True)
    if o["status"] != "pending":
        raise ToolError(f"order is '{o['status']}', items can only be modified once on a 'pending' order")
    lines = _match_lines(_items(conn, order_id), item_ids)
    targets = _swap_targets(conn, lines, new_item_ids)
    pm = _user_pm(conn, o["user_id"], payment_method_id)
    diff = sum((v["price"] for v in targets), Decimal("0")) - sum((ln["price"] for ln in lines), Decimal("0"))
    if pm["source"] == "gift_card" and diff > 0 and pm["balance"] < diff:
        raise ToolError("insufficient gift card balance to pay for the price difference")
    for ln, v in zip(lines, targets):
        conn.execute(update(order_items).where(order_items.c.order_id == order_id, order_items.c.line_no == ln["line_no"])
                     .values(item_id=v["item_id"], options=v["options"], price=v["price"]))
    if diff != 0:
        _add_tx(conn, order_id, "payment" if diff > 0 else "refund", abs(diff), payment_method_id)
        _adjust_gift_card(conn, pm, -diff)
    conn.execute(update(orders).where(orders.c.order_id == order_id).values(status="pending (items modified)"))
    return get_order_details(conn, order_id)


def return_delivered_order_items(conn, order_id: str, item_ids: list[str], payment_method_id: str) -> str:
    o = _order(conn, order_id, lock=True)
    if o["status"] != "delivered":
        raise ToolError(f"order is '{o['status']}', only delivered orders can be returned")
    lines = _match_lines(_items(conn, order_id), item_ids)
    pm = _user_pm(conn, o["user_id"], payment_method_id)
    original = conn.execute(select(transactions.c.payment_method_id).where(
        transactions.c.order_id == order_id, transactions.c.kind == "payment").order_by(transactions.c.tx_id)).scalars().first()
    if pm["source"] != "gift_card" and payment_method_id != original:
        raise ToolError("refund must go to the original payment method or an existing gift card")
    amount = sum((ln["price"] for ln in lines), Decimal("0"))
    rid = _next_id(conn, order_requests, order_requests.c.request_id, order_id, "R")
    conn.execute(insert(order_requests).values(
        request_id=rid, order_id=order_id, kind="return", item_ids=sorted(item_ids), new_item_ids=None,
        payment_method_id=payment_method_id, amount=amount, status="requested"))
    conn.execute(update(orders).where(orders.c.order_id == order_id).values(status="return requested"))
    return get_order_details(conn, order_id)


def exchange_delivered_order_items(conn, order_id: str, item_ids: list[str], new_item_ids: list[str], payment_method_id: str) -> str:
    o = _order(conn, order_id, lock=True)
    if o["status"] != "delivered":
        raise ToolError(f"order is '{o['status']}', only delivered orders can be exchanged")
    lines = _match_lines(_items(conn, order_id), item_ids)
    targets = _swap_targets(conn, lines, new_item_ids)
    pm = _user_pm(conn, o["user_id"], payment_method_id)
    diff = sum((v["price"] for v in targets), Decimal("0")) - sum((ln["price"] for ln in lines), Decimal("0"))
    if pm["source"] == "gift_card" and diff > 0 and pm["balance"] < diff:
        raise ToolError("insufficient gift card balance to pay for the price difference")
    pairs = sorted(zip([ln["item_id"] for ln in lines], new_item_ids))
    rid = _next_id(conn, order_requests, order_requests.c.request_id, order_id, "R")
    conn.execute(insert(order_requests).values(
        request_id=rid, order_id=order_id, kind="exchange", item_ids=[p[0] for p in pairs], new_item_ids=[p[1] for p in pairs],
        payment_method_id=payment_method_id, amount=diff, status="requested"))
    conn.execute(update(orders).where(orders.c.order_id == order_id).values(status="exchange requested"))
    return get_order_details(conn, order_id)


def get_shipment_status(conn, order_id: str) -> str:
    o = _order(conn, order_id)
    sh = conn.execute(select(shipments).where(shipments.c.order_id == order_id)).mappings().first()
    if sh is None:
        return _j({"order_id": order_id, "status": "not_shipped", "order_status": o["status"],
                   "shipping_method": o["shipping_method"]})
    return _j({**dict(sh), "order_status": o["status"], "shipping_method": o["shipping_method"],
               "delivered_on": o["delivered_on"]})


def modify_pending_order_payment(conn, order_id: str, payment_method_id: str) -> str:
    o = _order(conn, order_id, lock=True)
    if o["status"] not in PENDING:
        raise ToolError(f"order is '{o['status']}', only pending orders can change payment method")
    pm = _user_pm(conn, o["user_id"], payment_method_id)
    net = {k: v for k, v in _net_paid(conn, order_id).items() if v > 0}
    total = sum(net.values(), Decimal("0"))
    if set(net) == {payment_method_id}:
        raise ToolError("the order is already paid with this payment method")
    if pm["source"] == "gift_card" and pm["balance"] < total:
        raise ToolError(f"insufficient gift card balance ({pm['balance']}) to cover the order total ({total})")
    for old_pm, amt in sorted(net.items()):
        _refund(conn, order_id, amt, old_pm)
    _add_tx(conn, order_id, "payment", total, payment_method_id)
    _adjust_gift_card(conn, pm, -total)
    return get_order_details(conn, order_id)


def upgrade_shipping(conn, order_id: str, payment_method_id: str) -> str:
    o = _order(conn, order_id, lock=True)
    if o["status"] not in PENDING:
        raise ToolError(f"order is '{o['status']}', shipping can only be upgraded before the order ships")
    if o["shipping_method"] == "express":
        raise ToolError("order already ships express")
    tier = conn.execute(select(users.c.tier).where(users.c.user_id == o["user_id"])).scalar_one()
    fee = EXPRESS_FEE[tier]
    if fee > 0:
        pm = _user_pm(conn, o["user_id"], payment_method_id)
        if pm["source"] == "gift_card" and pm["balance"] < fee:
            raise ToolError("insufficient gift card balance for the express fee")
        _add_tx(conn, order_id, "payment", fee, payment_method_id)
        _adjust_gift_card(conn, pm, -fee)
    conn.execute(update(orders).where(orders.c.order_id == order_id).values(shipping_method="express"))
    return _j({"order_id": order_id, "shipping_method": "express", "fee_charged": fee, "membership_tier": tier})


def report_damaged_items(conn, order_id: str, item_ids: list[str], resolution: str) -> str:
    o = _order(conn, order_id, lock=True)
    if o["status"] != "delivered":
        raise ToolError(f"order is '{o['status']}', damage claims are only for delivered orders without an open request")
    if resolution not in CLAIM_RESOLUTIONS:
        raise ToolError(f"resolution must be one of {list(CLAIM_RESOLUTIONS)}")
    lines = _match_lines(_items(conn, order_id), item_ids)
    if resolution == "replacement":
        for ln in lines:
            if not conn.execute(select(variants.c.available).where(variants.c.item_id == ln["item_id"])).scalar_one():
                raise ToolError(f"item {ln['item_id']} is out of stock; offer a refund instead")
    amount = sum((ln["price"] for ln in lines), Decimal("0"))
    pm_id = _original_pm(conn, order_id) if resolution == "refund" else None
    cid = _next_id(conn, claims, claims.c.claim_id, order_id, "C")
    conn.execute(insert(claims).values(claim_id=cid, order_id=order_id, kind="damaged", item_ids=sorted(item_ids),
                                       resolution=resolution, amount=amount, payment_method_id=pm_id))
    if resolution == "refund":
        _refund(conn, order_id, amount, pm_id)
    conn.execute(update(orders).where(orders.c.order_id == order_id).values(status=f"damage claim ({resolution})"))
    return get_order_details(conn, order_id)


def file_lost_package_claim(conn, order_id: str, resolution: str) -> str:
    o = _order(conn, order_id, lock=True)
    if o["status"] != "processed":
        raise ToolError(f"order is '{o['status']}', lost-package claims are only for shipped (processed) orders")
    if resolution not in LOST_RESOLUTIONS:
        raise ToolError(f"resolution must be one of {list(LOST_RESOLUTIONS)}")
    sh = conn.execute(select(shipments).where(shipments.c.order_id == order_id)).mappings().first()
    if sh is None or sh["status"] == "delivered":
        raise ToolError("carrier shows this package as delivered or not shipped")
    lines = _items(conn, order_id)
    amount = sum((ln["price"] for ln in lines), Decimal("0"))
    cid = _next_id(conn, claims, claims.c.claim_id, order_id, "C")
    pm_id = _original_pm(conn, order_id) if resolution == "refund" else None
    conn.execute(insert(claims).values(claim_id=cid, order_id=order_id, kind="lost", item_ids=sorted(ln["item_id"] for ln in lines),
                                       resolution=resolution, amount=amount, payment_method_id=pm_id))
    if resolution == "refund":
        for pm, amt in sorted(_net_paid(conn, order_id).items()):
            if amt > 0:
                _refund(conn, order_id, amt, pm)
        conn.execute(update(orders).where(orders.c.order_id == order_id).values(status="lost (refunded)"))
    else:
        conn.execute(update(shipments).where(shipments.c.order_id == order_id).values(status="reshipped"))
        conn.execute(update(orders).where(orders.c.order_id == order_id).values(status="lost (reshipped)"))
    return get_order_details(conn, order_id)


def issue_price_adjustment(conn, order_id: str, item_id: str) -> str:
    o = _order(conn, order_id, lock=True)
    if o["status"] == "cancelled":
        raise ToolError("order is cancelled")
    line = _match_lines(_items(conn, order_id), [item_id])[0]
    current = conn.execute(select(variants.c.price).where(variants.c.item_id == item_id)).scalar_one()
    if current >= line["price"]:
        raise ToolError(f"no price drop: paid {line['price']}, current price {current}")
    done = conn.execute(select(func.count()).select_from(store_credits).where(
        store_credits.c.order_id == order_id, store_credits.c.item_id == item_id,
        store_credits.c.reason == "price_adjustment")).scalar_one()
    if done:
        raise ToolError("a price adjustment was already issued for this item")
    diff = line["price"] - current
    pm_id = _original_pm(conn, order_id)
    cid = _next_id(conn, store_credits, store_credits.c.credit_id, order_id, "A")
    conn.execute(insert(store_credits).values(credit_id=cid, user_id=o["user_id"], order_id=order_id, reason="price_adjustment",
                                              amount=diff, payment_method_id=pm_id, item_id=item_id))
    _refund(conn, order_id, diff, pm_id)
    return get_order_details(conn, order_id)


def issue_store_credit(conn, user_id: str, order_id: str, amount: float, reason: str) -> str:
    o = _order(conn, order_id, lock=True)
    if o["user_id"] != user_id:
        raise ToolError("order does not belong to this user")
    if reason != "late_delivery":
        raise ToolError("reason must be 'late_delivery'")
    amt = Decimal(str(amount)).quantize(Decimal("0.01"))
    if amt <= 0 or amt > MAX_STORE_CREDIT:
        raise ToolError(f"amount must be between 0.01 and {MAX_STORE_CREDIT}")
    if conn.execute(select(func.count()).select_from(store_credits).where(
            store_credits.c.order_id == order_id, store_credits.c.reason == "late_delivery")).scalar_one():
        raise ToolError("a late-delivery credit was already issued for this order")
    gc = conn.execute(select(payment_methods).where(payment_methods.c.user_id == user_id, payment_methods.c.source == "gift_card")
                      .order_by(payment_methods.c.payment_method_id)).mappings().first()
    if gc is None:
        gc_id = f"gift_card_{user_id.rsplit('_', 1)[-1]}"
        conn.execute(insert(payment_methods).values(payment_method_id=gc_id, user_id=user_id, source="gift_card",
                                                    balance=Decimal("0")))
        gc = _pm(conn, gc_id)
    cid = f"{order_id}-L1"
    conn.execute(insert(store_credits).values(credit_id=cid, user_id=user_id, order_id=order_id, reason="late_delivery",
                                              amount=amt, payment_method_id=gc["payment_method_id"]))
    _adjust_gift_card(conn, dict(gc), amt)
    return _j({"credit_id": cid, "amount": amt, "credited_to": gc["payment_method_id"]})


def modify_user_address(conn, user_id: str, address1: str, address2: str, city: str, state: str, country: str, zip: str) -> str:
    _one(conn, select(users).where(users.c.user_id == user_id), "user")
    addr = {"address1": address1, "address2": address2, "city": city, "state": state, "country": country, "zip": zip}
    conn.execute(update(users).where(users.c.user_id == user_id).values(address=addr))
    return get_user_details(conn, user_id)


# --------------------------------------------------------------------------- misc
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return Decimal(str(node.value))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ToolError("unsupported expression")


def calculate(conn, expression: str) -> str:
    try:
        return format(_eval(ast.parse(expression, mode="eval")).quantize(Decimal("0.01")), "f")
    except ZeroDivisionError:
        raise ToolError("division by zero")
    except SyntaxError:
        raise ToolError("invalid expression")


def transfer_to_human_agents(conn, summary: str, reason: str, user_id: str | None = None) -> str:
    if reason not in TRANSFER_REASONS:
        raise ToolError(f"reason must be one of {list(TRANSFER_REASONS)}")
    if user_id:
        _one(conn, select(users).where(users.c.user_id == user_id), "user")
    n = conn.execute(select(func.count()).select_from(support_tickets)).scalar_one()
    tid = f"TKT-{n + 1:04d}"
    conn.execute(insert(support_tickets).values(ticket_id=tid, user_id=user_id or None, reason=reason))
    return f"Transfer successful. Ticket {tid} created."


# --------------------------------------------------------------------------- registry
_S = {"type": "string"}
_L = {"type": "array", "items": {"type": "string"}}


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required if required is not None else list(props)}


TOOLS: dict[str, tuple[Callable, str, dict, bool]] = {
    # name: (fn, description, json-schema, writes)
    "find_user_id_by_email": (find_user_id_by_email, "Find a user id by email.", _schema({"email": _S}), False),
    "find_user_id_by_name_zip": (find_user_id_by_name_zip, "Find a user id by first name, last name and zip code.",
                                 _schema({"first_name": _S, "last_name": _S, "zip": _S}), False),
    "get_user_details": (get_user_details, "Get a user's profile, payment methods and order ids.", _schema({"user_id": _S}), False),
    "get_order_details": (get_order_details, "Get an order's status, items, address and payment history. Order ids look like '#W1000001'.",
                          _schema({"order_id": _S}), False),
    "list_all_product_types": (list_all_product_types, "List all product types with their product ids.", _schema({}), False),
    "get_product_details": (get_product_details, "Get a product's variants (item ids, options, price, availability).",
                            _schema({"product_id": _S}), False),
    "cancel_pending_order": (cancel_pending_order,
                             "Cancel a pending order. Refunds go to the original payment methods (gift-card refunds are immediate). "
                             "reason must be 'no longer needed' or 'ordered by mistake'. Requires explicit user confirmation.",
                             _schema({"order_id": _S, "reason": {"type": "string", "enum": list(CANCEL_REASONS)}}), True),
    "modify_pending_order_address": (modify_pending_order_address, "Change the shipping address of a pending order. Requires explicit user confirmation.",
                                     _schema({"order_id": _S, "address1": _S, "address2": _S, "city": _S, "state": _S, "country": _S, "zip": _S}), True),
    "modify_pending_order_items": (modify_pending_order_items,
                                   "Swap items in a pending order for other available variants of the same product. Can only be done once per order. "
                                   "The price difference is charged/refunded to payment_method_id. Requires explicit user confirmation.",
                                   _schema({"order_id": _S, "item_ids": _L, "new_item_ids": _L, "payment_method_id": _S}), True),
    "return_delivered_order_items": (return_delivered_order_items,
                                     "Request a return for items of a delivered order. Refund goes to the original payment method or an existing gift card. "
                                     "Requires explicit user confirmation.",
                                     _schema({"order_id": _S, "item_ids": _L, "payment_method_id": _S}), True),
    "exchange_delivered_order_items": (exchange_delivered_order_items,
                                       "Request an exchange of items in a delivered order for other available variants of the same product. "
                                       "Price difference charged/refunded to payment_method_id. Requires explicit user confirmation.",
                                       _schema({"order_id": _S, "item_ids": _L, "new_item_ids": _L, "payment_method_id": _S}), True),
    "get_shipment_status": (get_shipment_status, "Get carrier tracking for an order: status, tracking number, ship date, estimated delivery.",
                            _schema({"order_id": _S}), False),
    "modify_pending_order_payment": (modify_pending_order_payment,
                                     "Switch a pending order to a different payment method: the old method is refunded and the new one charged the "
                                     "full order total. A gift card must cover the whole total. Requires explicit user confirmation.",
                                     _schema({"order_id": _S, "payment_method_id": _S}), True),
    "upgrade_shipping": (upgrade_shipping,
                         "Upgrade a pending order from standard to express shipping. The fee depends on the customer's membership tier "
                         "and is charged to payment_method_id. Requires explicit user confirmation.",
                         _schema({"order_id": _S, "payment_method_id": _S}), True),
    "report_damaged_items": (report_damaged_items,
                             "Open a damaged/defective item claim on a delivered order. resolution 'refund' refunds the items to the original "
                             "payment method immediately; 'replacement' ships the same item again. Requires explicit user confirmation.",
                             _schema({"order_id": _S, "item_ids": _L, "resolution": {"type": "string", "enum": list(CLAIM_RESOLUTIONS)}}), True),
    "file_lost_package_claim": (file_lost_package_claim,
                                "Open a lost-package claim for a shipped order that never arrived. resolution 'refund' refunds the order to the "
                                "original payment method; 'reship' sends it again. Requires explicit user confirmation.",
                                _schema({"order_id": _S, "resolution": {"type": "string", "enum": list(LOST_RESOLUTIONS)}}), True),
    "issue_price_adjustment": (issue_price_adjustment,
                               "Refund the difference when an item's current price is lower than what the customer paid. The difference goes "
                               "to the original payment method. Requires explicit user confirmation.",
                               _schema({"order_id": _S, "item_id": _S}), True),
    "issue_store_credit": (issue_store_credit,
                           "Credit a customer's gift card as compensation. Only reason supported: 'late_delivery'. Requires explicit user confirmation.",
                           _schema({"user_id": _S, "order_id": _S, "amount": {"type": "number"},
                                    "reason": {"type": "string", "enum": ["late_delivery"]}}), True),
    "modify_user_address": (modify_user_address,
                            "Change the default address on the customer's account. Does not change existing orders. Requires explicit user confirmation.",
                            _schema({"user_id": _S, "address1": _S, "address2": _S, "city": _S, "state": _S, "country": _S, "zip": _S}), True),
    "calculate": (calculate, "Evaluate an arithmetic expression, e.g. '189.99 - 129.99'.", _schema({"expression": _S}), False),
    "transfer_to_human_agents": (transfer_to_human_agents,
                                 "Transfer the customer to a human agent and open a support ticket. Only use for the escalation cases in the "
                                 "policy. Pass the authenticated user_id if known.",
                                 _schema({"summary": _S, "reason": {"type": "string", "enum": list(TRANSFER_REASONS)}, "user_id": _S},
                                         ["summary", "reason"]), True),
}

TERMINAL_TOOLS = {"transfer_to_human_agents"}


def tool_specs() -> list[dict]:
    return [{"name": n, "description": d, "parameters": s} for n, (_, d, s, _) in TOOLS.items()]


def run_tool(schema: str, name: str, args: dict) -> str:
    if name not in TOOLS:
        return f"Error: unknown tool {name}"
    fn = TOOLS[name][0]
    try:
        with store_tx(schema) as conn:
            return fn(conn, **args)
    except ToolError as e:
        return f"Error: {e}"
    except TypeError as e:
        return f"Error: bad arguments: {e}"
    except Exception as e:  # pragma: no cover - defensive
        return f"Error: {type(e).__name__}: {e}"
