"""WarmStart product namespace.

The 0.x implementation remains in ``coldstart`` so existing imports keep
working. New integrations should prefer the ``warmstart`` command. The public
Python API is intentionally small while the package boundary is stabilized.
"""

from coldstart import __version__

__all__ = ["__version__"]
