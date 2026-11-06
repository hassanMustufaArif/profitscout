-- Read-only checks. Paste into Supabase -> SQL Editor. Changes nothing.

-- 1. Tables in public WITHOUT row level security (any signed-up user may be able to read/write them).
--    Expect no rows.
select c.relname as table_name
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public'
   and c.relkind in ('r', 'p')
   and not c.relrowsecurity
 order by 1;

-- 2. Tables with RLS on, and the policies on each (empty policies = only service_role can use it).
select c.relname as table_name,
       coalesce(string_agg(p.policyname || ' [' || p.cmd || ' to ' || array_to_string(p.roles, ',') || ']', '; '), '(no policies)') as policies
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  left join pg_policies p on p.schemaname = 'public' and p.tablename = c.relname
 where n.nspname = 'public' and c.relkind in ('r', 'p') and c.relrowsecurity
 group by c.relname
 order by 1;

-- 3. Table privileges still granted to anon (expect none, or only tables meant to be public).
select table_name, string_agg(privilege_type, ', ') as privileges
  from information_schema.role_table_grants
 where table_schema = 'public' and grantee = 'anon'
 group by table_name
 order by 1;

-- 4. Storage policies that let anon / public read (list) objects.
select policyname, cmd, roles, qual
  from pg_policies
 where schemaname = 'storage' and tablename = 'objects'
   and (roles && array['anon', 'public']::name[]);

-- 5. users.plan values (expect only free / pro / ultimate).
select plan, count(*) from public.users group by plan order by 2 desc;
