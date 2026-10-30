import os
import json
import sqlite3
import re

from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from groq import Groq


# --------------------------------------------------
# Configuration
# --------------------------------------------------

load_dotenv()

app = Flask(__name__)

DATABASE_PATH = "database.db"

groq_api_key = os.getenv("GROQ_API_KEY")

if not groq_api_key:
    raise RuntimeError(
        "GROQ_API_KEY is missing. Add it to your .env file."
    )

client = Groq(api_key=groq_api_key)

MODEL = "openai/gpt-oss-120b"


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
    """
    Remove markdown code fences and surrounding whitespace.
    """

    sql = sql.strip()

    sql = re.sub(r"^```sql\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"^```\s*", "", sql)
    sql = re.sub(r"\s*```$", "", sql)

    return sql.strip()


def is_safe_sql(sql):
    """
    Only allow SELECT queries.

    This prevents the AI from modifying the database.
    """

    sql = clean_sql(sql)

    if not sql:
        return False

    normalized = sql.lower().strip()

    # Must begin with SELECT
    if not normalized.startswith("select"):
        return False

    # Block potentially dangerous SQL operations
    forbidden_keywords = [
        "insert ",
        "update ",
        "delete ",
        "drop ",
        "alter ",
        "create ",
        "replace ",
        "attach ",
        "detach ",
        "pragma ",
        "vacuum ",
        "reindex ",
    ]

    for keyword in forbidden_keywords:
        if keyword in normalized:
            return False

    # Prevent multiple SQL statements
    if ";" in normalized.rstrip(";"):
        return False

    return True


# --------------------------------------------------
# Generate SQL
# --------------------------------------------------

def generate_sql(question):
    """
    Ask Groq to convert the user's natural-language
    business question into SQLite SQL.
    """

    system_prompt = f"""
You are the SQL analyst for Cocoa & Co., a handmade chocolate company.

Your job is to convert business questions into SQLite SQL queries.

DATABASE SCHEMA:
{DATABASE_SCHEMA}

Rules:

1. Return ONLY SQL.
2. The SQL must be a SELECT query.
3. Never use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE,
   REPLACE, ATTACH, DETACH, PRAGMA, or VACUUM.
4. Do not modify the database.
5. Use SQLite-compatible SQL.
6. Use JOINs when necessary.
7. Use clear column aliases for calculated values.
8. If calculating revenue, use:
   quantity * unit_price
9. For product revenue, join orders -> order_items -> products.
10. For ingredient cost, use product_ingredients and ingredients.
11. Do not invent tables or columns.
12. Return only the SQL query with no explanation.
"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": question,
            },
        ],
        temperature=0,
        max_tokens=1000,
    )

    sql = response.choices[0].message.content

    return clean_sql(sql)


# --------------------------------------------------
# Execute SQL
# --------------------------------------------------

def execute_sql(sql):
    """
    Execute a safe SELECT query and return the result.
    """

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
    """
    Ask Groq to explain the SQL result in plain business language.
    """

    result_data = {
        "columns": columns,
        "rows": rows,
    }

    prompt = f"""
You are a business analyst for Cocoa & Co., a handmade chocolate company.

The user asked:

{question}

The SQL query used was:

{sql}

The query returned:

{json.dumps(result_data, default=str)}

Explain the result in concise, easy-to-understand business language.

Rules:

- Focus on the actual result.
- Mention important numbers.
- Give useful business insight when appropriate.
- Do not invent information.
- Do not repeat the SQL query.
- Keep the explanation to 2-4 sentences.
"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": "You are a concise business analyst.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        temperature=0.2,
        max_tokens=400,
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
        data = request.get_json()

        if not data:
            return jsonify({
                "error": "No request data provided."
            }), 400

        question = data.get("question", "").strip()

        if not question:
            return jsonify({
                "error": "Please enter a question."
            }), 400

        # 1. Generate SQL
        sql = generate_sql(question)

        # 2. Safety check
        if not is_safe_sql(sql):
            return jsonify({
                "error": "The generated SQL query was rejected for safety.",
                "sql": sql,
            }), 400

        # 3. Execute SQL
        columns, rows = execute_sql(sql)

        # 4. Explain result
        explanation = explain_result(
            question,
            sql,
            columns,
            rows,
        )

        # 5. Return everything to frontend
        return jsonify({
            "question": question,
            "sql": sql,
            "columns": columns,
            "rows": rows,
            "explanation": explanation,
        })

    except sqlite3.Error as error:
        return jsonify({
            "error": f"Database error: {str(error)}"
        }), 500

    except Exception as error:
        return jsonify({
            "error": str(error)
        }), 500


# --------------------------------------------------
# Run App
# --------------------------------------------------

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True,
    )
