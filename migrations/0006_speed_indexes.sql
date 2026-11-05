-- 0006_speed_indexes.sql
-- Optional speed-up. The app lists orders by month (created_at) and loads their items by order_id on every
-- screen change; these indexes keep that fast as the order history grows. Safe to run more than once.
create index if not exists orders_created_at_idx   on public.orders (created_at desc, id desc);
create index if not exists order_items_order_id_idx on public.order_items (order_id);
create index if not exists orders_table_number_idx  on public.orders (lower(btrim(table_number)));
