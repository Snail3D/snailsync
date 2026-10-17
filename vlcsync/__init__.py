"""SnailSync -- watch the same movie in VLC together, on your own computers."""

from .sync import Timeline, Sample, Corrections          # noqa: F401
from .vlc import VLCClient, VLCError, to_uri, launch     # noqa: F401

__version__ = "0.1.0"
__all__ = ["Timeline", "Sample", "Corrections", "VLCClient", "VLCError",
           "to_uri", "launch"]