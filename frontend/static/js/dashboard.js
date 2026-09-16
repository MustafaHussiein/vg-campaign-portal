(async () => {
  if (!(await requireSession())) return;
  renderHeader("/dashboard");

  // --- headline figures ---
  try {
    const d = await api("/api/dashboard");
    document.getElementById("stats").innerHTML = `
      <div>
        <div class="stat-figure">${fmt(d.total_customers)}</div>
        <div class="stat-name">Customers — excludes deleted records</div>
      </div>
      <div>
        <div class="stat-figure">${fmt(d.contactable)}</div>
        <div class="stat-name">Contactable — opted in, not unsubscribed, not suppressed after a bounce, has an email</div>
      </div>
      <div>
        <div class="stat-figure">${fmt(d.unsubscribed)}</div>
        <div class="stat-name">Unsubscribed</div>
      </div>
      <div>
        <div class="stat-figure">${fmt(d.unknown_consent)}</div>
        <div class="stat-name">Consent unreadable in the import — counted as not contactable</div>
      </div>`;

    if (d.load_error_total > 0) {
      document.getElementById("quality").innerHTML = `
        <div class="notice">
          <strong>${fmt(d.load_error_total)} rows didn't load cleanly from your last import.</strong>
          The figures below exclude them.
          <ul>${d.load_errors.map(e => `<li>${e.message} — ${fmt(e.count)}</li>`).join("")}</ul>
        </div>`;
    }
  } catch (e) {
    showPageError(e.message);
    document.getElementById("stats").innerHTML = "";
  }

  // --- signups chart ---
  try {
    const { days } = await api("/api/signups");
    const chart = document.getElementById("chart");
    if (!days.length) {
      chart.innerHTML = `<div class="empty">No signups recorded in the last 30 days.</div>`;
    } else {
      const W = 720, H = 180, pad = 26;
      const max = Math.max(...days.map(d => d.count));
      const bw = (W - pad * 2) / days.length;
      const bars = days.map((d, i) => {
        const h = max ? (H - pad * 2) * (d.count / max) : 0;
        const x = pad + i * bw;
        const y = H - pad - h;
        return `<rect class="bar" x="${x + 1}" y="${y}" width="${Math.max(bw - 2, 1)}" height="${h}">
                  <title>${d.day}: ${fmt(d.count)}</title></rect>`;
      }).join("");
      const first = days[0].day, last = days[days.length - 1].day;
      chart.innerHTML = `
        <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Signups per day over the last 30 days">
          ${bars}
          <text class="axis-text" x="${pad}" y="${H - 8}">${first}</text>
          <text class="axis-text" x="${W - pad}" y="${H - 8}" text-anchor="end">${last}</text>
          <text class="axis-text" x="${pad}" y="${pad - 8}">peak ${fmt(max)}/day</text>
        </svg>`;
    }
  } catch (e) {
    document.getElementById("chart").innerHTML = `<div class="empty">Couldn't load the signup trend.</div>`;
  }

  // --- campaign performance ---
  try {
    const { rows } = await api("/api/campaign-performance");
    const wrap = document.getElementById("perf");
    if (!rows.length) {
      wrap.innerHTML = `<div class="empty">No campaigns yet.</div>`;
      return;
    }
    wrap.innerHTML = `
      <table>
        <thead><tr>
          <th>Campaign</th><th>Sent</th>
          <th class="num">Sent to</th>
          <th class="num">Delivered<br><span class="muted">reported</span></th>
          <th class="num">Bounced<br><span class="muted">reported</span></th>
          <th class="num">Bounced<br><span class="muted">observed</span></th>
          <th class="num">Opens<br><span class="muted">reported</span></th>
          <th class="num">Opens<br><span class="muted">observed</span></th>
        </tr></thead>
        <tbody>
          ${rows.map(r => `
            <tr>
              <td>${r.name || r.external_id}</td>
              <td class="muted">${r.sent_at ? r.sent_at.slice(0, 10) : "—"}</td>
              <td class="num">${fmt(r.reported_sent)}</td>
              <td class="num">${fmt(r.reported_delivered)}</td>
              <td class="num">${fmt(r.reported_bounced)}</td>
              <td class="num">${fmt(r.observed.bounce)}</td>
              <td class="num">${fmt(r.reported_opens)}</td>
              <td class="num">${fmt(r.observed.open)}</td>
            </tr>`).join("")}
        </tbody>
      </table>`;
  } catch (e) {
    document.getElementById("perf").innerHTML = `<div class="empty">Couldn't load campaign performance.</div>`;
  }
})();
