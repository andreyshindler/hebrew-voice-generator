/* Login and signup. Both pages post JSON like the rest of the API, so there
   are no HTML form submissions anywhere and no multipart parser on the server. */

import { api, ApiError, BASE, url } from "./api.js";
import { $ } from "./ui.js";

const errorBox = $("#form-error");

function showError(message) {
  if (!errorBox) return;
  errorBox.textContent = message;
  errorBox.hidden = false;
}

function nextTarget() {
  const next = new URLSearchParams(window.location.search).get("next");
  // Only ever follow a path inside this app: never an absolute URL from the
  // query string, and never a sibling app sharing the hostname.
  const safe =
    next && next.startsWith(`${BASE}/`) && !next.startsWith("//") ? next : null;
  return safe || url("/");
}

/** Replace the form with a "we sent you a link" panel. */
function showVerificationNotice(form, email) {
  const notice = $("#verify-notice");
  if (!notice) return;
  const target = $("#verify-email");
  if (target) target.textContent = email;
  form.hidden = true;
  notice.hidden = false;
}

function wire(form, submit) {
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (errorBox) errorBox.hidden = true;

    const button = form.querySelector("button[type=submit]");
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "רגע…";

    const data = Object.fromEntries(new FormData(form).entries());
    try {
      const result = await submit(data);
      // Signup with verification on returns no session - stay put and tell
      // the user to go and read their mail.
      if (result && result.status === "verification_sent") {
        showVerificationNotice(form, result.email);
        return;
      }
      window.location.href = nextTarget();
    } catch (error) {
      if (error instanceof ApiError && error.code === "email_unverified") {
        showResendPrompt(data.email);
      } else {
        showError(error instanceof ApiError ? error.message : "אירעה שגיאה, נסו שוב");
      }
      button.disabled = false;
      button.textContent = original;
    }
  });
}

/** Login refused because the address isn't confirmed: offer to resend. */
function showResendPrompt(email) {
  showError("יש לאמת את כתובת האימייל לפני הכניסה.");
  const prompt = $("#resend-prompt");
  if (!prompt) return;
  prompt.hidden = false;
  const button = $("#resend-btn");
  if (!button || button.dataset.wired) return;
  button.dataset.wired = "1";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api.resendVerification(String(email || "").trim());
      button.textContent = "נשלח קישור חדש";
    } catch (err) {
      button.disabled = false;
    }
  });
}

wire($("#login-form"), (data) => api.login(data.email.trim(), data.password));

wire($("#signup-form"), (data) => {
  if ((data.password || "").length < 10) {
    throw new ApiError("weak_password", "הסיסמה חייבת להכיל לפחות 10 תווים", {}, 400);
  }
  return api.signup(data.email.trim(), data.password, (data.invite_code || "").trim());
});
