-- 0005_dining_tables.sql
-- The live table squares on the dashboard ("Table 1, 2, 3 ...", with an Add table button).
--
-- A table is just a label. It counts as OCCUPIED while it has a non-cancelled order (orders.table_number = label)
-- newer than `cleared_after_order_id`; the "Free table" button moves that marker to the newest order id.
-- (An order id marker, not a clock time, so app/database clock differences can never confuse it.)
-- Orders themselves are never touched.
--
-- Access model (same as every other table): ONLY the backend talks to the database, through service_role.
-- RLS is on, nothing is granted to anon / authenticated / public, and there are no policies.
-- Safe to run twice. The app works without it (the squares are simply hidden until it is applied).

create table if not exists public.dining_tables (
  id          bigint generated always as identity primary key,
  label       text        not null check (char_length(btrim(label)) between 1 and 20),
  sort_order  integer     not null default 0,
  cleared_after_order_id bigint not null default 0,   -- orders with a higher id belong to the current sitting
  created_at  timestamptz not null default now()
);

-- "5", " 5" and "5 " are the same table (matches how the app compares orders.table_number).
create unique index if not exists dining_tables_label_uniq on public.dining_tables (lower(btrim(label)));

alter table public.dining_tables enable row level security;

revoke all on table public.dining_tables from public;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table public.dining_tables from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table public.dining_tables from authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant select, insert, update, delete on table public.dining_tables to service_role;
  end if;
end
$$;

-- Starter layout: tables 1 to 6. The owner adds more from the dashboard.
insert into public.dining_tables (label, sort_order, cleared_after_order_id)
select g::text, g, coalesce((select max(id) from public.orders), 0) from generate_series(1, 6) as g
on conflict do nothing;
