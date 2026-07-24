"""Self-contained monitoring dashboard served at /dashboard.

Vanilla JS polls /api/stats every couple of seconds and renders summary cards,
the active session→thread map, and a ring buffer of recent requests. No build
step, no external assets.
"""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Hyperagent Proxy · Dashboard</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
         background: #0b0b0d; color: #e4e4e7; padding: 24px 5%; }
  h1 { font-size: 1.4rem; margin: 0 0 2px; color: #fff; }
  .sub { color: #71717a; font-size: .85rem; margin-bottom: 20px; }
  .dot { width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:6px; }
  .ok { background:#4ade80; } .warn { background:#fbbf24; } .err { background:#f87171; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:24px; }
  .card { background:#18181b; border:1px solid #27272a; border-radius:12px; padding:14px 16px; }
  .card .label { color:#a1a1aa; font-size:.72rem; text-transform:uppercase; letter-spacing:.04em; }
  .card .value { font-size:1.5rem; font-weight:700; color:#fff; margin-top:4px; }
  h2 { font-size:.95rem; color:#d4d4d8; margin:22px 0 8px; }
  table { width:100%; border-collapse:collapse; font-size:.82rem; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid #1f1f23; white-space:nowrap;
           overflow:hidden; text-overflow:ellipsis; max-width:280px; }
  th { color:#71717a; font-weight:600; text-transform:uppercase; font-size:.68rem; letter-spacing:.04em; }
  tr:hover td { background:#141417; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color:#a5b4fc; }
  .pill { padding:2px 8px; border-radius:9999px; font-size:.7rem; font-weight:600; }
  .pill.ok { background:#14532d; color:#4ade80; } .pill.err { background:#450a0a; color:#f87171; }
  .pill.running { background:#1e3a5f; color:#60a5fa; }
  .pill.reuse { background:#3b2f0a; color:#fbbf24; } .pill.new { background:#1f2937; color:#9ca3af; }
  .empty { color:#52525b; font-style:italic; padding:12px 10px; }
  footer { margin-top:24px; color:#3f3f46; font-size:.72rem; }
</style>
</head>
<body>
  <h1>Hyperagent Local Proxy</h1>
  <div class="sub"><span id="statusDot" class="dot warn"></span><span id="statusText">connecting…</span> · auto-refresh 2s</div>

  <div class="cards" id="cards"></div>

  <h2>Active sessions → threads</h2>
  <table id="sessions"><thead><tr>
    <th>Session key</th><th>Thread</th><th>Model</th><th>Msgs</th><th>Idle</th><th>Age</th>
  </tr></thead><tbody><tr><td class="empty" colspan="6">loading…</td></tr></tbody></table>

  <h2>Recent requests</h2>
  <table id="requests"><thead><tr>
    <th>Request</th><th>Model</th><th>Status</th><th>Thread</th><th>Reuse</th><th>Latency</th><th>Tokens (p/c)</th>
  </tr></thead><tbody><tr><td class="empty" colspan="7">loading…</td></tr></tbody></table>

  <footer>Read-only view. Data is in-memory and resets when the proxy restarts.</footer>

<script>
const fmtDur = s => s==null ? "—" : (s<60 ? s.toFixed(0)+"s" : (s/60).toFixed(1)+"m");
const short = (t,n=10) => !t ? "—" : (t.length>n ? t.slice(0,n)+"…" : t);
function card(label, value){ return `<div class="card"><div class="label">${label}</div><div class="value">${value}</div></div>`; }

async function tick(){
  try {
    const r = await fetch("/api/stats", {cache:"no-store"});
    if(!r.ok) throw new Error("HTTP "+r.status);
    const d = await r.json();
    document.getElementById("statusDot").className = "dot ok";
    document.getElementById("statusText").textContent = "online";

    const s = d.summary || {};
    document.getElementById("cards").innerHTML =
      card("Uptime", fmtDur(s.uptime_seconds)) +
      card("Requests", s.requests_total ?? 0) +
      card("Errors", s.requests_error ?? 0) +
      card("Active streams", s.streams_active ?? 0) +
      card("Sessions", (d.sessions||[]).length) +
      card("Tokens p/c", (s.tokens_prompt ?? 0)+" / "+(s.tokens_completion ?? 0));

    const sb = document.querySelector("#sessions tbody");
    const sess = d.sessions || [];
    sb.innerHTML = sess.length ? sess.map(x => `<tr>
      <td class="mono" title="${x.key}">${short(x.key,26)}</td>
      <td class="mono" title="${x.thread_id}">${short(x.thread_id,14)}</td>
      <td>${x.model||"—"}</td><td>${x.message_count ?? 0}</td>
      <td>${fmtDur(x.idle_seconds)}</td><td>${fmtDur(x.age_seconds)}</td></tr>`).join("")
      : `<tr><td class="empty" colspan="6">No active sessions yet.</td></tr>`;

    const rb = document.querySelector("#requests tbody");
    const reqs = d.recent || [];
    rb.innerHTML = reqs.length ? reqs.map(x => {
      const st = x.status || "running";
      const reuse = x.reused_thread===true ? `<span class="pill reuse">reuse</span>`
                  : x.reused_thread===false ? `<span class="pill new">new</span>` : "—";
      return `<tr>
        <td class="mono">${short(x.id,14)}</td>
        <td>${x.model||"—"}</td>
        <td><span class="pill ${st}">${st}</span></td>
        <td class="mono" title="${x.thread_id||""}">${short(x.thread_id,12)}</td>
        <td>${reuse}</td>
        <td>${x.latency_ms!=null ? x.latency_ms+"ms" : "—"}</td>
        <td>${(x.prompt_tokens ?? "—")} / ${(x.completion_tokens ?? "—")}</td></tr>`;
    }).join("") : `<tr><td class="empty" colspan="7">No requests yet.</td></tr>`;
  } catch(e){
    document.getElementById("statusDot").className = "dot err";
    document.getElementById("statusText").textContent = "offline — "+e.message;
  }
}
tick(); setInterval(tick, 2000);
</script>
</body>
</html>
"""
