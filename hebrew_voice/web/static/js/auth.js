/* Login and signup. Both pages post JSON like the rest of the API, so there
   are no HTML form submissions anywhere and no multipart parser on the server. */

import { api, ApiError } from "./api.js";
import { $ } from "./ui.js";

const errorBox = $("#form-error");

function showError(message) {
  if (!errorBox) return;
  errorBox.textContent = message;
  errorBox.hidden = false;
}

function nextTarget() {
  const next = new URLSearchParams(window.location.search).get("next");
  // Only ever follow a same-site path, never an absolute URL from the query.
  return next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
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
      await submit(data);
      window.location.href = nextTarget();
    } catch (error) {
      showError(error instanceof ApiError ? error.message : "אירעה שגיאה, נסו שוב");
      button.disabled = false;
      button.textContent = original;
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
