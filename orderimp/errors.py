"""Error types used across the importer."""


class DocumentError(Exception):
    """Document-level failure.

    Raised for malformed XML (truncation, bad encoding, unclosed tags),
    DTD / entity declarations, wrong root, depth overflow and duplicate
    identities (batch ids, or customer/order ids inside one batch).
    Nothing may be published when this is raised; the previously
    published version stays readable.
    """
