let CAN_SEND = false;
let pendingCampaign = null;   // { id, count }
let currentSendId = null;
let shareCampaignId = null;

const el = (id) => document.getElementById(id);
const openModal = (id) => el(id).classList.add("open");
const closeModal = (id) => el(id).classList.remove("open");

function modalError(id, msg) {
  const box = el(id);
  box.textContent = msg;
  box.classList.add("visible");
}
function clearModalError(id) { el(id).classList.remove("visible"); }

async function loadCampaigns() {
  const wrap = el("table");
  wrap.innerHTML = `<div class="empty">Loading…</div>`;
  try {
    const d = await api("/api/campaigns");
    CAN_SEND = d.can_send;
    el("role-note").textContent = CAN_SEND
      ? "You can send campaigns and publish results."
      : "View only. Sending is limited to owners.";

    if (!d.rows.length) {
      wrap.innerHTML = `<div class="empty">No campaigns yet.</div>`;
      return;
    }

    wrap.innerHTML = `
      <table>
        <thead><tr>
          <th>Campaign</th><th>Channel</th><th>Sent</th>
          <th class="num">Reported sent</th><th>Send status</th><th></th>
        </tr></thead>
        <tbody>
          ${d.rows.map(c => {
            const send = (c.sends || [])[0];
            let statusCell = `<span class="muted">Not sent from here</span>`;
            if (send) {
              const cls = send.status === "completed" ? "ok"
                        : send.status === "failed" ? "bad" : "warn";
              statusCell = `<span class="tag ${cls}">${send.status}</span>
                            <span class="muted"> ${fmt(send.recipient_count)} recipients</span>`;
            }
            const actions = [];
            if (CAN_SEND && !send) actions.push(`<button class="btn-sm" data-send="${c.id}">Send</button>`);
            if (send) actions.push(`<button class="btn-sm ghost" data-results="${send.id}">Results</button>`);
            if (CAN_SEND) actions.push(`<button class="btn-sm ghost" data-share="${c.id}">Publish</button>`);
            return `<tr>
              <td>${(c.name ?? c.external_id ?? "").replace(/</g, "&lt;")}</td>
              <td class="muted">${c.channel ?? "—"}</td>
              <td class="muted">${c.sent_at ? c.sent_at.slice(0, 10) : "—"}</td>
              <td class="num">${fmt(c.reported_sent)}</td>
              <td>${statusCell}</td>
              <td>${actions.join(" ")}</td>
            </tr>`;
          }).join("")}
        </tbody>
      </table>`;

    wrap.querySelectorAll("[data-send]").forEach(b =>
      b.addEventListener("click", () => startSend(b.dataset.send)));
    wrap.querySelectorAll("[data-results]").forEach(b =>
      b.addEventListener("click", () => showResults(b.dataset.results)));
    wrap.querySelectorAll("[data-share]").forEach(b =>
      b.addEventListener("click", () => startShare(b.dataset.share)));
  } catch (e) {
    wrap.innerHTML = `<div class="empty">${e.message}</div>`;
  }
}

// ---------- Send ----------
async function startSend(campaignId) {
  clearModalError("confirm-error");
  el("confirm-count").textContent = "…";
  el("confirm-basis").textContent = "";
  el("confirm-sample").textContent = "";
  el("confirm-go").disabled = true;
  openModal("confirm-back");

  try {
    const p = await api("/api/sends/preview", {
      method: "POST",
      body: JSON.stringify({ campaign_id: campaignId }),
    });
    if (p.already_sent) {
      modalError("confirm-error", "This campaign has already been sent.");
      return;
    }
    pendingCampaign = { id: campaignId, count: p.recipient_count };
    el("confirm-title").textContent = p.campaign_name;
    el("confirm-count").textContent = fmt(p.recipient_count) + " people";
    el("confirm-basis").textContent = p.basis;
    el("confirm-sample").innerHTML = p.sample.length
      ? "e.g. " + p.sample.map(s => s.email).join(", ")
      : "";
    el("confirm-go-count").textContent = fmt(p.recipient_count);
    el("confirm-go").disabled = false;
  } catch (e) {
    modalError("confirm-error", e.message);
  }
}

el("confirm-cancel").addEventListener("click", () => closeModal("confirm-back"));

el("confirm-go").addEventListener("click", async () => {
  if (!pendingCampaign) return;
  const btn = el("confirm-go");
  btn.disabled = true;                      // guards the double-click
  btn.textContent = "Sending…";
  clearModalError("confirm-error");

  try {
    // expected_count is the number shown on screen. The server rejects
    // the send if the audience has moved since, rather than quietly
    // emailing a different number of people.
    const r = await api("/api/sends/confirm", {
      method: "POST",
      body: JSON.stringify({
        campaign_id: pendingCampaign.id,
        expected_count: pendingCampaign.count,
      }),
    });
    closeModal("confirm-back");
    await loadCampaigns();
    showResults(r.send_id, r.already_sent ? r.message : null);
  } catch (e) {
    modalError("confirm-error", e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Send to " + fmt(pendingCampaign.count);
    el("confirm-go-count").textContent = fmt(pendingCampaign.count);
  }
});

// ---------- Results ----------
async function showResults(sendId, note) {
  currentSendId = sendId;
  clearModalError("result-error");
  el("result-body").innerHTML = `<p class="muted">Loading…</p>`;
  openModal("result-back");
  if (note) modalError("result-error", note);

  try {
    const d = await api(`/api/sends/${sendId}`);
    renderResults(d);
  } catch (e) {
    el("result-body").innerHTML = "";
    modalError("result-error", e.message);
  }
}

function renderResults(d) {
  const s = d.send, b = d.breakdown;
  el("result-body").innerHTML = `
    <div class="result-line"><span>Approved and sent to</span><span class="v">${fmt(s.recipient_count)}</span></div>
    <div class="result-line"><span>Delivered</span><span class="v">${fmt(b.delivered)}</span></div>
    <div class="result-line"><span>Opened</span><span class="v">${fmt(b.opened)}</span></div>
    <div class="result-line"><span>Bounced</span><span class="v">${fmt(b.bounced)}</span></div>
    <div class="result-line"><span>Unsubscribed</span><span class="v">${fmt(b.unsubscribed)}</span></div>
    <div class="result-line"><span>Awaiting a report</span><span class="v">${fmt(b.queued)}</span></div>
    <p class="basis" style="margin-top:14px">
      ${fmt(d.events_received)} provider events recorded.
      ${s.last_polled_at ? "Last checked " + new Date(s.last_polled_at).toLocaleString() + "." : "Not checked yet."}
      Status is derived from every event received, so late or repeated reports don't change the totals.
    </p>
    <p class="basis">Batch ${s.batch_id ?? "—"}</p>`;
}

el("result-close").addEventListener("click", () => closeModal("result-back"));

el("result-refresh").addEventListener("click", async () => {
  const btn = el("result-refresh");
  btn.disabled = true;
  btn.textContent = "Checking…";
  clearModalError("result-error");
  try {
    const d = await api(`/api/sends/${currentSendId}/refresh`, { method: "POST" });
    renderResults(d);
  } catch (e) {
    modalError("result-error", e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Refresh from provider";
  }
});

// ---------- Share ----------
function startShare(campaignId) {
  shareCampaignId = campaignId;
  clearModalError("share-error");
  el("share-form").style.display = "";
  el("share-done").style.display = "none";
  el("share-go").style.display = "";
  el("share-pw").value = "";
  openModal("share-back");
}

el("share-cancel").addEventListener("click", () => closeModal("share-back"));

el("share-go").addEventListener("click", async () => {
  clearModalError("share-error");
  const pw = el("share-pw").value;
  if (pw.length < 8) {
    modalError("share-error", "Use a password of at least 8 characters.");
    return;
  }
  const btn = el("share-go");
  btn.disabled = true;
  try {
    const r = await api("/api/share", {
      method: "POST",
      body: JSON.stringify({ campaign_id: shareCampaignId, password: pw }),
    });
    el("share-form").style.display = "none";
    el("share-done").style.display = "";
    el("share-url").textContent = r.url;
    btn.style.display = "none";
  } catch (e) {
    modalError("share-error", e.message);
  } finally {
    btn.disabled = false;
  }
});

(async () => {
  if (!(await requireSession())) return;
  renderHeader("/campaigns");
  loadCampaigns();
})();
