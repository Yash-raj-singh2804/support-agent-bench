"""A compact, exact description of the store the attacker must write scenarios against (built from the seed)."""

from .. import seed_data as sd


def world_summary() -> str:
    vmap = {v[0]: v for v in sd.VARIANTS}
    pname = {p["product_id"]: p["name"] for p in sd.PRODUCTS}
    lines = [f"TODAY: {sd.STORE_TODAY}", "", "PRODUCTS AND VARIANTS (item_id: options, price, available):"]
    for p in sd.PRODUCTS:
        lines.append(f"- {p['name']} (product {p['product_id']})")
        for iid, pid, opts, price, avail in sd.VARIANTS:
            if pid == p["product_id"]:
                lines.append(f"    {iid}: {opts}, ${price}, {'in stock' if avail else 'OUT OF STOCK'}")
    pms: dict[str, list] = {}
    for pm in sd.PAYMENT_METHODS:
        pms.setdefault(pm[1], []).append(pm)
    lines += ["", "CUSTOMERS (user_id, name, email, zip, tier, address, payment methods):"]
    for uid, first, last, email, zip_, addr, tier in sd.USERS:
        pm_txt = "; ".join(
            f"{p[0]} ({p[2]}{' ' + p[3] if p[3] else ''}{' ending ' + p[4] if p[4] else ''}"
            f"{', balance $' + str(p[5]) if p[5] is not None else ''})" for p in pms.get(uid, []))
        a = addr
        lines.append(f"- {uid}: {first} {last}, {email}, zip {zip_}, tier {tier}, "
                     f"{a['address1']}{' ' + a['address2'] if a['address2'] else ''}, {a['city']} {a['state']}; "
                     f"payment: {pm_txt}")
    lines += ["", "ORDERS (order_id, customer, status, placed, delivered, items, paid with, shipping):"]
    for oid, uid, status, placed, delivered, items, pm in sd.ORDERS:
        its = []
        for spec in items:
            iid, paid = spec if isinstance(spec, tuple) else (spec, None)
            v = vmap[iid]
            its.append(f"{iid} {pname[v[1]]} {v[2]} ${paid if paid is not None else v[3]}")
        ship = sd.SHIPMENTS.get(oid)
        ship_txt = (f"carrier status {ship[0]}, shipped {ship[1]}, est. delivery {ship[2]}" if ship else "not shipped")
        method = "express" if oid in sd.EXPRESS_ORDERS else "standard"
        lines.append(f"- {oid}: {uid}, {status}, placed {placed}, delivered {delivered or '-'}, items [{'; '.join(its)}], "
                     f"paid with {pm}, {method} shipping, {ship_txt}")
    return "\n".join(lines)
