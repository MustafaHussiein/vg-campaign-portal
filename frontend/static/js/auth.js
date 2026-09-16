const sb = supabase.createClient(window.SUPABASE_URL, window.SUPABASE_ANON_KEY);

const errorBox = document.getElementById("error");
const signinBtn = document.getElementById("signin");
const googleBtn = document.getElementById("google");

function showError(msg) {
  errorBox.textContent = msg;
  errorBox.classList.add("visible");
}

function clearError() {
  errorBox.classList.remove("visible");
}

// A user can authenticate successfully and still have no profile row —
// most likely via Google, with an email that was never seeded. Auth
// succeeding is not the same as belonging to a brand, so check before
// sending anyone to the dashboard.
// The profiles RLS policy is brand-scoped, so a signed-in user can see
// every profile in their own brand (owner and analyst both). Ask for
// this user's row specifically rather than assuming a single result.
async function hasProfile() {
  const { data: userData } = await sb.auth.getUser();
  if (!userData?.user) return false;
  const { data, error } = await sb
    .from("profiles")
    .select("brand_id")
    .eq("id", userData.user.id)
    .maybeSingle();
  if (error) return false;
  return Boolean(data);
}

async function routeAfterSignIn() {
  if (await hasProfile()) {
    window.location.href = "/dashboard";
  } else {
    await sb.auth.signOut();
    showError("That account isn't set up with a brand yet. Ask your Velocity Growth contact to add it.");
  }
}

signinBtn.addEventListener("click", async () => {
  clearError();
  const email = document.getElementById("email").value.trim();
  const password = document.getElementById("password").value;

  if (!email || !password) {
    showError("Enter your email and password.");
    return;
  }

  signinBtn.disabled = true;
  signinBtn.textContent = "Signing in…";

  const { error } = await sb.auth.signInWithPassword({ email, password });

  if (error) {
    showError("That email and password don't match an account.");
    signinBtn.disabled = false;
    signinBtn.textContent = "Sign in";
    return;
  }

  await routeAfterSignIn();
});

document.getElementById("password").addEventListener("keydown", (e) => {
  if (e.key === "Enter") signinBtn.click();
});

googleBtn.addEventListener("click", async () => {
  clearError();
  const { error } = await sb.auth.signInWithOAuth({
    provider: "google",
    options: { redirectTo: window.location.origin + "/" },
  });
  if (error) showError("Couldn't start Google sign-in. Try again.");
});

// Google sends the user back here with a session already established,
// so re-run the same profile check on load rather than assuming the
// redirect means they're authorised.
(async () => {
  const { data } = await sb.auth.getSession();
  if (data.session) await routeAfterSignIn();
})();