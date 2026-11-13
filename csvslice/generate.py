"""Sample CSV generator used only for local tests and performance checks."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass


@dataclass
class GenerateSpec:
    rows: int = 20
    seed: int = 1234
    chunk_bytes: int | None = None
    target_size: int | None = None
    terminate_last: bool = True
    include_bom: bool = False
    multiline_every: int = 0
    quoted_comma_every: int = 0
    quoted_quote_every: int = 0


@dataclass
class GenerateResult:
    bytes_written: int
    data_records: int
    has_bom: bool
    seed: int


_HEADER = b"id,name,note,city,amount\n"

_CITIES = (
    "北京",
    "上海",
    "广州",
    "深圳",
    "杭州",
    "成都",
    "南京",
    "武汉",
)


def _quote_for_field(text: str) -> bytes:
    return b'"' + text.replace('"', '""').encode("utf-8") + b'"'


def _plain_field(text: str) -> bytes:
    if any(ch in text for ch in ',"\r\n'):
        return _quote_for_field(text)
    return text.encode("utf-8")


def _make_record(record_no: int, rng: random.Random, spec: GenerateSpec, width: int) -> bytes:
    city = _CITIES[record_no % len(_CITIES)]
    name = f"用户{record_no:08d}"
    amount = f"{(record_no % 9999) / 10:.2f}"
    note_parts: list[str] = [f"第{record_no}条复核材料"]

    # Pad the note so large files get moderately sized, variable records.
    if width > 0:
        filler_len = rng.randint(width // 2, width)
        note_parts.append("数据" * (filler_len // 6 + 1))

    note = "".join(note_parts)
    if spec.multiline_every and (record_no + 1) % spec.multiline_every == 0:
        note = note + "\n第二行：字段内换行\r\n第三行：中文仍属本字段"
    if spec.quoted_comma_every and (record_no + 1) % spec.quoted_comma_every == 0:
        note = note + ",含逗号"
    if spec.quoted_quote_every and (record_no + 1) % spec.quoted_quote_every == 0:
        note = note + ' 他说"复核无误"'

    fields = [str(record_no), name, note, city, amount]
    encoded = [_quote_for_field(f) if idx == 2 else _plain_field(f) for idx, f in enumerate(fields)]
    return b",".join(encoded) + b"\n"


def generate_csv(path: str, spec: GenerateSpec) -> GenerateResult:
    rng = random.Random(spec.seed)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data_records = 0
    written = 0
    with open(path, "wb") as f:
        if spec.include_bom:
            f.write(b"\xef\xbb\xbf")
            written += 3
        f.write(_HEADER)
        written += len(_HEADER)

        if spec.target_size is not None:
            width = spec.chunk_bytes or 4096
            while written < spec.target_size:
                record = _make_record(data_records, rng, spec, width)
                f.write(record)
                written += len(record)
                data_records += 1
        else:
            for record_no in range(spec.rows):
                record = _make_record(record_no, rng, spec, 0)
                if not spec.terminate_last and record_no == spec.rows - 1:
                    record = record[:-1]
                f.write(record)
                written += len(record)
                data_records += 1
    return GenerateResult(
        bytes_written=written,
        data_records=data_records,
        has_bom=spec.include_bom,
        seed=spec.seed,
    )
