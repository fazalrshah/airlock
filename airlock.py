"""
Airlock — a deterministic, self-healing egress gate (MCP server) for locked-down LLM agents.

Give an LLM agent internet access WITHOUT letting it roam or exfiltrate. The agent can reason and *ask*
Airlock to look something up, but the hard guarantees live in deterministic code it cannot override:

  * SINGLE EGRESS   — only ever navigates to ONE configured domain (default: a search engine).
  * FAIL-CLOSED     — if the browser/egress is unreachable, NO outbound call is made.
  * ONE-WAY         — only the sanitized query goes out; raw results come back; nothing else leaves.
  * STATUS-ONLY AUDIT — logs {request_id, status, ts}; never the query text or result bodies.

It drives a REAL, logged-in Chrome over CDP (so it survives the bot-walls that headless scrapers hit),
and it SELF-HEALS: if that Chrome is down, Airlock relaunches it; the MCP runs stateless so a restart
never strands the client; pair with the included launchd plist for crash/reboot resilience.

Exposed MCP tools: gate_status, search, fetch_page.
"""
import os, time, json, subprocess
from fastmcp import FastMCP
from playwright.sync_api import sync_playwright

# --- the entire policy surface (deterministic; the LLM cannot change any of this) ---------------
EGRESS_DOMAIN  = os.environ.get("AIRLOCK_EGRESS_DOMAIN", "https://www.google.com")  # the ONLY allowed host
SEARCH_PATH    = os.environ.get("AIRLOCK_SEARCH_PATH", "/search?q={q}")             # how to run a query
RESULT_SELECTOR= os.environ.get("AIRLOCK_RESULT_SELECTOR", "div#search a:has(h3)")  # what to read back
MAX_RESULTS    = int(os.environ.get("AIRLOCK_MAX_RESULTS", "10"))
FETCH_MAX_CHARS= int(os.environ.get("AIRLOCK_FETCH_MAX_CHARS", "15000"))
ALLOW_FETCH    = os.environ.get("AIRLOCK_ALLOW_FETCH", "1") not in ("0", "false", "no")
NAV_TIMEOUT_MS = int(os.environ.get("AIRLOCK_NAV_TIMEOUT_MS", "20000"))
AUDIT_LOG      = os.environ.get("AIRLOCK_AUDIT_LOG", os.path.expanduser("~/airlock_status.log"))
PORT           = int(os.environ.get("AIRLOCK_PORT", "9100"))

# --- the browser Airlock drives (a real, logged-in profile over CDP) ----------------------------
CHROME_CDP_URL    = os.environ.get("CHROME_CDP_URL", "http://localhost:9222")
CHROME_BIN        = os.environ.get("CHROME_BIN", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CHROME_PROFILE    = os.environ.get("CHROME_PROFILE", os.path.expanduser("~/airlock-chrome-profile"))
CHROME_DEBUG_PORT = os.environ.get("CHROME_DEBUG_PORT", "9222")
CHROME_LAUNCH_WAIT_S = int(os.environ.get("CHROME_LAUNCH_WAIT_S", "12"))

mcp = FastMCP("airlock")

def _now(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def _audit(request_id: str, status: str) -> None:
    # STATUS ONLY — never persist query text or result bodies.
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps({"request_id": request_id, "status": status, "ts": _now()}) + "\n")
    except Exception:
        pass

def _chrome_reachable(timeout_ms: int = 3000) -> bool:
    try:
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(CHROME_CDP_URL, timeout=timeout_ms)
            b.close()
        return True
    except Exception:
        return False

def _ensure_chrome() -> bool:
    """Self-heal: if the dedicated Chrome (CDP) is down, relaunch it with its logged-in profile and the
    debug port, then wait for CDP. Detached so it survives an Airlock restart."""
    if _chrome_reachable():
        return True
    if not os.path.exists(CHROME_BIN):
        return False
    try:
        subprocess.Popen(
            [CHROME_BIN, f"--remote-debugging-port={CHROME_DEBUG_PORT}",
             f"--user-data-dir={CHROME_PROFILE}", "--no-first-run",
             "--no-default-browser-check", "--restore-last-session=false"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        return False
    deadline = time.time() + CHROME_LAUNCH_WAIT_S
    while time.time() < deadline:
        if _chrome_reachable(timeout_ms=1500):
            return True
        time.sleep(1)
    return False


@mcp.tool
def gate_status() -> dict:
    """Is the egress aperture (the dedicated Chrome) reachable? Auto-launches it if down. This is the
    G1 precondition every outbound call checks first."""
    return {"status": "up" if _ensure_chrome() else "down", "egress_domain": EGRESS_DOMAIN, "ts": _now()}


@mcp.tool
def search(query: str, request_id: str) -> dict:
    """Run ONE query against the single allowed egress domain and return RAW results one-way.
    Fail-closed: if the gate is down, NO outbound call is made."""
    if not request_id:
        return {"request_id": request_id, "results": [], "status": "bad_request", "ts": _now()}
    if gate_status()["status"] != "up":
        _audit(request_id, "gate_offline")
        return {"request_id": request_id, "results": [], "status": "gate_offline", "ts": _now()}
    _audit(request_id, "up")
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CHROME_CDP_URL)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            url = EGRESS_DOMAIN.rstrip("/") + SEARCH_PATH.format(q=query)  # SINGLE EGRESS only
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            results = []
            for a in page.query_selector_all(RESULT_SELECTOR)[:MAX_RESULTS]:
                h3 = a.query_selector("h3")
                results.append({"title": h3.inner_text() if h3 else "", "url": a.get_attribute("href") or ""})
            page.close()
            return {"request_id": request_id, "results": results, "status": "ok", "ts": _now()}
    except Exception:
        _audit(request_id, "provider_error")
        return {"request_id": request_id, "results": [], "status": "provider_error", "ts": _now()}


@mcp.tool
def fetch_page(url: str, request_id: str) -> dict:
    """Fetch the rendered TEXT of a public page (read-only, one-way). Disabled unless AIRLOCK_ALLOW_FETCH=1.
    Fail-closed if the gate is down; only http(s) URLs."""
    if not ALLOW_FETCH:
        return {"request_id": request_id, "url": url, "content": "", "status": "fetch_disabled", "ts": _now()}
    if not request_id or not (url.startswith("http://") or url.startswith("https://")):
        return {"request_id": request_id, "url": url, "content": "", "status": "bad_request", "ts": _now()}
    if gate_status()["status"] != "up":
        _audit(request_id, "gate_offline")
        return {"request_id": request_id, "url": url, "content": "", "status": "gate_offline", "ts": _now()}
    _audit(request_id, "up")
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CHROME_CDP_URL)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            try:
                text = page.inner_text("body")
            except Exception:
                text = page.content()
            page.close()
            return {"request_id": request_id, "url": url, "content": text[:FETCH_MAX_CHARS],
                    "status": "ok", "ts": _now()}
    except Exception:
        _audit(request_id, "provider_error")
        return {"request_id": request_id, "url": url, "content": "", "status": "provider_error", "ts": _now()}


if __name__ == "__main__":
    # stateless_http=True: each request is self-contained, so an Airlock restart never strands the
    # MCP client with a "Session not found" error. (Pass to run(), NOT the FastMCP constructor.)
    print(f"[airlock] egress={EGRESS_DOMAIN}  port={PORT}  fetch={'on' if ALLOW_FETCH else 'off'}", flush=True)
    mcp.run(transport="http", host="0.0.0.0", port=PORT, stateless_http=True)
