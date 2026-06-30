from __future__ import annotations

import sqlite3


STORAGE_READ_ERRORS = (OSError, sqlite3.Error, ValueError)
