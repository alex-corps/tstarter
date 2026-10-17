-- Миграция под генератор данных. Идемпотентная.

-- возвраты и сторно пишутся отрицательным количеством
alter table sale_order_day drop constraint if exists sale_order_day_qnty_check;
alter table sale_order_day drop constraint if exists sale_order_day_price_sum_check;
alter table sale_order_day drop constraint if exists sale_order_day_price_inc_sum_check;
alter table sale_order_day add constraint sale_order_day_qnty_check check (qnty <> 0);

-- sale_order_day становится подневной агрегацией: день / клиент / sku
alter table sale_order_day drop column if exists sale_order_detail_id;
alter table sale_order_day drop column if exists sale_order_id;
alter table sale_order_day add column if not exists date_dt date;
alter table sale_order_day add column if not exists customer_id integer;

drop sequence if exists sale_order_day_sale_order_detail_id_seq;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'sale_order_day_pkey' and conrelid = 'sale_order_day'::regclass
    ) then
        alter table sale_order_day
            add constraint sale_order_day_pkey primary key (date_dt, customer_id, sku_id);
    end if;
end $$;

-- income_day: фактический срок поставки и признак первого поступления sku
alter table income_day add column if not exists income_day_cnt integer;
alter table income_day add column if not exists is_new boolean not null default false;

-- скрытое состояние генератора
create schema if not exists gen_internal;

create table if not exists gen_internal.params (
    key text primary key,
    value text not null
);

-- профиль sku: скрытые параметры спроса
create table if not exists gen_internal.sku_profile (
    sku_id integer primary key,
    root_category_id integer not null,
    demand_class text not null,          -- fast / intermittent / lumpy
    base_daily numeric not null,         -- средний дневной спрос, шт
    drift_rate numeric not null,         -- годовая скорость дрейфа
    lifecycle text not null,             -- stable / declining / growing
    shock_prob numeric not null,
    promo_elasticity numeric not null,
    abc_class text not null
);

-- профиль клиента
create table if not exists gen_internal.customer_profile (
    customer_id integer primary key,
    customer_type text not null,
    profile_type text not null,
    order_interval_days numeric not null, -- средний интервал между заказами
    order_size numeric not null,          -- базовый размер заказа, шт
    size_jitter numeric not null,
    activity_from date not null,
    activity_to date
);

-- какие клиенты покупают какой sku
create table if not exists gen_internal.sku_customer (
    sku_id integer not null,
    customer_id integer not null,
    weight numeric not null,
    primary key (sku_id, customer_id)
);

-- профиль поставщика: вариативность срока и недопоставки
create table if not exists gen_internal.supplier_profile (
    supplier_id integer primary key,
    stable boolean not null,
    lead_sigma numeric not null,         -- разброс срока, дней
    lead_bias numeric not null,          -- смещение срока, дней
    short_prob numeric not null          -- базовая вероятность недопоставки
);

-- доли аналогов внутри группы кроссов
create table if not exists gen_internal.cross_share (
    sku_id integer primary key,
    cross_group_id integer not null,
    base_share numeric not null
);

-- годовые аномалии сезонности
create table if not exists gen_internal.year_anomaly (
    year integer primary key,
    shift_months integer not null,
    winter_factor numeric not null
);

-- истинный спрос (ответы для самопроверки)
create table if not exists gen_internal.truth_demand (
    date_dt date not null,
    sku_id integer not null,
    demand_qnty integer not null,
    sold_qnty integer not null,
    lost_qnty integer not null,
    promo boolean not null,
    discount numeric not null,
    primary key (date_dt, sku_id)
);

-- журнал рассчитанных дней
create table if not exists gen_internal.calc_log (
    date_dt date primary key,
    sales_rows integer not null,
    order_rows integer not null,
    income_rows integer not null,
    oos_skus integer not null,
    calculated_at timestamp not null default now()
);

-- открытые бэкордеры поставщиков: недовезённый остаток заказа
create table if not exists gen_internal.backorder (
    supplier_id integer not null,
    sku_id integer not null,
    order_dt date not null,
    order_qnty integer not null,
    open_qnty integer not null,
    resolve_dt date,
    primary key (supplier_id, sku_id, order_dt)
);
