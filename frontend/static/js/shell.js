// Shared app shell: session guard, brand/role header, authed fetch.
const sb = supabase.createClient(window.SUPABASE_URL, window.SUPABASE_ANON_KEY);

const BRAND_CODES = {
  "kilele-rides": "KIL",
  "karoo-coaches": "KAR",
  "marrakech-express": "MAR",
};

let SESSION = null;
let PROFILE = null;

async function requireSession() {
  const { data } = await sb.auth.getSession();
  if (!data.session) {
    window.location.href = "/";
    return null;
  }
  SESSION = data.session;

  const { data: prof, error } = await sb
    .from("profiles")
    .select("role, brands(slug, name)")
    .eq("id", SESSION.user.id)
    .maybeSingle();

  if (error || !prof) {
    window.location.href = "/";
    return null;
  }
  PROFILE = prof;
  return prof;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${SESSION.access_token}`,
      ...(options.headers || {}),
    },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body.detail || "Something went wrong. Try again.");
  }
  return body;
}

function renderHeader(active) {
  const brand = PROFILE.brands;
  const isOwner = PROFILE.role === "owner";
  const nav = [
    ["/dashboard", "Dashboard"],
    ["/contacts", "Contacts"],
    ["/campaigns", "Campaigns"],
  ];
  document.getElementById("shell-header").innerHTML = `
    <div class="app-brand">
      <span class="code">${BRAND_CODES[brand.slug] || ""}</span>${brand.name}
    </div>
    <nav class="app-nav">
      ${nav.map(([href, label]) =>
        `<a href="${href}" class="${active === href ? "active" : ""}">${label}</a>`).join("")}
      <span class="role-pill">${isOwner ? "Owner" : "Analyst — view only"}</span>
      <button class="linkbtn" id="signout">Sign out</button>
    </nav>`;
  document.getElementById("signout").addEventListener("click", async () => {
    await sb.auth.signOut();
    window.location.href = "/";
  });
}

function fmt(n) {
  return (n ?? 0).toLocaleString();
}

function showPageError(msg) {
  const el = document.getElementById("page-error");
  if (!el) return;
  el.textContent = msg;
  el.classList.add("visible");
}
