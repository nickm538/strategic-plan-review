// Sign-in page: JSON login, readable errors (lockout shows minutes left), then go to the app.
const form = document.getElementById("login-form");
const err = document.getElementById("login-error");
const btn = document.getElementById("login-submit");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  err.textContent = "";
  const name = document.getElementById("name").value.trim();
  const password = document.getElementById("password").value;
  if (!name || !password) { err.textContent = "Enter your name and password."; return; }
  btn.disabled = true;
  try {
    const res = await fetch("/api/auth/login", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ name, password }),
    });
    if (res.ok) { window.location.assign("/"); return; }
    let detail = "Sign-in failed.";
    try { const body = await res.json(); if (typeof body.detail === "string") detail = body.detail; } catch { /* keep default */ }
    err.textContent = detail;
  } catch {
    err.textContent = "Network error. Check your connection and try again.";
  } finally {
    btn.disabled = false;
  }
});
