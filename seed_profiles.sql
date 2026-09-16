-- Links the six auth users to their brand and role.
-- Run AFTER creating the six users in Authentication -> Users and
-- after inserting the three brands.
insert into profiles (id, brand_id, role, full_name)
select u.id, b.id, v.role::user_role, v.full_name
from (values
  ('owner@kilelerides.test',        'kilele-rides',      'owner',   'Kilele Owner'),
  ('analyst@kilelerides.test',      'kilele-rides',      'analyst', 'Kilele Analyst'),
  ('owner@karoocoaches.test',       'karoo-coaches',     'owner',   'Karoo Owner'),
  ('analyst@karoocoaches.test',     'karoo-coaches',     'analyst', 'Karoo Analyst'),
  ('owner@marrakechexpress.test',   'marrakech-express', 'owner',   'Marrakech Owner'),
  ('analyst@marrakechexpress.test', 'marrakech-express', 'analyst', 'Marrakech Analyst')
) as v(email, brand_slug, role, full_name)
join auth.users u on u.email = v.email
join brands b on b.slug = v.brand_slug
on conflict (id) do nothing;
