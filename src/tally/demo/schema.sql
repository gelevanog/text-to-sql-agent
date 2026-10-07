-- Lumora: the demo company's database (an online smart-lighting retailer with a subscription service).
-- Created by `tally seed`; the rows come from a deterministic generator (src/tally/demo/generator.py).

CREATE TABLE regions (
    id          smallint PRIMARY KEY,
    code        text NOT NULL UNIQUE,
    name        text NOT NULL,
    timezone    text NOT NULL
);

CREATE TABLE currencies (
    code        char(3) PRIMARY KEY,
    name        text NOT NULL,
    decimals    smallint NOT NULL
);

CREATE TABLE countries (
    code          char(2) PRIMARY KEY,
    name          text NOT NULL,
    region_id     smallint NOT NULL REFERENCES regions (id),
    currency_code char(3) NOT NULL REFERENCES currencies (code)
);

CREATE TABLE fx_rates (
    currency_code char(3) NOT NULL REFERENCES currencies (code),
    rate_date     date NOT NULL,
    usd_per_unit  numeric(18, 8) NOT NULL,
    PRIMARY KEY (currency_code, rate_date)
);

CREATE TABLE categories (
    id          smallint PRIMARY KEY,
    name        text NOT NULL,
    parent_id   smallint REFERENCES categories (id)
);

CREATE TABLE products (
    id               integer PRIMARY KEY,
    sku              text NOT NULL UNIQUE,
    name             text NOT NULL,
    category_id      smallint NOT NULL REFERENCES categories (id),
    list_price_usd   numeric(10, 2) NOT NULL,
    unit_cost_usd    numeric(10, 2) NOT NULL,
    launched_on      date NOT NULL,
    discontinued_on  date
);

CREATE TABLE marketing_channels (
    id          smallint PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    is_paid     boolean NOT NULL
);

CREATE TABLE campaigns (
    id            integer PRIMARY KEY,
    name          text NOT NULL,
    channel_id    smallint NOT NULL REFERENCES marketing_channels (id),
    region_id     smallint REFERENCES regions (id),
    start_date    date NOT NULL,
    end_date      date NOT NULL,
    budget_usd    numeric(12, 2) NOT NULL
);

CREATE TABLE customers (
    id                      integer PRIMARY KEY,
    name                    text NOT NULL,
    email                   text NOT NULL,
    phone                   text,
    street_address          text,
    city                    text NOT NULL,
    country_code            char(2) NOT NULL REFERENCES countries (code),
    region_id               smallint NOT NULL REFERENCES regions (id),
    segment                 text NOT NULL CHECK (segment IN ('consumer', 'business')),
    signup_at               timestamptz NOT NULL,
    acquisition_channel_id  smallint REFERENCES marketing_channels (id),
    marketing_opt_in        boolean NOT NULL,
    is_test_account         boolean NOT NULL DEFAULT false,
    deleted_at              timestamptz
);

CREATE TABLE orders (
    id             integer PRIMARY KEY,
    customer_id    integer NOT NULL REFERENCES customers (id),
    region_id      smallint NOT NULL REFERENCES regions (id),
    country_code   char(2) NOT NULL REFERENCES countries (code),
    ordered_at     timestamptz NOT NULL,
    status         text NOT NULL CHECK (status IN ('placed', 'shipped', 'delivered', 'cancelled', 'returned')),
    currency_code  char(3) NOT NULL REFERENCES currencies (code),
    channel_id     smallint NOT NULL REFERENCES marketing_channels (id),
    campaign_id    integer REFERENCES campaigns (id),
    shipping_fee   numeric(12, 2) NOT NULL DEFAULT 0,
    deleted_at     timestamptz
);

CREATE TABLE order_items (
    id               integer PRIMARY KEY,
    order_id         integer NOT NULL REFERENCES orders (id),
    product_id       integer NOT NULL REFERENCES products (id),
    quantity         integer NOT NULL CHECK (quantity > 0),
    unit_price       numeric(12, 2) NOT NULL,
    discount_amount  numeric(12, 2) NOT NULL DEFAULT 0,
    region_id        smallint NOT NULL REFERENCES regions (id)
);

CREATE TABLE refunds (
    id             integer PRIMARY KEY,
    order_id       integer NOT NULL REFERENCES orders (id),
    order_item_id  integer REFERENCES order_items (id),
    refunded_at    timestamptz NOT NULL,
    amount         numeric(12, 2) NOT NULL,
    reason         text NOT NULL,
    region_id      smallint NOT NULL REFERENCES regions (id)
);

CREATE TABLE plans (
    id             smallint PRIMARY KEY,
    name           text NOT NULL,
    billing_period text NOT NULL CHECK (billing_period IN ('monthly', 'annual')),
    price_usd      numeric(10, 2) NOT NULL,
    is_business    boolean NOT NULL
);

CREATE TABLE plan_price_history (
    plan_id        smallint NOT NULL REFERENCES plans (id),
    valid_from     date NOT NULL,
    price_usd      numeric(10, 2) NOT NULL,
    PRIMARY KEY (plan_id, valid_from)
);

CREATE TABLE subscriptions (
    id             integer PRIMARY KEY,
    customer_id    integer NOT NULL REFERENCES customers (id),
    region_id      smallint NOT NULL REFERENCES regions (id),
    plan_id        smallint NOT NULL REFERENCES plans (id),
    started_at     timestamptz NOT NULL,
    cancelled_at   timestamptz,
    status         text NOT NULL CHECK (status IN ('active', 'cancelled', 'past_due')),
    currency_code  char(3) NOT NULL REFERENCES currencies (code)
);

CREATE TABLE invoices (
    id              integer PRIMARY KEY,
    subscription_id integer NOT NULL REFERENCES subscriptions (id),
    issued_at       timestamptz NOT NULL,
    period_start    date NOT NULL,
    period_end      date NOT NULL,
    amount          numeric(12, 2) NOT NULL,
    currency_code   char(3) NOT NULL REFERENCES currencies (code),
    status          text NOT NULL CHECK (status IN ('paid', 'open', 'void')),
    region_id       smallint NOT NULL REFERENCES regions (id)
);

CREATE TABLE payments (
    id            integer PRIMARY KEY,
    order_id      integer REFERENCES orders (id),
    invoice_id    integer REFERENCES invoices (id),
    paid_at       timestamptz NOT NULL,
    amount        numeric(12, 2) NOT NULL,
    currency_code char(3) NOT NULL REFERENCES currencies (code),
    method        text NOT NULL,
    status        text NOT NULL CHECK (status IN ('succeeded', 'failed')),
    region_id     smallint NOT NULL REFERENCES regions (id),
    CHECK ((order_id IS NULL) <> (invoice_id IS NULL))
);

CREATE TABLE support_tickets (
    id                  integer PRIMARY KEY,
    customer_id         integer NOT NULL REFERENCES customers (id),
    region_id           smallint NOT NULL REFERENCES regions (id),
    order_id            integer REFERENCES orders (id),
    created_at          timestamptz NOT NULL,
    resolved_at         timestamptz,
    category            text NOT NULL,
    priority            text NOT NULL CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    status              text NOT NULL CHECK (status IN ('open', 'pending', 'resolved')),
    satisfaction_score  smallint CHECK (satisfaction_score BETWEEN 1 AND 5),
    subject             text NOT NULL,
    body                text NOT NULL
);

COMMENT ON TABLE orders IS 'One row per checkout. Amounts are in the order currency; deleted_at marks voided duplicates.';
COMMENT ON COLUMN customers.email IS 'PII';
COMMENT ON COLUMN customers.phone IS 'PII';
COMMENT ON COLUMN customers.street_address IS 'PII';
