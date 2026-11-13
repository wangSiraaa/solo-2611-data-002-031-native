"""Synthetic sample-file generator.

Every file used by the test-suite is produced locally and
deterministically - nothing external is ever downloaded.  The small
samples are hand-checkable (see README for the manual arithmetic); the
big sample stresses streaming + memory limits.
"""

from __future__ import annotations

import json
import os
import random

# ---------------------------------------------------------------------------
# small.xml - hand-computed reference sample
#
# Document order of records (seq) and expected dispositions:
#   seq 1  customer b1/c1 "Alice"            -> published
#   seq 2  order    b1/o1  (2x150 + 1x0)     -> published, total 300
#   seq 3  order    b1/o2  -> c2 (FORWARD ref, 3x7) -> published, total 21
#   seq 4  customer b1/c2 "Bob"              -> published
#   seq 5  customer b1a/c1 "Alice Junior"    -> published (same id, other batch)
#   seq 6  order    b1a/o1 (1x150)           -> published, total 150
#   seq 7  order    b1a/oX -> c9             -> REJECT unknown_customer
#   seq 8  order    b1a/oP -> c2 (parent batch only) -> REJECT unknown_customer
#   seq 9  customer b2/c1 "Carol"            -> published
#   seq 10 order    b2/o9  (10x25)           -> published, total 250
#   seq 11 order    b2/oBad (good line + qty=0 line)   -> REJECT bad_line
#   seq 12 customer b2/cBad (no name)        -> REJECT missing_name
#   seq 13 order    b2/oUseBad -> cBad       -> REJECT customer_rejected
#   seq 14 order    b2/oEmpty (no lines)     -> REJECT no_lines
# FAKE1/FAKE2 live inside an extension subtree and must NOT be seen.
# Grand totals: 4 customers, 4 orders, 5 lines, 721 cents, 6 rejects.
# ---------------------------------------------------------------------------

SMALL_BODY = """\
  <{p}batch id="b1">
    <{p}customer id="c1" name="Alice"/>
    <{p}order id="o1" customer_id="c1">
      <{p}line sku="A" qty="2" unit_cents="150"/>
      <{p}line sku="B" qty="1" unit_cents="0"/>
    </{p}order>
    <{p}order id="o2" customer_id="c2">
      <{p}line sku="C" qty="3" unit_cents="7"/>
    </{p}order>
    <{p}customer id="c2" name="Bob"/>
    <{p}batch id="b1a">
      <{p}customer id="c1" name="Alice Junior"/>
      <{p}order id="o1" customer_id="c1">
        <{p}line sku="A" qty="1" unit_cents="150"/>
      </{p}order>
      <{p}order id="oX" customer_id="c9">
        <{p}line sku="Z" qty="1" unit_cents="5"/>
      </{p}order>
      <{p}order id="oP" customer_id="c2">
        <{p}line sku="Z" qty="2" unit_cents="5"/>
      </{p}order>
    </{p}batch>
  </{p}batch>
  <{p2}batch id="b2">
    <{p2}customer id="c1" name="Carol"/>
    <{p2}order id="o9" customer_id="c1">
      <{p2}line sku="D" qty="10" unit_cents="25"/>
    </{p2}order>
    <x:vendor>
      <{p2}order id="FAKE1" customer_id="c1"><{p2}line sku="F" qty="1" unit_cents="1"/></{p2}order>
      <{p3}order id="FAKE2" customer_id="c1"><{p3}line sku="F" qty="1" unit_cents="1"/></{p3}order>
    </x:vendor>
    <{p2}order id="oBad" customer_id="c1">
      <{p2}line sku="E" qty="2" unit_cents="10"/>
      <{p2}line sku="F" qty="0" unit_cents="10"/>
    </{p2}order>
    <{p2}customer id="cBad"/>
    <{p2}order id="oUseBad" customer_id="cBad">
      <{p2}line sku="G" qty="1" unit_cents="1"/>
    </{p2}order>
    <{p2}order id="oEmpty" customer_id="c1"/>
  </{p2}batch>
"""


def render_small(prefixed: bool) -> str:
    """Render small.xml; ``prefixed`` swaps the default namespace for a
    prefix binding (namespace prefix substitution must not change the
    imported result)."""
    if prefixed:
        head = '<v:export xmlns:v="urn:orders:v1" xmlns:x="urn:ext:vendor">'
        tail = "</v:export>"
        body = SMALL_BODY.format(p="v:", p2="v:", p3="v:")
    else:
        head = ('<export xmlns="urn:orders:v1" '
                'xmlns:o="urn:orders:v1" xmlns:x="urn:ext:vendor">')
        tail = "</export>"
        # b2 uses the o: prefix (prefix substitution inside one file);
        # FAKE1 stays in the default namespace, FAKE2 uses o:.
        body = SMALL_BODY.format(p="", p2="o:", p3="o:")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            + head + "\n" + body + tail + "\n")


# ---------------------------------------------------------------------------
# bad_records.xml - every record-level quarantine path
# ---------------------------------------------------------------------------

BAD_RECORDS_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<export xmlns="urn:orders:v1">
  <batch id="bb">
    <customer id="c1" name="Ok"/>
    <order id="ok1" customer_id="c1"><line sku="A" qty="1" unit_cents="1"/></order>
    <order id="q0" customer_id="c1"><line sku="A" qty="0" unit_cents="5"/></order>
    <order id="qneg" customer_id="c1"><line sku="A" qty="-2" unit_cents="5"/></order>
    <order id="qabc" customer_id="c1"><line sku="A" qty="abc" unit_cents="5"/></order>
    <order id="qmiss" customer_id="c1"><line sku="A" unit_cents="5"/></order>
    <order id="qws" customer_id="c1"><line sku="A" qty=" 1" unit_cents="5"/></order>
    <order id="uneg" customer_id="c1"><line sku="A" qty="1" unit_cents="-1"/></order>
    <order id="umiss" customer_id="c1"><line sku="A" qty="1"/></order>
    <order id="smiss" customer_id="c1"><line qty="1" unit_cents="5"/></order>
    <order id="sempty" customer_id="c1"><line sku="" qty="1" unit_cents="5"/></order>
    <order id="mixed" customer_id="c1">
      <line sku="G" qty="1" unit_cents="100"/>
      <line sku="B" qty="x" unit_cents="100"/>
    </order>
    <order id="nol" customer_id="c1"></order>
    <order customer_id="c1"><line sku="A" qty="1" unit_cents="1"/></order>
    <order id="noc"><line sku="A" qty="1" unit_cents="1"/></order>
    <order id="nest" customer_id="c1">
      <line sku="A" qty="1" unit_cents="1"/>
      <order id="inner" customer_id="c1"><line sku="I" qty="1" unit_cents="1"/></order>
    </order>
    <order id="nestb" customer_id="c1">
      <batch id="innerb"><order id="innero" customer_id="c1"><line sku="I" qty="1" unit_cents="1"/></order></batch>
    </order>
    <customer id="noname"/>
    <customer name="NoId"/>
    <order id="usebad" customer_id="noname"><line sku="A" qty="1" unit_cents="1"/></order>
    <order id="ghost" customer_id="nope"><line sku="A" qty="1" unit_cents="1"/></order>
    <order id="big" customer_id="c1"><line sku="{BIG_SKU}" qty="1" unit_cents="1"/></order>
    <customer id="huge" name="{BIG_NAME}"/>
    <order id="usehuge" customer_id="huge"><line sku="A" qty="1" unit_cents="1"/></order>
  </batch>
</export>
"""


def render_bad_records() -> str:
    big_sku = "S" * (70 * 1024)   # pushes the order subtree past 64 KiB
    big_name = "N" * (70 * 1024)  # pushes the customer subtree past 64 KiB
    return BAD_RECORDS_TEMPLATE.replace("{BIG_SKU}", big_sku).replace("{BIG_NAME}", big_name)


# ---------------------------------------------------------------------------
# extension.xml - foreign namespaces and unknown business tags are ignored
# ---------------------------------------------------------------------------

EXTENSION_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<export xmlns="urn:orders:v1" xmlns:x="urn:ext" xmlns:y="urn:other">
  <x:config>
    <x:order id="F1" x:customer_id="x"><x:line sku="Z" qty="9" unit_cents="9"/></x:order>
  </x:config>
  <batch id="b1">
    <customer id="c1" name="A"/>
    <y:wrapper>
      <order id="F2" customer_id="c1"><line sku="Z" qty="9" unit_cents="9"/></order>
    </y:wrapper>
    <order id="real" customer_id="c1"><line sku="A" qty="1" unit_cents="10"/></order>
    <unknownbiz>
      <order id="F3" customer_id="c1"><line sku="Z" qty="9" unit_cents="9"/></order>
    </unknownbiz>
  </batch>
</export>
"""


def render_deep(n_batches: int) -> str:
    """export + ``n_batches`` nested batches + customer/order/line.

    Max element depth = 1 (export) + n_batches + 1 (order) + 1 (line).
    """
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<export xmlns="urn:orders:v1">']
    parts += [f'<batch id="d{i}">' for i in range(n_batches)]
    parts.append('<customer id="c1" name="Deep"/>')
    parts.append('<order id="o1" customer_id="c1">'
                 '<line sku="A" qty="1" unit_cents="5"/></order>')
    parts.append('</batch>' * n_batches)
    parts.append('</export>')
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# document-level failure samples
# ---------------------------------------------------------------------------

def docfailure_files(small_xml: str) -> dict[str, bytes]:
    small = small_xml.encode("utf-8")
    cut = small[: int(len(small) * 0.6)]
    return {
        "truncated.xml": cut,  # file tail chopped off mid-document
        "dtd.xml": (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE export [<!ENTITY greeting "hello">]>\n'
            '<export xmlns="urn:orders:v1"><batch id="b1">'
            '<customer id="c1" name="&greeting;"/></batch></export>\n'
        ).encode(),
        "entity_ref.xml": (
            '<export xmlns="urn:orders:v1"><batch id="b1">'
            '<customer id="c1" name="A &nosuch; B"/></batch></export>\n'
        ).encode(),
        "dup_batch.xml": (
            '<export xmlns="urn:orders:v1"><batch id="b1"/>'
            '<batch id="b1"/></export>\n'
        ).encode(),
        "dup_customer.xml": (
            '<export xmlns="urn:orders:v1"><batch id="b1">'
            '<customer id="c1" name="A"/><customer id="c1" name="B"/>'
            '</batch></export>\n'
        ).encode(),
        "dup_order.xml": (
            '<export xmlns="urn:orders:v1"><batch id="b1">'
            '<customer id="c1" name="A"/>'
            '<order id="o1" customer_id="c1"><line sku="A" qty="1" unit_cents="1"/></order>'
            '<order id="o1" customer_id="c1"><line sku="A" qty="1" unit_cents="1"/></order>'
            '</batch></export>\n'
        ).encode(),
        "bad_root.xml": b'<notexport xmlns="urn:orders:v1"/>\n',
        "wrong_ns.xml": b'<export xmlns="urn:orders:v2"/>\n',
        "no_ns_root.xml": b'<export/>\n',
        "outside_batch.xml": (
            '<export xmlns="urn:orders:v1">'
            '<customer id="c1" name="A"/></export>\n'
        ).encode(),
        "junk_after.xml": (
            '<export xmlns="urn:orders:v1"/><export xmlns="urn:orders:v1"/>\n'
        ).encode(),
        "mismatched.xml": (
            '<export xmlns="urn:orders:v1"><batch id="b1"></export>\n'
        ).encode(),
        "bad_encoding.xml": (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<export xmlns="urn:orders:v1"><batch id="b1">'
            b'<customer id="c1" name="\xff\xfe"/></batch></export>\n'
        ),
    }


# ---------------------------------------------------------------------------
# big.xml - streaming / memory stress sample (deterministic)
# ---------------------------------------------------------------------------

def write_big(path, n_orders=1_200_000, seed=7,
              orders_per_sub=1200, customers_per_sub=400, subs_per_top=10):
    """Write a large, fully valid export.

    Orders are written *before* the customers they reference, so every
    order is a forward reference.  Returns the expected totals (also
    stored to ``<path>.expected.json``).
    """
    rng = random.Random(seed)
    n_subs = (n_orders + orders_per_sub - 1) // orders_per_sub
    total_cents = 0
    lines = 0
    customers = 0
    batches = 0
    order_seq = 0
    cust_seq = 0
    with open(path, "wb", buffering=1 << 20) as fh:
        w = lambda s: fh.write(s.encode("utf-8"))
        w('<?xml version="1.0" encoding="UTF-8"?>\n<export xmlns="urn:orders:v1">\n')
        subs_done = 0
        top = 0
        while subs_done < n_subs:
            w(f'<batch id="top-{top}">\n')
            batches += 1
            for _ in range(subs_per_top):
                if subs_done >= n_subs:
                    break
                sub = subs_done
                w(f'<batch id="sub-{sub}">\n')
                batches += 1
                base = cust_seq
                n_o = min(orders_per_sub, n_orders - order_seq)
                # orders first -> forward references to customers below
                for i in range(n_o):
                    oid = f"o{order_seq}"
                    cref = f"c{base + (i * 7 + 3) % customers_per_sub}"
                    parts = [f'<order id="{oid}" customer_id="{cref}">']
                    for k in range(2):
                        qty = rng.randint(1, 5)
                        unit = rng.randint(0, 9999)
                        total_cents += qty * unit
                        lines += 1
                        parts.append(
                            f'<line sku="SKU-{k}" qty="{qty}" unit_cents="{unit}"/>'
                        )
                    parts.append("</order>\n")
                    w("".join(parts))
                    order_seq += 1
                for j in range(customers_per_sub):
                    w(f'<customer id="c{cust_seq}" name="Customer {cust_seq}"/>\n')
                    cust_seq += 1
                    customers += 1
                w("</batch>\n")
                subs_done += 1
            w("</batch>\n")
            top += 1
        w("</export>\n")
    expected = {
        "customers": customers,
        "orders": n_orders,
        "lines": lines,
        "batches": batches,
        "total_cents": total_cents,
        "rejects": 0,
    }
    with open(path + ".expected.json", "w", encoding="utf-8") as fh:
        json.dump(expected, fh, indent=2)
    return expected


# ---------------------------------------------------------------------------

def generate_all(out_dir, big_orders=1_200_000, seed=7, write_big_file=True):
    """Write the complete synthetic sample set into ``out_dir``."""
    os.makedirs(out_dir, exist_ok=True)
    small = render_small(prefixed=False)
    files: dict[str, bytes] = {
        "small.xml": small.encode("utf-8"),
        "small_prefixed.xml": render_small(prefixed=True).encode("utf-8"),
        "bad_records.xml": render_bad_records().encode("utf-8"),
        "extension.xml": EXTENSION_XML.encode("utf-8"),
        "deep_ok.xml": render_deep(29).encode("utf-8"),   # max depth exactly 32
        "deep_fail.xml": render_deep(30).encode("utf-8"),  # max depth 33
    }
    files.update(docfailure_files(small))
    written = []
    for name, data in sorted(files.items()):
        p = os.path.join(out_dir, name)
        with open(p, "wb") as fh:
            fh.write(data)
        written.append(p)
    if write_big_file:
        write_big(os.path.join(out_dir, "big.xml"),
                  n_orders=big_orders, seed=seed)
        written.append(os.path.join(out_dir, "big.xml"))
    return written
