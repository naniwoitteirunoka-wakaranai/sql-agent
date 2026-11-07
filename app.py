
import os
import json
import sqlite3
import re
from pathlib import Path

import zstandard as zstd
import sqlglot
from sqlglot import exp

from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from groq import Groq


# --------------------------------------------------
# Configuration
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)

DATABASE_PATH = BASE_DIR / "database.db"
COMPRESSED_DB_PATH = BASE_DIR / "database.db.zst"

groq_api_key = os.getenv("GROQ_API_KEY")

if not groq_api_key:
    raise RuntimeError(
        "GROQ_API_KEY is missing. Add it to your .env file."
    )

client = Groq(api_key=groq_api_key)

MODEL = "openai/gpt-oss-120b"

MAX_SQL_ROWS = 200


# --------------------------------------------------
# Restore Compressed Database
# --------------------------------------------------

def ensure_database():

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

                zstd.ZstdDecompressor().copy_stream(
                    source,
                    output
                )

        connection = sqlite3.connect(temp_path)

        try:

            result = connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()

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


ensure_database()


# --------------------------------------------------
# Read-Only Database Connection
# --------------------------------------------------

def get_db():

    database_uri = DATABASE_PATH.as_uri() + "?mode=ro"

    connection = sqlite3.connect(
        database_uri,
        uri=True,
        timeout=30
    )

    connection.row_factory = sqlite3.Row

    # Additional SQLite-level read-only protection
    connection.execute("PRAGMA query_only = ON")

    return connection


# --------------------------------------------------
# Database Schema for the AI
# --------------------------------------------------

DATABASE_SCHEMA = """
Database: Veloura Chocolates handmade chocolate business

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
# Clean Generated SQL
# --------------------------------------------------

def clean_sql(sql):

    if not isinstance(sql, str):
        return ""

    sql = sql.strip()

    # Remove Markdown SQL code fences
    sql = re.sub(
        r"^```(?:sql)?\s*",
        "",
        sql,
        flags=re.IGNORECASE
    )

    sql = re.sub(
        r"\s*```$",
        "",
        sql
    )

    return sql.strip()


# --------------------------------------------------
# SQL Validator
# --------------------------------------------------

def validate_sql(sql):
    """
    Validate generated SQL before execution.

    Checks:
    1. SQL is not empty.
    2. SQL contains exactly one statement.
    3. SQL syntax is valid.
    4. Root statement is a SELECT query.
    5. SQL does not contain write operations.
    6. Tables and columns exist in SQLite.

    Returns:
        (True, "SQL validation successful")
        (False, "Reason for rejection")
    """

    sql = clean_sql(sql)

    if not sql:
        return False, "Generated SQL is empty."

    # ----------------------------------------------
    # 1. Parse SQL using SQLGlot
    # ----------------------------------------------

    try:

        statements = sqlglot.parse(
            sql,
            read="sqlite"
        )

        statements = [
            statement
            for statement in statements
            if statement is not None
        ]

    except sqlglot.errors.ParseError as error:

        return False, f"SQL syntax error: {error}"

    # ----------------------------------------------
    # 2. Reject multiple SQL statements
    # ----------------------------------------------

    if len(statements) != 1:

        return False, (
            "Multiple SQL statements are not allowed."
        )

    parsed_sql = statements[0]

    # ----------------------------------------------
    # 3. Only allow SELECT-style queries
    # ----------------------------------------------

    if not isinstance(parsed_sql, exp.Query):

        return False, (
            "Only SELECT queries are permitted."
        )

    # ----------------------------------------------
    # 4. Reject dangerous SQL operations
    # ----------------------------------------------

    forbidden_nodes = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        exp.Alter,
        exp.Command,
    )

    for node in parsed_sql.walk():

        if isinstance(node, forbidden_nodes):

            return False, (
                f"Forbidden SQL operation detected: "
                f"{type(node).__name__}"
            )

    # ----------------------------------------------
    # 5. Validate against actual SQLite schema
    # ----------------------------------------------

    connection = None

    try:

        connection = get_db()

        # EXPLAIN QUERY PLAN checks whether SQLite
        # can prepare the query without executing it.

        connection.execute(
            "EXPLAIN QUERY PLAN " + sql
        )

    except sqlite3.Error as error:

        return False, (
            f"Database validation failed: {str(error)}"
        )

    finally:

        if connection:
            connection.close()

    return True, "SQL validation successful."


# --------------------------------------------------
# SQLite Authorizer
# --------------------------------------------------

def sqlite_authorizer(action, arg1, arg2, database, trigger):
    """
    SQLite-level security boundary.

    Denies database modifications even if a query
    somehow passes the earlier validation layer.
    """

    denied_actions = {

        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,

        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
        sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,

        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER,
        sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,

        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_SAVEPOINT,
        sqlite3.SQLITE_PRAGMA,
    }

    if action in denied_actions:
        return sqlite3.SQLITE_DENY

    # Block potentially dangerous extension/file functions.
    if action == sqlite3.SQLITE_FUNCTION:

        function_name = (
            arg2 or arg1 or ""
        ).lower()

        if function_name in {
            "load_extension",
            "writefile",
        }:
            return sqlite3.SQLITE_DENY

    return sqlite3.SQLITE_OK


# --------------------------------------------------
# Generate SQL
# --------------------------------------------------

def generate_sql(question, previous_sql=None, error_message=None):

    correction_context = ""

    if previous_sql and error_message:

        correction_context = f"""
The previous SQL query failed validation.

Previous SQL:
{previous_sql}

Validation error:
{error_message}

Generate a corrected query that fixes the error.
Do not repeat the same mistake.
"""

    system_prompt = f"""
You are the SQL analyst for Veloura Chocolates,
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
12. Do not generate multiple SQL statements.

{correction_context}
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

    # Validate before execution
    is_valid, validation_message = validate_sql(sql)

    if not is_valid:

        raise ValueError(validation_message)

    connection = get_db()

    try:

        # Install SQLite authorizer
        connection.set_authorizer(sqlite_authorizer)

        cursor = connection.execute(sql)

        rows = cursor.fetchmany(MAX_SQL_ROWS)

        columns = [
            description[0]
            for description in cursor.description
        ]

        data = [
            dict(row)
            for row in rows
        ]

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
You are a business analyst for Veloura Chocolates.

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
# Generate, Validate and Execute
# --------------------------------------------------

def process_question(question):
    """
    Main SQL agent workflow.

    Attempts:
    1. Generate SQL.
    2. Validate SQL.
    3. If invalid, ask the LLM to correct it once.
    4. Execute validated SQL.
    """

    previous_sql = None
    validation_error = None

    for attempt in range(2):

        # Generate SQL
        sql = generate_sql(
            question,
            previous_sql,
            validation_error
        )

        # Validate SQL
        is_valid, validation_message = validate_sql(sql)

        if not is_valid:

            app.logger.warning(
                "SQL validation failed (attempt %s): %s",
                attempt + 1,
                validation_message
            )

            previous_sql = sql
            validation_error = validation_message

            continue

        # Execute SQL
        try:

            columns, rows = execute_sql(sql)

            return sql, columns, rows

        except sqlite3.Error as error:

            app.logger.warning(
                "SQL execution failed (attempt %s): %s",
                attempt + 1,
                error
            )

            previous_sql = sql
            validation_error = str(error)

        except ValueError as error:

            previous_sql = sql
            validation_error = str(error)

    raise ValueError(
        "The AI could not generate a valid SQL query "
        f"after two attempts. Last error: {validation_error}"
    )


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

        question = data.get("question", "")

        if not isinstance(question, str):

            return jsonify({
                "error": "Question must be text."
            }), 400

        question = question.strip()

        if not question:

            return jsonify({
                "error": "Please enter a question."
            }), 400

        if len(question) > 1000:

            return jsonify({
                "error": "Question is too long. Please keep it under 1000 characters."
            }), 400

        # ------------------------------------------
        # Generate, validate and execute SQL
        # ------------------------------------------

        sql, columns, rows = process_question(question)

        # ------------------------------------------
        # Explain results
        # ------------------------------------------

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

    except ValueError as error:

        return jsonify({
            "error": str(error)
        }), 400

    except sqlite3.Error as error:

        app.logger.exception("Database error")

        return jsonify({
            "error": f"Database error: {str(error)}"
        }), 500

    except Exception as error:

        app.logger.exception(
            "Question processing failed"
        )

        return jsonify({
            "error": "Something went wrong while processing your question."
        }), 500


# --------------------------------------------------
# Run Application
# --------------------------------------------------

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=False
    )
