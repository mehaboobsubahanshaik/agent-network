// Small fetch wrapper for the AgentNet REST API.

export class ApiError extends Error {
  constructor(status, data) {
    super(errorText({ status, data }));
    this.status = status;
    this.data = data;
  }
}

export async function api(path, { method = "GET", body, token } = {}) {
  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  let response;
  try {
    response = await fetch(`/api/v1${path}`, { method, headers, body: body ? JSON.stringify(body) : undefined });
  } catch {
    throw new ApiError(0, { detail: "Cannot reach the API. Is the backend running?" });
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    /* empty body (204) */
  }
  if (!response.ok) throw new ApiError(response.status, data);
  return data;
}

// FastAPI errors come in a few shapes; turn any of them into one readable line.
export function errorText(error) {
  const detail = error?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => `${d.loc?.slice(-1)[0] ?? "field"}: ${d.msg}`).join("; ");
  if (detail?.message) return [detail.message, ...(detail.reasons ?? [])].join(" ");
  return error?.status ? `Request failed (HTTP ${error.status}).` : "Request failed.";
}

// "prompt_injection: the text tries to..." -> { code: "Prompt injection", text: "the text tries to..." }
export function splitReason(reason) {
  const index = reason.indexOf(":");
  if (index === -1) return { code: "", text: reason };
  const code = reason.slice(0, index).replaceAll("_", " ");
  return { code: code.charAt(0).toUpperCase() + code.slice(1), text: reason.slice(index + 1).trim() };
}

export function timeAgo(iso) {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export const isWaiting = (log) => log.decision === "notified" && (log.status === "NOTIFIED" || log.status === "FAILED");
