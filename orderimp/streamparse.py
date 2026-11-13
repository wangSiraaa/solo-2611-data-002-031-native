"""Event-driven XML parser built on :mod:`xml.parsers.expat`.

Design notes
------------
* **Streaming** - the file is read in ``chunk_size`` byte blocks; expat
  never builds a tree, so there is nothing to "release": elements are
  handled and forgotten as events arrive.  Only the current
  customer/order record (bounded to 64 KiB of source) is kept in memory.
* **Namespaces** - expat expands names to ``"uri local"``.  Any element
  outside ``urn:orders:v1`` starts an *ignored subtree*: nothing inside
  it is recognised, not even business-looking pseudo orders.
* **Limits** - element nesting deeper than ``max_depth`` (32) is a
  document-level failure; a customer/order subtree larger than
  ``max_record_bytes`` (64 KiB) is quarantined as a single bad record.
* **DTD / entities** - any DOCTYPE is rejected outright, which also
  forbids all entity declarations.  Undefined entity references are
  malformed-XML errors raised by expat itself.

Record-size measurement: at an end-element event expat's
``CurrentByteIndex`` points at the ``<`` of the end tag (or just past
``/>`` for self-closing tags).  A small rolling tail window of recently
fed bytes lets us locate the end tag's ``>`` and thus the exact byte
span of the record.  (A self-closing record that is the last child of
its parent can be overestimated by a few bytes - harmless for a
resource guard.)
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from xml.parsers import expat

from . import DEFAULT_MAX_DEPTH, DEFAULT_MAX_RECORD_BYTES, NS
from .errors import DocumentError
from .staging import Staging

E_EXPORT = NS + " export"
E_BATCH = NS + " batch"
E_CUSTOMER = NS + " customer"
E_ORDER = NS + " order"
E_LINE = NS + " line"

_RE_UINT = re.compile(r"[0-9]+")


@dataclass
class Limits:
    max_depth: int = DEFAULT_MAX_DEPTH
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES


@dataclass
class Stats:
    batches: int = 0
    customers_seen: int = 0
    orders_seen: int = 0
    extension_subtrees: int = 0        # foreign-namespace subtrees ignored
    unknown_business_subtrees: int = 0  # unknown tags inside the business ns
    stray_lines: int = 0               # <line> outside any <order>


class TailWindow:
    """Rolling window over the most recently fed bytes.

    End tags we need to inspect always sit within the last chunk plus a
    few bytes, so the window only needs to be ``chunk_size + slack``.
    """

    __slots__ = ("keep", "buf", "start")

    def __init__(self, keep: int):
        self.keep = keep
        self.buf = bytearray()
        self.start = 0  # absolute stream offset of buf[0]

    def feed(self, data: bytes) -> None:
        self.buf += data
        if len(self.buf) > self.keep:
            drop = len(self.buf) - self.keep
            del self.buf[:drop]
            self.start += drop

    def startswith(self, prefix: bytes, abs_pos: int) -> bool:
        i = abs_pos - self.start
        return 0 <= i and bytes(self.buf[i : i + len(prefix)]) == prefix

    def find(self, needle: bytes, abs_pos: int) -> int:
        i = abs_pos - self.start
        if i < 0:
            return -1
        j = self.buf.find(needle, i)
        return -1 if j < 0 else self.start + j


def _clean(value):
    """Strip an attribute; empty/blank becomes None."""
    if value is None:
        return None
    s = value.strip()
    return s if s else None


def _parse_uint(raw, minimum):
    """Strict non-negative integer: decimal digits only, no sign/space."""
    if raw is None or not _RE_UINT.fullmatch(raw):
        return None
    try:
        value = int(raw)
    except ValueError:  # absurdly long digit strings (int max str digits)
        return None
    return value if value >= minimum else None


class StreamParser:
    def __init__(self, staging: Staging, limits: Limits, chunk_size: int):
        self.stg = staging
        self.limits = limits
        self.stats = Stats()
        self.depth = 0
        self.ctx: list[str] = []      # element contexts: export/batch/customer/order/line
        self.ignoring = 0             # >0 while inside an ignored subtree
        self.batch_stack: list[str] = []
        self.seq = 0                  # document occurrence counter (customer+order)
        self.cur_customer: dict | None = None
        self.cur_order: dict | None = None
        self.tail = TailWindow(chunk_size + 8192)

        p = expat.ParserCreate(namespace_separator=" ")
        p.StartElementHandler = self._start
        p.EndElementHandler = self._end
        p.StartDoctypeDeclHandler = self._reject_doctype
        p.EntityDeclHandler = self._reject_entity
        self.p = p

    # -- forbidden constructs ---------------------------------------------
    @staticmethod
    def _reject_doctype(*_args):
        raise DocumentError("DTD/doctype declarations are forbidden")

    @staticmethod
    def _reject_entity(*_args):
        raise DocumentError("entity declarations are forbidden")

    # -- element dispatch ----------------------------------------------------
    def _start(self, name, attrs):
        self.depth += 1
        if self.depth > self.limits.max_depth:
            raise DocumentError(
                f"element nesting depth exceeds {self.limits.max_depth}"
            )
        if self.ignoring:
            self.ignoring += 1
            return
        if self.depth == 1:
            if name != E_EXPORT:
                raise DocumentError(
                    "root element must be <export> in namespace " + NS
                )
            self.ctx.append("export")
            return

        parent = self.ctx[-1]
        if parent == "export":
            if name == E_BATCH:
                self._start_batch(attrs)
                self.ctx.append("batch")
            elif name in (E_CUSTOMER, E_ORDER):
                raise DocumentError(
                    f"record element {name.split(' ')[1]!r} outside any batch"
                )
            else:
                self._ignore(name)
        elif parent == "batch":
            if name == E_BATCH:
                self._start_batch(attrs)
                self.ctx.append("batch")
            elif name == E_CUSTOMER:
                self._start_customer(attrs)
                self.ctx.append("customer")
            elif name == E_ORDER:
                self._start_order(attrs)
                self.ctx.append("order")
            elif name == E_LINE:
                self.stats.stray_lines += 1
                self._ignore(name)
            else:
                self._ignore(name)
        elif parent == "order":
            if name == E_LINE:
                self._add_line(attrs)
                self.ctx.append("line")
            elif name == E_ORDER:
                self._flag_order("nested_order")
                self._ignore(name)
            elif name == E_BATCH:
                self._flag_order("nested_batch")
                self._ignore(name)
            else:
                self._ignore(name)
        else:  # customer / line are leaf records: ignore any children
            self._ignore(name)

    def _end(self, name):
        try:
            if self.ignoring:
                self.ignoring -= 1
                return
            ctx = self.ctx.pop()
            if ctx == "batch":
                self.batch_stack.pop()
            elif ctx == "order":
                self._end_order()
            elif ctx == "customer":
                self._end_customer()
        finally:
            self.depth -= 1

    def _ignore(self, name):
        self.ignoring = 1
        if name.startswith(NS + " "):
            self.stats.unknown_business_subtrees += 1
        else:
            self.stats.extension_subtrees += 1

    # -- batches --------------------------------------------------------------
    def _start_batch(self, attrs):
        bid = _clean(attrs.get("id"))
        if bid is None:
            raise DocumentError("batch with missing or empty id")
        self.stg.insert_batch(bid)  # duplicate -> DocumentError
        self.batch_stack.append(bid)
        self.stats.batches += 1

    # -- customers --------------------------------------------------------------
    def _start_customer(self, attrs):
        self.seq += 1
        self.stats.customers_seen += 1
        self.cur_customer = {
            "batch_id": self.batch_stack[-1],
            "cid": _clean(attrs.get("id")),
            "name": _clean(attrs.get("name")),
            "seq": self.seq,
            "start": self.p.CurrentByteIndex,
        }

    def _end_customer(self):
        c = self.cur_customer
        self.cur_customer = None
        reason = None
        if self._record_size(c["start"]) > self.limits.max_record_bytes:
            reason = "subtree_too_large"
        elif c["cid"] is None:
            reason = "missing_id"
        elif c["name"] is None:
            reason = "missing_name"
        valid = reason is None
        # A present id claims its identity even when the record is
        # rejected, so duplicates still fail the whole document.
        self.stg.add_customer(c["batch_id"], c["cid"], c["name"], valid, c["seq"])
        if not valid:
            self.stg.add_reject(
                "customer", c["batch_id"], c["cid"], c["seq"], reason
            )

    # -- orders --------------------------------------------------------------
    def _start_order(self, attrs):
        self.seq += 1
        self.stats.orders_seen += 1
        self.cur_order = {
            "batch_id": self.batch_stack[-1],
            "oid": _clean(attrs.get("id")),
            "cust": _clean(attrs.get("customer_id")),
            "seq": self.seq,
            "start": self.p.CurrentByteIndex,
            "lines": [],
            "nlines": 0,
            "reason": None,   # structural problem found while parsing
            "detail": None,
            "overflow": False,
        }

    def _flag_order(self, reason):
        o = self.cur_order
        if o["reason"] is None:
            o["reason"] = reason
        o["lines"] = []

    def _add_line(self, attrs):
        o = self.cur_order
        o["nlines"] += 1
        if o["reason"] is not None or o["overflow"]:
            return
        if self.p.CurrentByteIndex - o["start"] > self.limits.max_record_bytes:
            o["overflow"] = True
            o["lines"] = []
            return
        sku = attrs.get("sku")
        qty = _parse_uint(attrs.get("qty"), minimum=1)
        unit = _parse_uint(attrs.get("unit_cents"), minimum=0)
        bad = []
        if sku is None or not sku.strip():
            bad.append("sku")
        if qty is None:
            bad.append("qty")
        if unit is None:
            bad.append("unit_cents")
        if bad:
            o["reason"] = "bad_line"
            o["detail"] = f"line {o['nlines']}: invalid {', '.join(bad)}"
            o["lines"] = []  # whole order is quarantined; keep nothing
            return
        o["lines"].append((sku, qty, unit))

    def _end_order(self):
        o = self.cur_order
        self.cur_order = None
        if o["oid"] is None:
            reason = "missing_id"
        elif o["cust"] is None:
            reason = "missing_customer_id"
        elif o["overflow"] or self._record_size(o["start"]) > self.limits.max_record_bytes:
            reason = "subtree_too_large"
        elif o["reason"] is not None:
            reason = o["reason"]
        elif o["nlines"] == 0:
            reason = "no_lines"
        else:
            reason = None
        valid = reason is None
        key = self.stg.add_order(
            o["batch_id"], o["oid"], o["cust"], valid, o["seq"], reason
        )
        if valid:
            self.stg.add_lines(key, o["lines"])
        else:
            self.stg.add_reject(
                "order", o["batch_id"], o["oid"], o["seq"], reason, o["detail"]
            )

    # -- helpers --------------------------------------------------------------
    def _record_size(self, start: int) -> int:
        idx = self.p.CurrentByteIndex
        if self.tail.startswith(b"</", idx):
            gt = self.tail.find(b">", idx)
            return (gt + 1) - start if gt >= 0 else idx + 256 - start
        return idx - start  # self-closing tag: index sits just past "/>"


def parse_file(path, staging: Staging, limits: Limits, chunk_size: int):
    """Stream-parse ``path`` into ``staging``.

    Returns ``(stats, sha256_hex, byte_size)``.  Raises
    :class:`DocumentError` on any document-level failure; whatever was
    staged so far is simply discarded by the caller.
    """
    sp = StreamParser(staging, limits, chunk_size)
    sha = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    sp.p.Parse(b"", True)
                    break
                sha.update(chunk)
                size += len(chunk)
                sp.tail.feed(chunk)
                sp.p.Parse(chunk, False)
    except DocumentError:
        raise
    except expat.ExpatError as exc:
        raise DocumentError(
            f"malformed XML (line {exc.lineno}, column {exc.offset}): {exc}"
        ) from None
    except UnicodeDecodeError as exc:  # defensive; expat normally catches these
        raise DocumentError(f"encoding error: {exc}") from None
    staging.flush()
    return sp.stats, sha.hexdigest(), size
