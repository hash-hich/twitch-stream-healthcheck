/**
 * app.js — Twitch Stream Monitor dashboard client.
 *
 * Connects to /ws/metrics, receives MonitorSnapshot JSON every second,
 * and updates the DOM and Chart.js datasets.
 */

"use strict";

const MAX_POINTS = 60;

// ─── Chart setup ──────────────────────────────────────────────────────────

function makeEmptyData() {
  return Array(MAX_POINTS).fill(null);
}

function makeEmptyLabels() {
  return Array(MAX_POINTS).fill("");
}

const CHART_DEFAULTS = {
  type: "line",
  options: {
    animation: false,
    responsive: true,
    maintainAspectRatio: false,
    interaction: { intersect: false, mode: "index" },
    scales: {
      x: { display: false },
      y: {
        grid: { color: "rgba(148,163,184,0.07)" },
        ticks: { color: "#64748b", font: { size: 11 } },
        border: { display: false },
      },
    },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: "rgba(15,23,42,0.95)",
        borderColor: "rgba(148,163,184,0.2)",
        borderWidth: 1,
        titleColor: "#94a3b8",
        bodyColor: "#e2e8f0",
        padding: 10,
      },
    },
  },
};

const latencyChart = new Chart(
  document.getElementById("chart-latency").getContext("2d"),
  {
    ...structuredClone(CHART_DEFAULTS),
    data: {
      labels: makeEmptyLabels(),
      datasets: [
        {
          data: makeEmptyData(),
          borderColor: "#8b5cf6",
          backgroundColor: "rgba(139,92,246,0.08)",
          fill: true,
          tension: 0.35,
          pointRadius: 0,
          borderWidth: 2,
        },
      ],
    },
  }
);

const bitrateChart = new Chart(
  document.getElementById("chart-bitrate").getContext("2d"),
  {
    ...structuredClone(CHART_DEFAULTS),
    data: {
      labels: makeEmptyLabels(),
      datasets: [
        {
          data: makeEmptyData(),
          borderColor: "#10b981",
          backgroundColor: "rgba(16,185,129,0.08)",
          fill: true,
          tension: 0.35,
          pointRadius: 0,
          borderWidth: 2,
        },
      ],
    },
  }
);

// ─── Status helpers ────────────────────────────────────────────────────────

const STATUS_STYLES = {
  healthy:  { pill: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30", dot: "bg-emerald-400" },
  degraded: { pill: "bg-yellow-500/15 text-yellow-400 border-yellow-500/30",   dot: "bg-yellow-400"  },
  down:     { pill: "bg-red-500/15 text-red-400 border-red-500/30",             dot: "bg-red-400"     },
};

const SEVERITY_STYLES = {
  info:     "text-sky-400",
  warning:  "text-yellow-400",
  critical: "text-red-400",
};

function capitalize(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// ─── DOM updaters ──────────────────────────────────────────────────────────

function updateStatus(snap) {
  const pill   = document.getElementById("status-pill");
  const styles = STATUS_STYLES[snap.status] || STATUS_STYLES.down;

  pill.className =
    `inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold border ${styles.pill}`;
  pill.innerHTML =
    `<span class="dot ${styles.dot}"></span>${capitalize(snap.status)}`;
}

function updateCards(snap) {
  // Uptime
  const uptime = Math.round(snap.uptime_seconds);
  const h = Math.floor(uptime / 3600);
  const m = Math.floor((uptime % 3600) / 60);
  const s = uptime % 60;
  document.getElementById("card-uptime").textContent =
    h > 0
      ? `${h}h ${String(m).padStart(2, "0")}m`
      : `${m}m ${String(s).padStart(2, "0")}s`;

  // Segments
  const total  = snap.segments_total;
  const failed = snap.segments_failed;
  document.getElementById("card-segments").textContent =
    `${total - failed} / ${total}`;
  const rate = total > 0 ? (((total - failed) / total) * 100).toFixed(1) : "—";
  document.getElementById("card-success-rate").textContent =
    `success rate ${rate}%`;

  // Latency
  document.getElementById("card-latency").textContent =
    snap.median_latency_ms > 0
      ? `${snap.median_latency_ms.toFixed(0)} ms`
      : "— ms";

  // Bitrate
  document.getElementById("card-bitrate").textContent =
    snap.effective_bitrate_bps != null
      ? `${(snap.effective_bitrate_bps / 1_000_000).toFixed(2)} Mbps`
      : "— Mbps";

  // Channel name (only needs setting once but harmless to repeat)
  document.getElementById("channel-name").textContent = snap.channel;
}

function pushPoint(chart, value) {
  chart.data.datasets[0].data.push(value);
  chart.data.datasets[0].data.shift();
  chart.data.labels.push("");
  chart.data.labels.shift();
  chart.update("none"); // skip animation for real-time feel
}

function updateCharts(snap) {
  pushPoint(latencyChart, snap.median_latency_ms || null);
  pushPoint(
    bitrateChart,
    snap.effective_bitrate_bps != null
      ? snap.effective_bitrate_bps / 1_000_000
      : null
  );
}

function updateIncidents(snap) {
  const incidents = snap.recent_incidents || [];
  const body      = document.getElementById("incidents-body");
  const countEl   = document.getElementById("incidents-count");

  countEl.textContent = `${incidents.length} incident${incidents.length !== 1 ? "s" : ""}`;

  if (incidents.length === 0) {
    body.innerHTML =
      `<tr><td colspan="4" class="py-6 text-center text-slate-600 text-xs">No incidents detected</td></tr>`;
    return;
  }

  const rows = incidents
    .slice(-10)
    .reverse()
    .map((inc) => {
      const ts      = new Date(inc.timestamp_utc).toLocaleTimeString();
      const sevCls  = SEVERITY_STYLES[inc.severity] || "text-slate-400";
      const typeTag = inc.type.replace(/_/g, " ");
      return `
        <tr class="border-b border-slate-800/60 hover:bg-slate-800/30 transition-colors">
          <td class="py-2.5 pr-4 text-xs text-slate-500 font-mono">${ts}</td>
          <td class="py-2.5 pr-4 text-xs text-slate-300">${typeTag}</td>
          <td class="py-2.5 pr-4 text-xs font-medium ${sevCls}">${capitalize(inc.severity)}</td>
          <td class="py-2.5 text-xs text-slate-400 truncate max-w-xs" title="${inc.message}">${inc.message}</td>
        </tr>`;
    })
    .join("");

  body.innerHTML = rows;
}

function updateTimestamp() {
  document.getElementById("last-updated").textContent =
    `Updated ${new Date().toLocaleTimeString()}`;
}

// ─── WebSocket connection ──────────────────────────────────────────────────

function connect() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const ws       = new WebSocket(`${protocol}://${location.host}/ws/metrics`);

  ws.addEventListener("open", () => {
    console.info("[monitor] WebSocket connected");
  });

  ws.addEventListener("message", (event) => {
    try {
      const snap = JSON.parse(event.data);
      updateStatus(snap);
      updateCards(snap);
      updateCharts(snap);
      updateIncidents(snap);
      updateTimestamp();
    } catch (err) {
      console.error("[monitor] Failed to parse snapshot:", err);
    }
  });

  ws.addEventListener("close", (event) => {
    console.warn(`[monitor] WebSocket closed (${event.code}), reconnecting in 2s…`);
    setTimeout(connect, 2000);
  });

  ws.addEventListener("error", (err) => {
    console.error("[monitor] WebSocket error:", err);
  });
}

connect();
