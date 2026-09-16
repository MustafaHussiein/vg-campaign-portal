// Deliberately has no Supabase client and no keys — a stranger's page
// should carry nothing but the token in its own URL.
const $ = (id) => document.getElementById(id);

function showError(msg) {
  $("error").textContent = msg;
  $("error").classList.add("visible");
}

async function open() {
  $("error").classList.remove("visible");
  const pw = $("pw").value;
  if (!pw) { showError("Enter the password."); return; }

  const btn = $("open");
  btn.disabled = true;
  btn.textContent = "Checking…";

  try {
    const res = await fetch(`/api/shared/${window.SHARE_TOKEN}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pw }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || "That link and password don't match.");

    $("gate").style.display = "none";
    $("results").style.display = "";
    $("camp-name").textContent = body.campaign_name ?? "Campaign results";
    $("camp-meta").textContent = [
      body.channel,
      body.sent_at ? "sent " + String(body.sent_at).slice(0, 10) : null,
    ].filter(Boolean).join(" · ");

    const line = (label, value) =>
      `<div class="result-line"><span>${label}</span><span class="v">${(value ?? 0).toLocaleString()}</span></div>`;

    $("numbers").innerHTML =
      line("Sent to", body.reported_sent) +
      line("Delivered", body.reported_delivered) +
      line("Bounced (reported)", body.reported_bounced) +
      line("Bounced (observed in log)", body.observed_bounced) +
      line("Opens (reported)", body.reported_opens) +
      line("Opens (observed in log)", body.observed_opens) +
      line("Clicks (observed in log)", body.observed_clicks) +
      `<p class="sub" style="margin-top:16px">Reported figures come from the messaging provider at
       send time. Observed figures are counted from the raw engagement log. Where they differ, both
       are shown rather than picking one.</p>`;
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "View results";
  }
}

$("open").addEventListener("click", open);
$("pw").addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
