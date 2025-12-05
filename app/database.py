# app/database.py
import os
import pyodbc
from contextlib import contextmanager
from typing import Literal
from dotenv import load_dotenv

load_dotenv()

# Shared connection settings
BASE_CONFIG = {
    "driver": os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server"),
    "server": os.getenv("DB_SERVER", "208.64.33.61"),
    "uid": os.getenv("DB_USER", "Masinde"),
    "pwd": os.getenv("DB_PASSWORD", "qwerty@123#"),
}

# Define the two databases you use
DATABASES = {
    "Ridgeways": "Ridgeways",
    "SyfeParking": "SyfeParking",   # This is where your function lives
    # Add more if needed later
}

def build_connection_string(database_name: str) -> str:
    return (
        f"DRIVER={{{BASE_CONFIG['driver']}}};"
        f"SERVER={BASE_CONFIG['server']};"
        f"DATABASE={database_name};"
        f"UID={BASE_CONFIG['uid']};"
        f"PWD={BASE_CONFIG['pwd']};"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
        "Connection Timeout=30;"
    )

@contextmanager
def get_cursor(
    db: Literal["Ridgeways", "SyfeParking"] = "Ridgeways"
):
    """
    Usage:
        with get_cursor("Ridgeways") as cursor:     # default
            cursor.execute("SELECT ... FROM transactions")

        with get_cursor("SyfeParking") as cursor:
            cursor.execute("SELECT dbo.[Transactions.CheckParkingFeeDue](?)", "KCA123X")
    """
    db_name = DATABASES[db]
    conn_str = build_connection_string(db_name)
    conn = None
    try:
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()
        yield cursor
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        raise e
    finally:
        if conn:
            conn.close()