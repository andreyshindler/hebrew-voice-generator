/* The single place that talks to the server.
   Adds the CSRF header, unwraps the error envelope, and translates the
   server's stable error codes into Hebrew. */

/* The URL prefix the app is served under ("" at the root, "/voice-gen" behind
   a subpath). Set by the server on <html data-base>, so it works on the login
   page too, which has no bootstrap payload. */
export const BASE = document.documentElement.dataset.base || "";

/** Turn an app-absolute path into a real URL: "/api/me" -> "/voice-gen/api/me". */
export const url = (path) => `${BASE}${path}`;

export class ApiError extends Error {
  constructor(code, message, detail, status) {
    super(message);
    this.code = code;
    this.detail = detail || {};
    this.status = status;
  }
}

/* The server sends machine-readable codes and never carries translations;
   the Hebrew lives here. */
const MESSAGES = {
  auth_required: "יש להתחבר כדי להמשיך",
  invalid_credentials: "אימייל או סיסמה שגויים",
  account_disabled: "החשבון מושבת",
  account_locked: "החשבון ננעל זמנית לאחר יותר מדי ניסיונות. נסו שוב בעוד כמה דקות",
  invalid_invite: "קוד ההזמנה אינו תקין",
  email_taken: "כתובת האימייל כבר רשומה",
  email_unverified: "יש לאמת את כתובת האימייל. שלחנו אליכם קישור בהרשמה",
  email_send_failed: "לא הצלחנו לשלוח את מייל האימות. נסו שוב בעוד רגע",
  invalid_token: "הקישור אינו תקף או שפג תוקפו",
  signup_disabled: "ההרשמה סגורה כרגע",
  csrf_failed: "פג תוקף החיבור. רעננו את הדף ונסו שוב",
  cross_origin_blocked: "הבקשה נחסמה מטעמי אבטחה",
  text_too_long: "הטקסט ארוך מהמותר",
  empty_after_preparation: "אין טקסט להקראה",
  unknown_voice: "הקול שנבחר אינו זמין",
  validation_failed: "אחד השדות אינו תקין",
  quota_exceeded: "נגמרה המכסה היומית. היא מתאפסת בחצות",
  rate_limited: "יותר מדי בקשות. המתינו רגע ונסו שוב",
  already_running: "כבר רצה יצירה אחת. המתינו שתסתיים",
  server_busy: "השרת עמוס כרגע. נסו שוב בעוד כמה שניות",
  synthesis_timeout: "היצירה ארכה יותר מדי. נסו טקסט קצר יותר",
  tts_upstream_failed: "שירות ההקראה אינו זמין כרגע. נסו שוב בעוד רגע",
  not_found: "הפריט לא נמצא",
  internal_error: "אירעה שגיאה בשרת",
};

export function messageFor(code, fallback) {
  return MESSAGES[code] || fallback || "אירעה שגיאה";
}

function readCookie(name) {
  const match = document.cookie.match(
    new RegExp("(?:^|; )" + name.replace(/([.$?*|{}()[\]\\/+^])/g, "\\$1") + "=([^;]*)")
  );
  return match ? decodeURIComponent(match[1]) : "";
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export async function request(path, { method = "GET", body, signal } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (UNSAFE.has(method)) headers["X-CSRF-Token"] = readCookie("hv_csrf");

  const response = await fetch(url(path), {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });

  if (response.status === 204) return null;

  let payload = null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    payload = await response.json().catch(() => null);
  }

  if (!response.ok) {
    const error = (payload && payload.error) || {};
    // An expired session should take the user to the login page, not leave
    // them staring at a dead button. `path` is still the unprefixed form here,
    // which is what this test wants.
    if (response.status === 401 && !path.startsWith("/api/auth/")) {
      window.location.href = url("/login");
    }
    throw new ApiError(
      error.code || `http_${response.status}`,
      messageFor(error.code, error.message),
      error.detail,
      response.status
    );
  }
  return payload;
}

export const api = {
  me: () => request("/api/auth/me"),
  login: (email, password) =>
    request("/api/auth/login", { method: "POST", body: { email, password } }),
  signup: (email, password, invite_code) =>
    request("/api/auth/signup", { method: "POST", body: { email, password, invite_code } }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
  resendVerification: (email) =>
    request("/api/auth/resend-verification", { method: "POST", body: { email } }),
  preview: (body, signal) => request("/api/preview", { method: "POST", body, signal }),
  synthesize: (body) => request("/api/synthesize", { method: "POST", body }),
  history: (params = {}) => {
    const query = new URLSearchParams();
    if (params.limit) query.set("limit", params.limit);
    if (params.before) query.set("before", params.before);
    const suffix = query.toString() ? `?${query}` : "";
    return request(`/api/generations${suffix}`);
  },
  generation: (id) => request(`/api/generations/${id}`),
  remove: (id) => request(`/api/generations/${id}`, { method: "DELETE" }),
};
