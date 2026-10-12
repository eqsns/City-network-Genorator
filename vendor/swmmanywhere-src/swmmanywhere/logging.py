"""Logging module for SWMManywhere.

Example:
```python
>>> import os
>>> os.environ["SWMMANYWHERE_VERBOSE"] = "true"
>>> # logging is now enabled in any swmmanywhere module
>>> from swmmanywhere.logging import logger # You can now log yourself
>>> logger.add("file.log") # Add a log file
>>> logger.info("This is an info message.") # Write to stdout and file.log
This is an info message.
>>> os.environ["SWMMANYWHERE_VERBOSE"] = "false" # Disable logging
```
"""

from __future__ import annotations

import os
import sys

import loguru


def verbose() -> bool:
    """Get the verbosity."""
    return os.getenv("SWMMANYWHERE_VERBOSE", "false").lower() == "true"


def set_verbose(verbose: bool):
    """Set the verbosity."""
    os.environ["SWMMANYWHERE_VERBOSE"] = str(verbose).lower()


def dynamic_filter(record):
    """A dynamic filter."""
    return verbose()


def get_logger() -> loguru.logger:
    """Get a logger."""
    # Frozen Windows workers can expose redirected streams in the system code
    # page despite PYTHONUTF8. The desktop log viewer always decodes UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    logger = loguru.logger
    logger.configure(
        handlers=[
            {
                "sink": sys.stdout,
                "filter": dynamic_filter,
                "colorize": bool(sys.stdout and sys.stdout.isatty()),
                "format": " | ".join(
                    [
                        "<cyan>{time:YYYY/MM/DD HH:mm:ss}</>",
                        "{message}",
                    ]
                ),
            }
        ]
    )
    return logger


# Get the logger
logger = get_logger()

# Add a test_logger method to the logger
logger.test_logger = lambda: logger.info("This is a test message.")

# Store the original add method
original_add = logger.add


# Define a new function that wraps the original add method
def new_add(sink, **kwargs):
    """A new add method to wrap existing one but with the filter."""
    # Include the dynamic filter in the kwargs if not already specified
    if "filter" not in kwargs:
        kwargs["filter"] = dynamic_filter
    # Call the original add method with the updated kwargs
    return original_add(sink, **kwargs)


# Replace the logger's add method with new_add
logger.add = new_add
