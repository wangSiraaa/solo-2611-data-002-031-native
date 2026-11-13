"""PTY-only legacy terminal entry assistant.

The assistant observes an 80x24 terminal screen rebuilt from ANSI output and
sends keystrokes through a real PTY.  It never imports the legacy application,
never opens its SQLite database and never adds hidden interfaces.
"""

__version__ = "1.0.0"
