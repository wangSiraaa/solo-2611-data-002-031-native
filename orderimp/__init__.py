"""orderimp - streaming importer for nested-batch order XML exports.

Business namespace is fixed to ``urn:orders:v1``.  The package provides:

* :mod:`orderimp.streamparse` - event-driven (expat) parser with limits
* :mod:`orderimp.staging`     - SQLite staging area (scratch, on disk)
* :mod:`orderimp.publish`     - reference resolution + atomic version publish
* :mod:`orderimp.generate`    - synthetic sample file generator
* :mod:`orderimp.verify`      - self-check suite
* :mod:`orderimp.cli`         - ``generate`` / ``import`` / ``inspect`` / ``verify``
"""

NS = "urn:orders:v1"
VERSION = "1.0.0"

# Hard limits required by the spec.
DEFAULT_MAX_DEPTH = 32
DEFAULT_MAX_RECORD_BYTES = 64 * 1024  # 64 KiB per customer/order subtree
