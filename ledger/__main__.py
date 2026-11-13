"""Module entry point: python -m ledger ... -> ledger.main()"""
from .ledger import run

if __name__ == "__main__":
    raise SystemExit(run())
