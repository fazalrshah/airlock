# 🛬 Airlock — a deterministic egress gate for locked-down LLM agents

**Give an LLM agent internet access without letting it roam or exfiltrate.**

The usual options are bad: either you *prompt* the model "please don't leak data / only search Google"
(unenforceable — one clever prompt and it's gone), or you hand it a raw browser tool (it can navigate
anywhere and POST anything out). Airlock is the third option:

> The agent can **reason** and *ask* Airlock to look something up — but the guarantees live in
> **deterministic code the model cannot override.** The LLM literally cannot widen the aperture.

Airlock is a small **MCP server** that enforces:

| Guarantee | How |
|---|---|
| **Single egress** | Only ever navigates to ONE configured domain. Result URLs are read, never followed. |
| **Fail-closed** | If the browser/egress is down, **no outbound call is made** — period. |
| **One-way** | Only the sanitized query goes out; raw results come back; nothing else leaves. |
| **Status-only audit** | Logs `{request_id, status, ts}` — never the query text or result bodies. |

And it's **self-healing**: it drives a real, logged-in Chrome over CDP (survives the bot-walls headless
scrapers hit), **relaunches that Chrome if it dies**, runs **stateless** (a restart never strands the
client), and ships with a **launchd** plist for crash/reboot resilience.

---

## The threat model it actually addresses

An autonomous agent with internet access is an exfiltration risk: prompt-injected content, a confused
plan, or a jailbreak can turn "research this" into "POST our secrets to attacker.com." Airlock makes that
**structurally impossible** for the agent — the only thing that ever leaves is a query string to one
pre-approved domain, and the only thing that comes back is read-only result text. The locked-down agent
holds no credentials and has no other network path.

```
  locked-down LLM agent ──(asks)──► Airlock (deterministic MCP) ──► ONE allowed domain
        ▲                                  │
        └────────── raw results ◄──────────┘   (one-way; status-only audit)
```

## Quickstart

```bash
pip install -r requirements.txt
playwright install chromium    # or use your system Chrome (default)

# 1. Launch the dedicated, logged-in Chrome ONCE (sign in to whatever you need, then leave it):
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 --user-data-dir="$HOME/airlock-chrome-profile"

# 2. Run Airlock
python airlock.py
```

Register it with your MCP client (e.g. an OpenClaw/Claude agent) as a streamable-http server at
`http://host.docker.internal:9100/mcp/` (or `localhost` if not containerized), and scope it to the one
agent allowed to do research. Tools: `gate_status`, `search`, `fetch_page`.

## Configuration (env)

| Var | Default | Notes |
|---|---|---|
| `AIRLOCK_EGRESS_DOMAIN` | `https://www.google.com` | the **only** domain Airlock will navigate to |
| `AIRLOCK_SEARCH_PATH` | `/search?q={q}` | how a query is run on that domain |
| `AIRLOCK_RESULT_SELECTOR` | `div#search a:has(h3)` | what to read back |
| `AIRLOCK_ALLOW_FETCH` | `1` | allow `fetch_page` (set `0` for search-only) |
| `AIRLOCK_MAX_RESULTS` | `10` | |
| `CHROME_CDP_URL` | `http://localhost:9222` | the Chrome Airlock drives |
| `CHROME_PROFILE` | `~/airlock-chrome-profile` | dedicated logged-in profile |

See [`.env.example`](.env.example). Always-on via launchd: [`com.airlock.gate.plist`](com.airlock.gate.plist).

## Hard-won lessons baked in

- **Stateless MCP is mandatory.** Streamable-HTTP MCP ties sessions to the process; restart the gate and
  the client dies with `Session not found` (-32600). Fix: `mcp.run(..., stateless_http=True)` — and pass
  it to `run()`, **not** the `FastMCP(...)` constructor (constructor kwargs are deprecated and crash older
  versions — a nasty silent footgun).
- **Self-heal your dependency.** A gate whose browser quit is just "down." Airlock relaunches its own
  Chrome (`subprocess.Popen(start_new_session=True)`) so a closed window recovers with no human.
- **Drive a real logged-in browser, not headless.** Real profile over CDP sails past the bot-walls that
  block headless scrapers — and keeps you logged in to whatever you need.
- **Put guarantees in code, not prompts.** "Don't exfiltrate" in a system prompt is a suggestion. A single
  hard-coded egress domain + fail-closed checks is a guarantee.

## Built by

Built by **[KodeKing](https://www.kodeking.net)** · author **[Fazal Shah](https://www.fazalshah.com)**.
We build local, private, multi-agent AI systems for teams who can't send their data to the cloud.
Issues and PRs welcome.

## License

MIT — see [LICENSE](LICENSE).
