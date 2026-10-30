
import os
import json
import sqlite3
import re
from pathlib import Path

import zstandard as zstd
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from groq import Groq


# --------------------------------------------------
# Configuration
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)

# Use a separate database for compressed database testing
DATABASE_PATH = BASE_DIR / "database.db"
COMPRESSED_DB_PATH = BASE_DIR / "database.db.zst"

groq_api_key = os.getenv("GROQ_API_KEY")

if not groq_api_key:
    raise RuntimeError(
        "GROQ_API_KEY is missing. Add it to your .env file."
    )

client = Groq(api_key=groq_api_key)

MODEL = "openai/gpt-oss-120b"


# --------------------------------------------------
# Restore Compressed Database
# --------------------------------------------------

def ensure_database():
    """
    Restore the compressed database if the test database
    does not already exist.
    """

    if DATABASE_PATH.exists():
        print("Test database already exists.")
        return

    if not COMPRESSED_DB_PATH.exists():
        raise FileNotFoundError(
            f"Compressed database not found: {COMPRESSED_DB_PATH}"
        )

    print("Restoring database from compressed file...")

    temp_path = DATABASE_PATH.with_suffix(".tmp")

    try:
        with open(COMPRESSED_DB_PATH, "rb") as source:
            with open(temp_path, "wb") as output:
                zstd.ZstdDecompressor().copy_stream(source, output)

        # Verify that the restored file is a valid SQLite database
        connection = sqlite3.connect(temp_path)

        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()

            if not result or result[0] != "ok":
                raise sqlite3.DatabaseError(
                    "Restored database failed integrity check."
                )
        finally:
            connection.close()

        temp_path.replace(DATABASE_PATH)

        print("Database restored and verified successfully!")

    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise


# Restore before starting the application
ensure_database()


# --------------------------------------------------
# Database
# --------------------------------------------------

def get_db():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


# --------------------------------------------------
# Database Schema for the AI
# --------------------------------------------------

DATABASE_SCHEMA = """
Database: Cocoa & Co. handmade chocolate business

TABLE customers
- id INTEGER PRIMARY KEY
- name TEXT
- city TEXT
- email TEXT
- signup_date TEXT

TABLE products
- id INTEGER PRIMARY KEY
- name TEXT
- category TEXT
- description TEXT
- price_eur REAL
- cost_eur REAL

TABLE orders
- id INTEGER PRIMARY KEY
- customer_id INTEGER
- order_date TEXT
- payment_method TEXT
- status TEXT

TABLE order_items
- id INTEGER PRIMARY KEY
- order_id INTEGER
- product_id INTEGER
- quantity INTEGER
- unit_price REAL

TABLE employees
- id INTEGER PRIMARY KEY
- name TEXT
- role TEXT
- department TEXT
- hire_date TEXT
- salary REAL
- city TEXT

TABLE ingredients
- id INTEGER PRIMARY KEY
- name TEXT
- category TEXT
- unit TEXT
- cost_per_unit_eur REAL
- supplier_name TEXT

TABLE product_ingredients
- product_id INTEGER
- ingredient_id INTEGER
- quantity_required REAL

Relationships:

customers.id -> orders.customer_id
orders.id -> order_items.order_id
products.id -> order_items.product_id
products.id -> product_ingredients.product_id
ingredients.id -> product_ingredients.ingredient_id
"""


# --------------------------------------------------
# SQL Safety
# --------------------------------------------------

def clean_sql(sql):
    sql = sql.strip()

    sql = re.sub(
        r"^```sql\s*", "", sql, flags=re.IGNORECASE
    )

    sql = re.sub(r"^```\s*", "", sql)
    sql = re.sub(r"\s*```$", "", sql)

    return sql.strip()


def is_safe_sql(sql):
    """
    Allow only a single read-only SELECT statement.
    """

    sql = clean_sql(sql)

    if not sql:
        return False

    normalized = sql.lower().strip()

    if not normalized.startswith("select"):
        return False

    forbidden_keywords = [
        "insert", "update", "delete", "drop",
        "alter", "create", "replace", "attach",
        "detach", "pragma", "vacuum", "reindex"
    ]

    if ";" in normalized.rstrip(";"):
        return False

    for keyword in forbidden_keywords:
        if re.search(r"\b" + keyword + r"\b", normalized):
            return False

    return True


# --------------------------------------------------
# Generate SQL
# --------------------------------------------------

def generate_sql(question):

    system_prompt = f"""
You are the SQL analyst for Cocoa & Co.,
a handmade chocolate company.

Convert business questions into SQLite SQL queries.

DATABASE SCHEMA:
{DATABASE_SCHEMA}

Rules:

1. Return ONLY SQL.
2. Generate only SELECT queries.
3. Never modify the database.
4. Use SQLite-compatible SQL.
5. Use JOINs when necessary.
6. Use clear column aliases for calculations.
7. Calculate revenue using quantity * unit_price.
8. For product revenue, join orders, order_items and products.
9. For ingredient cost, use product_ingredients and ingredients.
10. Do not invent tables or columns.
11. Return only SQL without explanations.
"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": question
            }
        ],
        temperature=0,
        max_tokens=1000
    )

    sql = response.choices[0].message.content

    return clean_sql(sql)


# --------------------------------------------------
# Execute SQL
# --------------------------------------------------

def execute_sql(sql):

    if not is_safe_sql(sql):
        raise ValueError("Unsafe SQL query rejected.")

    connection = get_db()

    try:
        cursor = connection.execute(sql)

        rows = cursor.fetchmany(200)

        columns = [
            description[0]
            for description in cursor.description
        ]

        data = [dict(row) for row in rows]

        return columns, data

    finally:
        connection.close()


# --------------------------------------------------
# Explain Results
# --------------------------------------------------

def explain_result(question, sql, columns, rows):

    result_data = {
        "columns": columns,
        "rows": rows
    }

    prompt = f"""
You are a business analyst for Cocoa & Co.

The user asked:
{question}

The SQL query used was:
{sql}

The query returned:
{json.dumps(result_data, default=str)}

Explain the result in concise business language.

Rules:
- Focus on actual results.
- Mention important numbers.
- Give useful business insights.
- Do not invent information.
- Do not repeat the SQL query.
- Keep the explanation to 2-4 sentences.
"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": "You are a concise business analyst."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.2,
        max_tokens=400
    )

    return response.choices[0].message.content.strip()


# --------------------------------------------------
# Routes
# --------------------------------------------------

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/ask", methods=["POST"])
def ask_question():

    try:
        data = request.get_json(silent=True)

        if not data:
            return jsonify({
                "error": "No request data provided."
            }), 400

        question = data.get("question", "").strip()

        if not question:
            return jsonify({
                "error": "Please enter a question."
            }), 400

        # Generate SQL
        sql = generate_sql(question)

        # Validate SQL
        if not is_safe_sql(sql):
            return jsonify({
                "error": "Generated SQL query was rejected for safety.",
                "sql": sql
            }), 400

        # Execute SQL
        columns, rows = execute_sql(sql)

        # Explain results
        explanation = explain_result(
            question,
            sql,
            columns,
            rows
        )

        return jsonify({
            "question": question,
            "sql": sql,
            "columns": columns,
            "rows": rows,
            "explanation": explanation
        })

    except sqlite3.Error as error:
        return jsonify({
            "error": f"Database error: {str(error)}"
        }), 500

    except Exception as error:
        app.logger.exception("Question processing failed")

        return jsonify({
            "error": str(error)
        }), 500


# --------------------------------------------------
# Run Application
# --------------------------------------------------

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )
