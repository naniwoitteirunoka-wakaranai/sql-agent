import sqlite3
from pathlib import Path


# --------------------------------------------------
# Database Configuration
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

DATABASE_PATH = BASE_DIR / "database.db"


# --------------------------------------------------
# Get Database Connection
# --------------------------------------------------

def get_db():
    """
    Create and return a SQLite database connection.
    """

    connection = sqlite3.connect(DATABASE_PATH)

    # Allows rows to behave like dictionaries
    connection.row_factory = sqlite3.Row

    return connection


# --------------------------------------------------
# Close Database Connection
# --------------------------------------------------

def close_db(connection):
    """
    Safely close a database connection.
    """

    if connection:
        connection.close()
