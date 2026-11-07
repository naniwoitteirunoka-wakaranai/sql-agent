
import os
import json
import sqlite3
import re
from pathlib import Path

import zstandard as zstd
import sqlglot
from sqlglot import exp

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    Response,
    stream_with_context
)

from dotenv import load_dotenv
from groq import Groq


# ==================================================
# CONFIGURATION
# ==================================================

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)

DATABASE_PATH = BASE_DIR / "database.db"
COMPRESSED_DB_PATH = BASE_DIR / "database.db.zst"

groq_api_key = os.getenv("GROQ_API_KEY")

if not groq_api_key:
    raise RuntimeError("GROQ_API_KEY is missing.")

client = Groq(api_key=groq_api_key)

MODEL = "openai/gpt-oss-120b"

MAX_SQL_ROWS = 200
MAX_HISTORY_TURNS = 8


# ==================================================
# DATABASE RESTORATION
# ==================================================

def ensure_database():

    if DATABASE_PATH.exists():
        print("Database already exists.")
        return

    if not COMPRESSED_DB_PATH.exists():
        raise FileNotFoundError(
            f"Compressed database not found: {COMPRESSED_DB_PATH}"
        )

    print("Restoring compressed database...")

    temp_path = DATABASE_PATH.with_suffix(".tmp")

    try:

        with open(COMPRESSED_DB_PATH, "rb") as source:
            with open(temp_path, "wb") as output:
                zstd.ZstdDecompressor().copy_stream(
                    source, output
                )

        connection = sqlite3.connect(temp_path)

        try:
            result = connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()

            if not result or result[0] != "ok":
                raise sqlite3.DatabaseError(
                    "Database integrity check failed."
                )

        finally:
            connection.close()

        temp_path.replace(DATABASE_PATH)

        print("Database restored successfully.")

    except Exception:

        if temp_path.exists():
            temp_path.unlink()

        raise


ensure_database()


# ==================================================
# READ-ONLY DATABASE CONNECTION
# ==================================================

def get_db():

    uri = DATABASE_PATH.as_uri() + "?mode=ro"

    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=30
    )

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")

    return connection


# ==================================================
# DATABASE SCHEMA
# ==================================================

DATABASE_SCHEMA = """
Database: Veloura Chocolates

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

RELATIONSHIPS:

customers.id -> orders.customer_id
orders.id -> order_items.order_id
products.id -> order_items.product_id
products.id -> product_ingredients.product_id
ingredients.id -> product_ingredients.ingredient_id
"""


# ==================================================
# CLEAN SQL
# ==================================================

def clean_sql(sql):

    if not isinstance(sql, str):
        return ""

    sql = sql.strip()

    sql = re.sub(
        r"^```(?:sql)?\s*",
        "",
        sql,
        flags=re.IGNORECASE
    )

    sql = re.sub(r"\s*```$", "", sql)

    return sql.strip()


# ==================================================
# SQL VALIDATOR
# ==================================================

def validate_sql(sql):

    sql = clean_sql(sql)

    if not sql:
        return False, "SQL query is empty."

    try:

        statements = [
            statement
            for statement in sqlglot.parse(
                sql,
                read="sqlite"
            )
            if statement is not None
        ]

    except sqlglot.errors.ParseError as error:

        return False, f"SQL syntax error: {error}"

    if len(statements) != 1:
        return False, "Multiple SQL statements are not allowed."

    parsed = statements[0]

    if not isinstance(parsed, exp.Query):
        return False, "Only SELECT queries are permitted."

    forbidden_nodes = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        exp.Alter,
        exp.Command,
    )

    for node in parsed.walk():

        if isinstance(node, forbidden_nodes):
            return False, "Forbidden SQL operation detected."

    connection = None

    try:

        connection = get_db()

        connection.execute(
            "EXPLAIN QUERY PLAN " + sql
        )

    except sqlite3.Error as error:

        return False, f"Database validation failed: {error}"

    finally:

        if connection:
            connection.close()

    return True, "SQL validation successful."


# ==================================================
# SQLITE AUTHORZER
# ==================================================

def sqlite_authorizer(action, arg1, arg2, database, trigger):

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

    if action == sqlite3.SQLITE_FUNCTION:

        function_name = (arg2 or arg1 or "").lower()

        if function_name in {"load_extension", "writefile"}:
            return sqlite3.SQLITE_DENY

    return sqlite3.SQLITE_OK


# ==================================================
# CONVERSATION HISTORY
# ==================================================

def clean_history(history):

    if not isinstance(history, list):
        return []

    cleaned = []

    for item in history[-MAX_HISTORY_TURNS:]:

        if not isinstance(item, dict):
            continue

        question = item.get("question", "")
        answer = item.get("answer", "")

        if not isinstance(question, str):
            continue

        if not isinstance(answer, str):
            continue

        cleaned.append({
            "question": question[:1000],
            "answer": answer[:2000]
        })

    return cleaned


def format_history(history):

    if not history:
        return "No previous conversation."

    formatted = []

    for item in history:

        formatted.append(
            f"Previous user question: {item['question']}\n"
            f"Previous assistant answer: {item['answer']}"
        )

    return "\n\n".join(formatted)


# ==================================================
# GENERATE SQL
# ==================================================

def generate_sql(
    question,
    history=None,
    previous_sql=None,
    error_message=None
):

    history_context = format_history(history or [])

    correction_context = ""

    if previous_sql and error_message:

        correction_context = f"""
Your previous SQL was invalid.

Previous SQL:
{previous_sql}

Validation or execution error:
{error_message}

Correct the SQL using the actual database schema.
"""

    system_prompt = f"""
You are the SQL analyst for Veloura Chocolates.

Convert business questions into SQLite SQL queries.

DATABASE SCHEMA:
{DATABASE_SCHEMA}

PREVIOUS CONVERSATION:
{history_context}

RULES:

1. Return ONLY SQL.
2. Generate only SELECT queries.
3. Never modify the database.
4. Use SQLite-compatible SQL.
5. Use JOINs when necessary.
6. Use clear column aliases.
7. Calculate revenue using quantity * unit_price.
8. For product revenue, join orders, order_items and products.
9. For ingredient costs, use product_ingredients and ingredients.
10. Never invent tables or columns.
11. Do not generate multiple SQL statements.
12. Use previous conversation context to resolve follow-up questions.
13. Resolve references such as "it", "that city", "second one",
    and "those products" using conversation history.
14. Return SQL only, without Markdown or explanations.

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

    return clean_sql(
        response.choices[0].message.content
    )


# ==================================================
# EXECUTE SQL
# ==================================================

def execute_sql(sql):

    valid, message = validate_sql(sql)

    if not valid:
        raise ValueError(message)

    connection = get_db()

    try:

        connection.set_authorizer(sqlite_authorizer)

        cursor = connection.execute(sql)

        if cursor.description is None:
            raise ValueError(
                "Query did not return a result set."
            )

        rows = cursor.fetchmany(MAX_SQL_ROWS)

        columns = [
            description[0]
            for description in cursor.description
        ]

        data = [dict(row) for row in rows]

        return columns, data

    finally:
        connection.close()


# ==================================================
# EXPLAIN RESULTS
# ==================================================

def explain_result(
    question,
    sql,
    columns,
    rows,
    history=None
):

    result_data = {
        "columns": columns,
        "rows": rows
    }

    prompt = f"""
You are a business analyst for Veloura Chocolates.

Previous conversation:
{format_history(history or [])}

Current user question:
{question}

SQL query:
{sql}

Actual database results:
{json.dumps(result_data, default=str)}

Explain the results in concise business language.

Rules:
- Focus on actual results.
- Mention important numbers.
- Give useful business insights.
- Do not invent information.
- Do not repeat SQL.
- Keep the answer to 2-4 sentences.
- Use previous conversation only when relevant.
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


# ==================================================
# LIVE AGENT STREAM
# ==================================================

def stream_agent(question, history):

    def send_event(event_type, payload):

        return (
            "data: "
            + json.dumps(
                {"type": event_type, **payload},
                default=str
            )
            + "\n\n"
        )

    def send_trace(name, detail, status="completed"):

        return send_event("trace", {
            "name": name,
            "detail": detail,
            "status": status
        })

    try:

        # STEP 1: Understand request

        yield send_trace(
            "Understanding request",
            "Processing your business question and conversation context."
        )

        previous_sql = None
        validation_error = None

        sql = None
        columns = None
        rows = None

        # STEP 2: Generate and validate SQL

        for attempt in range(2):

            yield send_trace(
                "Generating SQL",
                "Groq is generating a SQLite query."
                if attempt == 0 else
                "Groq is correcting the previous query."
            )

            sql = generate_sql(
                question,
                history,
                previous_sql,
                validation_error
            )

            yield send_trace(
                "Validating SQL",
                "Checking SQL syntax, query type and database compatibility."
            )

            valid, message = validate_sql(sql)

            if not valid:

                yield send_trace(
                    "SQL validation",
                    message,
                    "failed"
                )

                previous_sql = sql
                validation_error = message

                if attempt == 0:

                    yield send_trace(
                        "Self-correction",
                        "Sending the validation error back to Groq."
                    )

                continue

            yield send_trace(
                "SQL validation",
                "Query passed validation. Read-only access confirmed."
            )

            # STEP 3: Execute query

            yield send_trace(
                "Database execution",
                "Executing the validated query against SQLite."
            )

            try:

                columns, rows = execute_sql(sql)

                yield send_trace(
                    "Database execution",
                    f"Successfully retrieved {len(rows)} rows."
                )

                break

            except (sqlite3.Error, ValueError) as error:

                yield send_trace(
                    "Database execution",
                    str(error),
                    "failed"
                )

                previous_sql = sql
                validation_error = str(error)

                if attempt == 0:

                    yield send_trace(
                        "Self-correction",
                        "Attempting to correct the query using the execution error."
                    )

        if columns is None:

            raise ValueError(
                "Could not generate a valid SQL query after two attempts. "
                f"Last error: {validation_error}"
            )

        # STEP 4: Explain results

        yield send_trace(
            "Generating business insight",
            "Groq is analysing the database results."
        )

        explanation = explain_result(
            question,
            sql,
            columns,
            rows,
            history
        )

        yield send_trace(
            "Completed",
            "Your business answer is ready."
        )

        # STEP 5: Send result

        yield send_event("result", {
            "question": question,
            "sql": sql,
            "columns": columns,
            "rows": rows,
            "explanation": explanation
        })

    except Exception as error:

        app.logger.exception("Streaming agent failed")

        message = (
            str(error)
            if isinstance(error, ValueError)
            else "Something went wrong while processing your question."
        )

        yield send_event("error", {
            "error": message
        })


# ==================================================
# ROUTES
# ==================================================

@app.route("/")
def dashboard():

    return render_template("dashboard.html")


@app.route("/api/ask/stream", methods=["POST"])
def ask_question_stream():

    data = request.get_json(silent=True)

    if not isinstance(data, dict):

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
            "error": "Question must be under 1000 characters."
        }), 400

    history = clean_history(
        data.get("history", [])
    )

    return Response(

        stream_with_context(
            stream_agent(question, history)
        ),

        mimetype="text/event-stream",

        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive"
        }
    )


# ==================================================
# RUN APPLICATION
# ==================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=False,
        threaded=True
    )
