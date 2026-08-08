/* The resend form on the verification landing page. */

import { api } from "./api.js";
import { $ } from "./ui.js";

const form = $("#resend-form");

if (form) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = $("#form-error");
    const ok = $("#form-ok");
    error.hidden = true;
    ok.hidden = true;

    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "שולח…";

    const email = new FormData(form).get("email");
    try {
      await api.resendVerification(String(email || "").trim());
      // The server answers the same way whether or not the address exists, so
      // this message deliberately doesn't confirm it does.
      ok.hidden = false;
      form.querySelector("input[name=email]").value = "";
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  });
}
