"""Error types used across the migration tool."""


class MoverError(Exception):
    """Base class for all tool errors."""


class ConfigError(MoverError):
    """The target configuration file is invalid."""


class CatalogError(MoverError):
    """The catalog does not have the shape this tool requires."""


class PlanChanged(MoverError):
    """Re-validation under the migration lock found the plan no longer holds.

    Raised inside the migration transaction; raising it aborts the
    transaction, so every change of the attempt is rolled back.
    """


class AlreadyDoneInTxn(MoverError):
    """Re-validation under the lock found the month already migrated."""


class InTxnVerificationFailed(MoverError):
    """Post-move invariants did not hold inside the migration transaction."""
