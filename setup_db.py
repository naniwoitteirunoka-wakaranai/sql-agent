import csv
import os
import random
import sqlite3
import time
from datetime import date, timedelta


# ============================================================
# COCOA & CO. DATABASE BUILDER
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.path.join(BASE_DIR, "database.db")

CUSTOMERS_CSV = os.path.join(
    BASE_DIR, "cocoa_co_customers_v2.csv"
)

PRODUCTS_CSV = os.path.join(
    BASE_DIR, "cocoa_co_products.csv"
)

INGREDIENTS_CSV = os.path.join(
    BASE_DIR, "cocoa_co_ingredients.csv"
)

PRODUCT_INGREDIENTS_CSV = os.path.join(
    BASE_DIR, "cocoa_co_product_ingredients.csv"
)

EMPLOYEES_CSV = os.path.join(
    BASE_DIR, "cocoa_co_employees_final.csv"
)


# ============================================================
# SETTINGS
# ============================================================

ORDER_COUNT = 1_000_000
BATCH_SIZE = 10_000

random.seed(2026)


# ============================================================
# HELPERS
# ============================================================

def read_csv(path):
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"\nMissing file:\n{path}\n\n"
            "Make sure all CSV files are in the same folder "
            "as setup_database.py."
        )

    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def random_date(start, end):
    days = (end - start).days
    return start + timedelta(
        days=random.randint(0, days)
    )


# ============================================================
# LOAD CSV FILES
# ============================================================

print()
print("=" * 60)
print("🍫 COCOA & CO. DATABASE BUILDER")
print("=" * 60)

print("\nLoading CSV files...")

customers = read_csv(CUSTOMERS_CSV)
products = read_csv(PRODUCTS_CSV)
ingredients = read_csv(INGREDIENTS_CSV)
product_ingredients = read_csv(PRODUCT_INGREDIENTS_CSV)
employees = read_csv(EMPLOYEES_CSV)

print(f"Customers:           {len(customers):,}")
print(f"Products:            {len(products):,}")
print(f"Ingredients:         {len(ingredients):,}")
print(f"Product ingredients: {len(product_ingredients):,}")
print(f"Employees:           {len(employees):,}")


# ============================================================
# VALIDATION
# ============================================================

if len(customers) != 10_000:
    raise ValueError(
        f"Expected 10,000 customers, found {len(customers)}"
    )

if len(products) != 200:
    raise ValueError(
        f"Expected 200 products, found {len(products)}"
    )

if len(ingredients) != 50:
    raise ValueError(
        f"Expected 50 ingredients, found {len(ingredients)}"
    )

if len(employees) != 40:
    raise ValueError(
        f"Expected 40 employees, found {len(employees)}"
    )


# ============================================================
# REMOVE OLD DATABASE
# ============================================================

if os.path.exists(DB_PATH):

    print("\nRemoving existing database...")

    # Remove WAL/SHM files too if they exist
    for extra in [
        DB_PATH + "-wal",
        DB_PATH + "-shm"
    ]:
        if os.path.exists(extra):
            os.remove(extra)

    os.remove(DB_PATH)


# ============================================================
# CONNECT TO SQLITE
# ============================================================

print("\nCreating SQLite database...")

conn = sqlite3.connect(DB_PATH)

# SQLite performance settings
conn.execute("PRAGMA journal_mode = WAL;")
conn.execute("PRAGMA synchronous = NORMAL;")
conn.execute("PRAGMA temp_store = MEMORY;")
conn.execute("PRAGMA cache_size = -100000;")

cur = conn.cursor()


# ============================================================
# CREATE TABLES
# ============================================================

print("Creating tables...")

cur.executescript("""

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    email TEXT NOT NULL,
    signup_date TEXT NOT NULL
);


CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT,
    price_eur REAL NOT NULL,
    ingredients TEXT
);


CREATE TABLE ingredients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit TEXT NOT NULL,
    cost_per_unit_eur REAL NOT NULL,
    supplier_name TEXT NOT NULL
);


CREATE TABLE product_ingredients (
    product_id INTEGER NOT NULL,
    ingredient_id INTEGER NOT NULL,
    quantity_required REAL NOT NULL,

    PRIMARY KEY (
        product_id,
        ingredient_id
    ),

    FOREIGN KEY (product_id)
        REFERENCES products(id),

    FOREIGN KEY (ingredient_id)
        REFERENCES ingredients(id)
);


CREATE TABLE employees (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    department TEXT NOT NULL,
    hire_date TEXT NOT NULL,
    salary_eur REAL NOT NULL,
    city TEXT NOT NULL
);


CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    order_date TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    status TEXT NOT NULL,

    FOREIGN KEY (customer_id)
        REFERENCES customers(id)
);


CREATE TABLE order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL,

    FOREIGN KEY (order_id)
        REFERENCES orders(id),

    FOREIGN KEY (product_id)
        REFERENCES products(id)
);

""")


# ============================================================
# INSERT CUSTOMERS
# ============================================================

print("\nImporting customers...")

customer_rows = [
    (
        int(row["id"]),
        row["name"],
        row["city"],
        row["email"],
        row["signup_date"]
    )
    for row in customers
]

cur.executemany(
    """
    INSERT INTO customers
    (
        id,
        name,
        city,
        email,
        signup_date
    )
    VALUES (?, ?, ?, ?, ?)
    """,
    customer_rows
)


# ============================================================
# INSERT PRODUCTS
# ============================================================

print("Importing products...")

product_rows = [
    (
        int(row["id"]),
        row["name"],
        row["category"],
        row["description"],
        float(row["price_eur"]),
        row["ingredients"]
    )
    for row in products
]

cur.executemany(
    """
    INSERT INTO products
    (
        id,
        name,
        category,
        description,
        price_eur,
        ingredients
    )
    VALUES (?, ?, ?, ?, ?, ?)
    """,
    product_rows
)


# ============================================================
# INSERT INGREDIENTS
# ============================================================

print("Importing ingredients...")

ingredient_rows = [
    (
        int(row["id"]),
        row["name"],
        row["category"],
        row["unit"],
        float(row["cost_per_unit_eur"]),
        row["supplier_name"]
    )
    for row in ingredients
]

cur.executemany(
    """
    INSERT INTO ingredients
    (
        id,
        name,
        category,
        unit,
        cost_per_unit_eur,
        supplier_name
    )
    VALUES (?, ?, ?, ?, ?, ?)
    """,
    ingredient_rows
)


# ============================================================
# INSERT PRODUCT INGREDIENTS
# ============================================================

print("Importing product ingredients...")

product_ingredient_rows = [
    (
        int(row["product_id"]),
        int(row["ingredient_id"]),
        float(row["quantity_required"])
    )
    for row in product_ingredients
]

cur.executemany(
    """
    INSERT INTO product_ingredients
    (
        product_id,
        ingredient_id,
        quantity_required
    )
    VALUES (?, ?, ?)
    """,
    product_ingredient_rows
)


# ============================================================
# INSERT EMPLOYEES
# ============================================================

print("Importing employees...")

employee_rows = [
    (
        int(row["id"]),
        row["name"],
        row["role"],
        row["department"],
        row["hire_date"],
        float(row["salary_eur"]),
        row["city"]
    )
    for row in employees
]

cur.executemany(
    """
    INSERT INTO employees
    (
        id,
        name,
        role,
        department,
        hire_date,
        salary_eur,
        city
    )
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """,
    employee_rows
)

conn.commit()


# ============================================================
# PREPARE ORDER DATA
# ============================================================

print("\nPreparing order generation...")

customer_ids = [
    int(row["id"])
    for row in customers
]


product_info = {
    int(row["id"]): {
        "price": float(row["price_eur"])
    }
    for row in products
}


product_ids = list(product_info.keys())


payment_methods = [
    "Credit Card",
    "Debit Card",
    "PayPal",
    "Bancontact",
    "Apple Pay",
    "Google Pay"
]


status_values = [
    "Completed",
    "Processing",
    "Shipped",
    "Cancelled",
    "Refunded"
]


status_weights = [
    0.86,
    0.05,
    0.04,
    0.025,
    0.025
]


ORDER_START = date(2019, 1, 1)
ORDER_END = date(2026, 10, 15)


# ============================================================
# GENERATE ORDERS + ORDER ITEMS
# ============================================================

print()
print("=" * 60)
print("🔥 GENERATING THE TWO MONSTER TABLES")
print("=" * 60)

print(f"Orders: {ORDER_COUNT:,}")
print("Order items: approximately 2M+")
print()
print("SQLite is entering the gym. 💪🍫")
print()


order_sql = """
INSERT INTO orders
(
    id,
    customer_id,
    order_date,
    payment_method,
    status
)
VALUES (?, ?, ?, ?, ?)
"""


order_item_sql = """
INSERT INTO order_items
(
    id,
    order_id,
    product_id,
    quantity,
    unit_price
)
VALUES (?, ?, ?, ?, ?)
"""


order_batch = []
order_item_batch = []

order_item_id = 1
total_items = 0

start_time = time.time()


for order_id in range(
    1,
    ORDER_COUNT + 1
):

    # --------------------------------------------
    # Generate order
    # --------------------------------------------

    customer_id = random.choice(
        customer_ids
    )

    order_date = random_date(
        ORDER_START,
        ORDER_END
    ).isoformat()

    payment_method = random.choice(
        payment_methods
    )

    status = random.choices(
        status_values,
        weights=status_weights,
        k=1
    )[0]


    order_batch.append(
        (
            order_id,
            customer_id,
            order_date,
            payment_method,
            status
        )
    )


    # --------------------------------------------
    # Generate 1-6 products for this order
    # --------------------------------------------

    roll = random.random()

    if roll < 0.55:
        item_count = 1

    elif roll < 0.85:
        item_count = 2

    elif roll < 0.96:
        item_count = 3

    elif roll < 0.995:
        item_count = 4

    else:
        item_count = random.randint(
            5,
            6
        )


    selected_products = random.sample(
        product_ids,
        item_count
    )


    # --------------------------------------------
    # Generate order items
    # --------------------------------------------

    for product_id in selected_products:

        quantity = random.choices(
            [1, 2, 3, 4, 5],
            weights=[
                0.60,
                0.25,
                0.10,
                0.04,
                0.01
            ],
            k=1
        )[0]


        base_price = product_info[
            product_id
        ]["price"]


        # Simulate occasional discounts
        # and price changes.
        price_factor = random.choice(
            [
                1.00,
                1.00,
                1.00,
                0.95,
                0.90,
                1.05
            ]
        )


        unit_price = round(
            base_price * price_factor,
            2
        )


        order_item_batch.append(
            (
                order_item_id,
                order_id,
                product_id,
                quantity,
                unit_price
            )
        )


        order_item_id += 1
        total_items += 1


    # --------------------------------------------
    # Insert every 10,000 orders
    # --------------------------------------------

    if order_id % BATCH_SIZE == 0:

        cur.executemany(
            order_sql,
            order_batch
        )

        cur.executemany(
            order_item_sql,
            order_item_batch
        )

        conn.commit()


        order_batch.clear()
        order_item_batch.clear()


        elapsed = (
            time.time() - start_time
        )

        rate = (
            order_id / elapsed
            if elapsed > 0
            else 0
        )


        print(
            f"{order_id:>10,} / "
            f"{ORDER_COUNT:,} orders | "
            f"{total_items:>10,} items | "
            f"{rate:,.0f} orders/sec"
        )


# ============================================================
# FLUSH REMAINING DATA
# ============================================================

if order_batch:

    cur.executemany(
        order_sql,
        order_batch
    )


if order_item_batch:

    cur.executemany(
        order_item_sql,
        order_item_batch
    )


conn.commit()


# ============================================================
# CREATE INDEXES
# ============================================================

print()
print("Creating indexes...")

cur.executescript("""

CREATE INDEX idx_orders_customer
ON orders(customer_id);

CREATE INDEX idx_orders_date
ON orders(order_date);

CREATE INDEX idx_orders_status
ON orders(status);

CREATE INDEX idx_order_items_order
ON order_items(order_id);

CREATE INDEX idx_order_items_product
ON order_items(product_id);

CREATE INDEX idx_product_ingredients_ingredient
ON product_ingredients(ingredient_id);

CREATE INDEX idx_customers_city
ON customers(city);

CREATE INDEX idx_products_category
ON products(category);

CREATE INDEX idx_employees_department
ON employees(department);

""")


conn.commit()


# ============================================================
# ANALYZE
# ============================================================

print("Running SQLite ANALYZE...")

cur.execute("ANALYZE")

conn.commit()


# ============================================================
# FINAL REPORT
# ============================================================

print()
print("=" * 60)
print("🍫 COCOA & CO. DATABASE READY")
print("=" * 60)

tables = [
    "customers",
    "products",
    "ingredients",
    "product_ingredients",
    "employees",
    "orders",
    "order_items"
]


for table in tables:

    count = cur.execute(
        f"SELECT COUNT(*) FROM {table}"
    ).fetchone()[0]

    print(
        f"{table:<25} {count:>12,} rows"
    )


elapsed = time.time() - start_time

conn.close()


# Calculate approximate database size.
db_size_mb = (
    os.path.getsize(DB_PATH)
    / (1024 * 1024)
)


print()
print("-" * 60)
print(
    f"Database size: {db_size_mb:,.1f} MB"
)
print(
    f"Generation time: {elapsed:,.1f} seconds"
)
print(
    f"Database: {DB_PATH}"
)

print()
print("DONE. 🍫🐉")
print("The database monster has awakened.")
print()
