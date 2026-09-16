let page = 0, pageSize = 50, total = 0, query = "";

function statusTag(c) {
  if (c.unsubscribed_at) return `<span class="tag bad">Unsubscribed</span>`;
  if (c.suppressed_until && new Date(c.suppressed_until) > new Date())
    return `<span class="tag warn">Suppressed</span>`;
  if (c.consent_marketing === true) return `<span class="tag ok">Contactable</span>`;
  if (c.consent_marketing === null) return `<span class="tag warn">Consent unknown</span>`;
  return `<span class="tag">Not opted in</span>`;
}

async function load() {
  const wrap = document.getElementById("table");
  wrap.innerHTML = `<div class="empty">Loading…</div>`;
  try {
    const d = await api(`/api/contacts?page_num=${page}&page_size=${pageSize}&search=${encodeURIComponent(query)}`);
    total = d.total;

    document.getElementById("count-note").textContent =
      query ? `${fmt(total)} matching contacts` : `${fmt(total)} contacts, excluding deleted records`;

    if (!d.rows.length) {
      wrap.innerHTML = `<div class="empty">${query ? "Nothing matches that search." : "No contacts loaded yet."}</div>`;
    } else {
      wrap.innerHTML = `
        <table>
          <thead><tr>
            <th>ID</th><th>Name</th><th>Email</th><th>Country</th>
            <th>City</th><th>Signed up</th><th>Status</th>
          </tr></thead>
          <tbody>
            ${d.rows.map(c => `
              <tr>
                <td class="muted">${c.external_id ?? ""}</td>
                <td>${(c.full_name ?? "").replace(/</g, "&lt;")}</td>
                <td>${c.email ?? `<span class="muted">none</span>`}</td>
                <td>${c.country ?? `<span class="muted">—</span>`}</td>
                <td>${c.city ?? `<span class="muted">—</span>`}</td>
                <td class="muted">${c.signup_at ? c.signup_at.slice(0, 10) : "—"}</td>
                <td>${statusTag(c)}</td>
              </tr>`).join("")}
          </tbody>
        </table>`;
    }

    const pages = Math.max(Math.ceil(total / pageSize), 1);
    document.getElementById("page-info").textContent = `Page ${page + 1} of ${fmt(pages)}`;
    document.getElementById("prev").disabled = page === 0;
    document.getElementById("next").disabled = page + 1 >= pages;
  } catch (e) {
    wrap.innerHTML = `<div class="empty">${e.message}</div>`;
  }
}

(async () => {
  if (!(await requireSession())) return;
  renderHeader("/contacts");

  let timer;
  document.getElementById("search").addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { query = e.target.value.trim(); page = 0; load(); }, 300);
  });
  document.getElementById("prev").addEventListener("click", () => { if (page > 0) { page--; load(); } });
  document.getElementById("next").addEventListener("click", () => { page++; load(); });

  load();
})();
