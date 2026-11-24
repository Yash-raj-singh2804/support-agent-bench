"""Deterministic seed data for the mock store.

Everything here is hand-written so that tasks in ``data/tasks.json`` can refer to
concrete IDs, prices and dates. The store's "today" is defined in the policy.
"""

from decimal import Decimal as D

STORE_TODAY = "2026-11-10"


def _addr(a1, a2, city, state, zip_, country="USA"):
    return {"address1": a1, "address2": a2, "city": city, "state": state, "country": country, "zip": zip_}


PRODUCTS = [
    {"product_id": "9523456873", "name": "T-Shirt"},
    {"product_id": "8310926033", "name": "Water Bottle"},
    {"product_id": "6992792935", "name": "Headphones"},
    {"product_id": "7453219980", "name": "Desk Lamp"},
    {"product_id": "5148306472", "name": "Running Shoes"},
]

VARIANTS = [
    # T-Shirt
    ("1000000001", "9523456873", {"color": "blue", "size": "M"}, D("24.99"), True),
    ("1000000002", "9523456873", {"color": "blue", "size": "L"}, D("24.99"), True),
    ("1000000003", "9523456873", {"color": "black", "size": "M"}, D("26.49"), True),
    ("1000000004", "9523456873", {"color": "black", "size": "L"}, D("26.49"), False),
    ("1000000005", "9523456873", {"color": "white", "size": "S"}, D("22.99"), True),
    # Water Bottle
    ("2000000001", "8310926033", {"capacity": "500ml", "material": "stainless steel", "color": "green"}, D("34.50"), True),
    ("2000000002", "8310926033", {"capacity": "750ml", "material": "stainless steel", "color": "green"}, D("39.00"), True),
    ("2000000003", "8310926033", {"capacity": "750ml", "material": "plastic", "color": "blue"}, D("18.25"), True),
    ("2000000004", "8310926033", {"capacity": "1000ml", "material": "glass", "color": "clear"}, D("42.00"), False),
    # Headphones
    ("3000000001", "6992792935", {"type": "over-ear", "connectivity": "wireless", "color": "black"}, D("189.99"), True),
    ("3000000002", "6992792935", {"type": "over-ear", "connectivity": "wired", "color": "black"}, D("129.99"), True),
    ("3000000003", "6992792935", {"type": "in-ear", "connectivity": "wireless", "color": "white"}, D("149.00"), True),
    ("3000000004", "6992792935", {"type": "on-ear", "connectivity": "wireless", "color": "red"}, D("159.50"), False),
    # Desk Lamp
    ("4000000001", "7453219980", {"color": "black", "brightness": "low"}, D("45.00"), True),
    ("4000000002", "7453219980", {"color": "black", "brightness": "high"}, D("59.00"), True),
    ("4000000003", "7453219980", {"color": "silver", "brightness": "high"}, D("61.50"), True),
    # Running Shoes
    ("5000000001", "5148306472", {"size": "9", "color": "grey"}, D("110.00"), True),
    ("5000000002", "5148306472", {"size": "10", "color": "grey"}, D("110.00"), True),
    ("5000000003", "5148306472", {"size": "10", "color": "red"}, D("115.00"), True),
    ("5000000004", "5148306472", {"size": "11", "color": "grey"}, D("110.00"), False),
]

# user_id, first, last, email, zip, address, membership tier
USERS = [
    ("mia_garcia_4512", "Mia", "Garcia", "mia.garcia@example.com", "94110",
     _addr("123 Valencia St", "", "San Francisco", "CA", "94110"), "standard"),
    ("noah_kim_2290", "Noah", "Kim", "noah.kim@example.com", "10003",
     _addr("88 E 10th St", "Apt 4B", "New York", "NY", "10003"), "standard"),
    ("ava_patel_7731", "Ava", "Patel", "ava.patel@example.com", "60614",
     _addr("1500 N Clark St", "", "Chicago", "IL", "60614"), "plus"),
    ("liam_oconnor_3305", "Liam", "O'Connor", "liam.oconnor@example.com", "78704",
     _addr("2100 S Lamar Blvd", "Unit 12", "Austin", "TX", "78704"), "premium"),
    ("sofia_rossi_5508", "Sofia", "Rossi", "sofia.rossi@example.com", "98103",
     _addr("4012 Fremont Ave N", "", "Seattle", "WA", "98103"), "plus"),
    ("ethan_wright_9014", "Ethan", "Wright", "ethan.wright@example.com", "02139",
     _addr("77 Massachusetts Ave", "", "Cambridge", "MA", "02139"), "standard"),
    ("olivia_chen_6021", "Olivia", "Chen", "olivia.chen@example.com", "30309",
     _addr("1100 Peachtree St NE", "Apt 1502", "Atlanta", "GA", "30309"), "premium"),
    ("james_miller_8840", "James", "Miller", "james.miller@example.com", "85004",
     _addr("45 W Washington St", "", "Phoenix", "AZ", "85004"), "standard"),
    ("priya_shah_1187", "Priya", "Shah", "priya.shah@example.com", "94301",
     _addr("520 Cowper St", "", "Palo Alto", "CA", "94301"), "plus"),
    ("diego_lopez_4473", "Diego", "Lopez", "diego.lopez@example.com", "33131",
     _addr("801 Brickell Ave", "Unit 2204", "Miami", "FL", "33131"), "standard"),
    # Hard tasks: several near-identical orders (blue T-shirts, the same desk lamp) to tell apart by date and size.
    ("grace_thompson_6634", "Grace", "Thompson", "grace.thompson@example.com", "55408",
     _addr("2900 Hennepin Ave", "Apt 3", "Minneapolis", "MN", "55408"), "standard"),
]

PAYMENT_METHODS = [
    ("credit_card_4512", "mia_garcia_4512", "credit_card", "visa", "4512", None),
    ("gift_card_8801", "mia_garcia_4512", "gift_card", None, None, D("50.00")),
    ("paypal_2290", "noah_kim_2290", "paypal", None, None, None),
    ("gift_card_1177", "noah_kim_2290", "gift_card", None, None, D("12.00")),
    ("credit_card_7731", "ava_patel_7731", "credit_card", "mastercard", "7731", None),
    ("credit_card_3305", "liam_oconnor_3305", "credit_card", "visa", "3305", None),
    ("gift_card_6620", "liam_oconnor_3305", "gift_card", None, None, D("200.00")),
    ("paypal_5508", "sofia_rossi_5508", "paypal", None, None, None),
    ("credit_card_9014", "ethan_wright_9014", "credit_card", "amex", "9014", None),
    ("gift_card_4410", "ethan_wright_9014", "gift_card", None, None, D("5.00")),
    ("credit_card_6021", "olivia_chen_6021", "credit_card", "visa", "6021", None),
    ("gift_card_3390", "olivia_chen_6021", "gift_card", None, None, D("25.00")),
    ("paypal_8840", "james_miller_8840", "paypal", None, None, None),
    ("credit_card_8840", "james_miller_8840", "credit_card", "mastercard", "8840", None),
    ("credit_card_1187", "priya_shah_1187", "credit_card", "visa", "1187", None),
    ("gift_card_5512", "priya_shah_1187", "gift_card", None, None, D("100.00")),
    ("credit_card_4473", "diego_lopez_4473", "credit_card", "discover", "4473", None),
    ("credit_card_6634", "grace_thompson_6634", "credit_card", "visa", "6634", None),
    ("gift_card_2207", "grace_thompson_6634", "gift_card", None, None, D("30.00")),
    ("paypal_6634", "grace_thompson_6634", "paypal", None, None, None),
]

# order_id, user_id, status, placed_on, delivered_on, [item_id | (item_id, price_paid)], payment_method_id
ORDERS = [
    ("#W1000001", "mia_garcia_4512", "pending", "2026-11-08", None, ["1000000001", "2000000001"], "credit_card_4512"),
    ("#W1000002", "mia_garcia_4512", "delivered", "2026-10-22", "2026-10-28", ["3000000001"], "credit_card_4512"),
    ("#W1000003", "noah_kim_2290", "delivered", "2026-09-14", "2026-09-20", ["5000000001"], "paypal_2290"),
    ("#W1000004", "noah_kim_2290", "pending", "2026-11-09", None, ["4000000001"], "gift_card_1177"),
    ("#W1000005", "ava_patel_7731", "delivered", "2026-10-27", "2026-11-02", ["1000000003", "5000000002"], "credit_card_7731"),
    ("#W1000006", "ava_patel_7731", "processed", "2026-11-06", None, ["3000000003"], "credit_card_7731"),
    ("#W1000007", "liam_oconnor_3305", "pending", "2026-11-09", None, ["3000000002", "1000000005"], "credit_card_3305"),
    ("#W1000008", "liam_oconnor_3305", "delivered", "2026-10-30", "2026-11-05", ["2000000002", "4000000002"], "gift_card_6620"),
    ("#W1000009", "sofia_rossi_5508", "delivered", "2026-10-09", "2026-10-15", ["5000000003"], "paypal_5508"),
    ("#W1000010", "sofia_rossi_5508", "pending", "2026-11-07", None, ["2000000003"], "paypal_5508"),
    ("#W1000011", "ethan_wright_9014", "delivered", "2026-11-02", "2026-11-08", ["1000000002"], "credit_card_9014"),
    ("#W1000012", "ethan_wright_9014", "pending (items modified)", "2026-11-05", None, ["4000000003"], "credit_card_9014"),
    ("#W1000013", "olivia_chen_6021", "delivered", "2026-10-26", "2026-11-04", ["3000000003"], "credit_card_6021"),
    ("#W1000014", "olivia_chen_6021", "delivered", "2026-09-30", "2026-10-05", ["5000000001"], "credit_card_6021"),
    ("#W1000015", "james_miller_8840", "processed", "2026-10-28", None, ["4000000003"], "paypal_8840"),
    ("#W1000016", "james_miller_8840", "delivered", "2026-11-01", "2026-11-08", ["2000000001", "1000000005"], "credit_card_8840"),
    ("#W1000017", "priya_shah_1187", "delivered", "2026-10-20", "2026-10-25", ["3000000002"], "credit_card_1187"),
    ("#W1000018", "priya_shah_1187", "pending", "2026-11-09", None, ["4000000001", "2000000003"], "credit_card_1187"),
    ("#W1000019", "diego_lopez_4473", "delivered", "2026-11-01", "2026-11-07", [("1000000003", D("29.99"))], "credit_card_4473"),
    ("#W1000020", "diego_lopez_4473", "delivered", "2026-10-14", "2026-10-20", [("5000000003", D("129.00"))], "credit_card_4473"),
    ("#W1000021", "olivia_chen_6021", "delivered", "2026-11-03", "2026-11-07", ["3000000001"], "credit_card_6021"),
    # Grace's lookalike orders. #22: in the 30-day window, past the 7-day damage window. #23: inside both.
    # #24: pending. #25: past the return window.
    ("#W1000022", "grace_thompson_6634", "delivered", "2026-10-19", "2026-10-24", ["1000000001", "4000000001"], "credit_card_6634"),
    ("#W1000023", "grace_thompson_6634", "delivered", "2026-11-01", "2026-11-06", ["1000000002", "4000000001"], "credit_card_6634"),
    ("#W1000024", "grace_thompson_6634", "pending", "2026-11-08", None, ["1000000001", "5000000002"], "paypal_6634"),
    ("#W1000025", "grace_thompson_6634", "delivered", "2026-09-20", "2026-09-26", ["1000000001"], "credit_card_6634"),
]

# order_id: (carrier status, shipped_on, estimated_delivery); orders not listed haven't shipped yet
SHIPMENTS = {
    "#W1000002": ("delivered", "2026-10-24", "2026-10-28"),
    "#W1000003": ("delivered", "2026-09-16", "2026-09-20"),
    "#W1000005": ("delivered", "2026-10-29", "2026-11-01"),
    "#W1000006": ("in_transit", "2026-11-08", "2026-11-12"),
    "#W1000008": ("delivered", "2026-11-01", "2026-11-01"),
    "#W1000009": ("delivered", "2026-10-11", "2026-10-15"),
    "#W1000011": ("delivered", "2026-11-04", "2026-11-07"),
    "#W1000013": ("delivered", "2026-10-27", "2026-10-30"),
    "#W1000014": ("delivered", "2026-10-01", "2026-10-05"),
    "#W1000015": ("lost", "2026-10-30", "2026-11-03"),
    "#W1000016": ("delivered", "2026-11-02", "2026-11-05"),
    "#W1000017": ("delivered", "2026-10-21", "2026-10-25"),
    "#W1000019": ("delivered", "2026-11-03", "2026-11-07"),
    "#W1000020": ("delivered", "2026-10-16", "2026-10-20"),
    "#W1000021": ("delivered", "2026-11-04", "2026-11-07"),
    "#W1000022": ("delivered", "2026-10-21", "2026-10-24"),
    "#W1000023": ("delivered", "2026-11-03", "2026-11-06"),
    "#W1000025": ("delivered", "2026-09-22", "2026-09-26"),
}
EXPRESS_ORDERS = {"#W1000013"}


def rows():
    """Yield (table_name, list_of_row_dicts) in FK-safe order."""
    vmap = {v[0]: v for v in VARIANTS}
    pname = {p["product_id"]: p["name"] for p in PRODUCTS}
    umap = {u[0]: u for u in USERS}

    yield "users", [
        dict(user_id=u, first_name=f, last_name=l, email=e, zip=z, address=a, tier=t) for u, f, l, e, z, a, t in USERS
    ]
    yield "payment_methods", [
        dict(payment_method_id=p, user_id=u, source=s, brand=b, last_four=lf, balance=bal)
        for p, u, s, b, lf, bal in PAYMENT_METHODS
    ]
    yield "products", PRODUCTS
    yield "variants", [
        dict(item_id=i, product_id=p, options=o, price=pr, available=av) for i, p, o, pr, av in VARIANTS
    ]
    yield "orders", [
        dict(order_id=o, user_id=u, status=s, address=umap[u][5], placed_on=po, delivered_on=do, cancel_reason=None,
             shipping_method="express" if o in EXPRESS_ORDERS else "standard")
        for o, u, s, po, do, _, _ in ORDERS
    ]
    items, txs = [], []
    for o, _u, _s, _po, _do, item_specs, pm in ORDERS:
        total = D("0")
        for n, spec in enumerate(item_specs, start=1):
            iid, paid = spec if isinstance(spec, tuple) else (spec, None)
            _, pid, opts, price, _ = vmap[iid]
            price = paid if paid is not None else price
            items.append(dict(order_id=o, line_no=n, item_id=iid, product_id=pid, name=pname[pid], options=opts, price=price))
            total += price
        txs.append(dict(tx_id=f"{o}-T1", order_id=o, kind="payment", amount=total, payment_method_id=pm))
    yield "order_items", items
    yield "transactions", txs
    yield "order_requests", []
    yield "shipments", [
        dict(order_id=o, carrier="UPS", tracking_number=f"1Z999AA1{o[2:]}", status=st, shipped_on=sh, estimated_delivery=est)
        for o, (st, sh, est) in SHIPMENTS.items()
    ]
    yield "claims", []
    yield "store_credits", []
    yield "support_tickets", []


TIER_RETURN_DAYS = {"standard": 30, "plus": 45, "premium": 60}
EXPRESS_FEE = {"standard": D("9.99"), "plus": D("4.99"), "premium": D("0.00")}
