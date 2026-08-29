(function () {
  "use strict";

  const REAL_EVENT_ID = /^\d+-\d+$/;
  const COMPOSITE_EVENT_ID = /^e=(\d+-\d+|-);t=(\d+-\d+|-)$/;
  const TERMINAL_STATES = new Set(["done", "failed"]);
  const STALE_GRACE_MS = 120000;

  function isRealEventId(id) {
    return typeof id === "string" && (REAL_EVENT_ID.test(id) || COMPOSITE_EVENT_ID.test(id));
  }

  function parseFrames(buffer) {
    const frames = [];
    let cursor = 0;

    while (true) {
      const next = buffer.indexOf("\n\n", cursor);
      if (next === -1) {
        break;
      }
      const raw = buffer.slice(cursor, next);
      cursor = next + 2;

      const frame = {};
      const dataLines = [];
      for (const originalLine of raw.split("\n")) {
        const line = originalLine.endsWith("\r") ? originalLine.slice(0, -1) : originalLine;
        if (!line || line.startsWith(":")) {
          continue;
        }
        const separator = line.indexOf(":");
        const field = separator === -1 ? line : line.slice(0, separator);
        let value = separator === -1 ? "" : line.slice(separator + 1);
        if (value.startsWith(" ")) {
          value = value.slice(1);
        }
        if (field === "data") {
          dataLines.push(value);
        } else if (field === "id" || field === "event") {
          frame[field] = value;
        }
      }
      if (Object.keys(frame).length || dataLines.length) {
        frame.data = dataLines.join("\n");
        frames.push(frame);
      }
    }

    return { frames, tail: buffer.slice(cursor) };
  }

  function entryData(entry) {
    const raw = entry && Object.prototype.hasOwnProperty.call(entry, "data") ? entry.data : {};
    if (raw && typeof raw === "object") {
      return raw;
    }
    try {
      return JSON.parse(raw || "{}");
    } catch (_err) {
      return {};
    }
  }

  function parseTs(value) {
    if (!value) {
      return null;
    }
    const ts = Date.parse(value);
    return Number.isNaN(ts) ? null : ts;
  }

  function isStale(state, now) {
    if (!state || state.state !== "running") {
      return false;
    }
    const lastEventTs = parseTs(state.last_event_ts);
    if (lastEventTs === null) {
      return false;
    }
    return (now || Date.now()) - lastEventTs > STALE_GRACE_MS;
  }

// Do NOT call from the live path. It expects raw redis fields (event_type/sent_at); a live
// frame is already reduced server-side and carries last_event/last_event_ts instead — none of
// this function's event_type branches would ever fire, and state would never be set at all
// (verified: feeding it real already-reduced frames leaves `state` absent from every output,
// not frozen at a prior value — the seat would render "unknown" forever). Kept unwired, exported
// only for the JS/Python parity contract test (test_appjs_parser_and_reducer_match_visibility_
// reducer), which asserts this function stays byte-identical to Python's _reduce_seat over a
// shared fixture.
function reduceSeat(state, entry) {
    const reduced = Object.assign({}, state || {});
    if (!entry || Object.keys(entry).length === 0) {
      if (isStale(reduced)) {
        reduced.state = "stale";
      }
      return reduced;
    }

    const eventType = entry.event_type;
    const data = entryData(entry);
    reduced.run_id = entry.run_id || reduced.run_id;
    reduced.task_id = entry.task_id || reduced.task_id;
    reduced.seat_id = entry.seat_id || reduced.seat_id;
    reduced.orchestrator = entry.orchestrator || reduced.orchestrator;
    reduced.last_event_ts = entry.sent_at || reduced.last_event_ts;
    reduced.last_event = eventType || reduced.last_event;

    if (eventType === "task_started" || eventType === "task_continuing") {
      reduced.state = "running";
    } else if (eventType === "task_finished") {
      reduced.state = data.ok === false ? "failed" : "done";
    } else if (eventType === "vote") {
      reduced.voted = true;
      if (Object.prototype.hasOwnProperty.call(data, "stance")) {
        reduced.stance = data.stance;
      }
      if (!TERMINAL_STATES.has(reduced.state)) {
        reduced.state = "voted";
      }
    }

    if (isStale(reduced)) {
      reduced.state = "stale";
    }
    return reduced;
  }

  function authHeaders() {
    const token = localStorage.getItem("token") || localStorage.token || "";
    return token ? { Authorization: "Bearer " + token } : {};
  }

  function formatJson(data) {
    try {
      return JSON.stringify(data, null, 2);
    } catch (_err) {
      return String(data);
    }
  }

  function toInt(value) {
    const parsed = parseInt(value, 10);
    return Number.isNaN(parsed) ? 0 : parsed;
  }

  function timelinePrefix(data, options) {
    const escapeContent = options && options.escapeContent;
    const showTimestamps = options && options.showTimestamps;
    const showLabels = options && options.showLabels;
    const esc = (value) => (escapeContent ? escapeHtml(value) : value);
    return [
      showTimestamps ? esc(data.ts || data.sent_at || "") : "",
      showLabels ? esc(data.source || "event") : "",
      showLabels ? esc(data.kind || data.event_type || "") : "",
    ].filter(Boolean).join(" ");
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));
  }

  function formatTimelineEvent(data, options) {
    const escapeContent = options && options.escapeContent;
    const prefix = timelinePrefix(data || {}, options);
    if (!data || data.source !== "transcript") {
      return prefix;
    }
    const kind = data.kind || data.event_type || "";
    const content = escapeContent ? escapeHtml(data.content || "") : (data.content || "");
    const meta = data.meta || {};
    const toolName = escapeContent ? escapeHtml(data.tool_name || "") : (data.tool_name || "");
    let detail = "";
    if (toolName === "apply_patch" && meta.file) {
      const fileName = escapeContent ? escapeHtml(meta.file) : meta.file;
      const added = escapeContent ? escapeHtml(toInt(meta.added)) : toInt(meta.added);
      const removed = escapeContent ? escapeHtml(toInt(meta.removed)) : toInt(meta.removed);
      detail = "edited `" + fileName + "` +" + added + "/-" + removed;
      if (content) {
        detail += "\n<details><summary>diff</summary>\n" + content + "\n</details>";
      }
    } else if (kind === "model_thinking") {
      detail = "<details><summary>thinking</summary>";
      if (content) {
        detail += "\n" + content;
      }
      detail += "\n</details>";
    } else if (toolName || ["command_started", "command_finished", "command_output", "tool_call", "tool_output"].includes(kind)) {
      detail = [toolName, content].filter(Boolean).join("\n");
    } else {
      detail = content;
    }
    return [prefix, detail].filter(Boolean).join(" ");
  }

  function formatTimelineFrame(frame, options) {
    const event = (frame && frame.event) || "message";
    const data = frame && frame.data;
    if (data && data.source === "transcript") {
      return formatTimelineEvent(data, options);
    }
    return "[" + event + "] " + formatJson(data);
  }

  function appendTimelineFrame(timelineEl, frame, options) {
    const effectiveOptions = Object.assign(
      { escapeContent: true },
      options,
      { escapeContent: options && options.escapeContent === false ? false : true }
    );
    if (frame && frame.data && frame.data.source === "transcript" && timelineEl.insertAdjacentHTML) {
      const rendered = formatTimelineFrame(frame, effectiveOptions);
      const collapsed = collapseOutput(frame.data, rendered, Boolean(effectiveOptions.expanded));
      timelineEl.insertAdjacentHTML("beforeend", collapsed + "<br><br>");
    } else {
      timelineEl.textContent += formatTimelineFrame(frame, effectiveOptions) + "\n\n";
    }
    timelineEl.scrollTop = timelineEl.scrollHeight;
  }

  function streamSSE(url, onEvent) {
    let lastId = "";
    let stopped = false;
    let reconnectMs = 500;
    const decoder = new TextDecoder();
    const abortController = new AbortController();

    async function connect() {
      while (!stopped) {
        const headers = authHeaders();
        if (isRealEventId(lastId)) {
          headers["Last-Event-ID"] = lastId;
        }
        try {
          const response = await fetch(url, { headers, signal: abortController.signal });
          if (!response.ok) {
            const httpError = new Error("SSE " + response.status);
            httpError.status = response.status;
            throw httpError;
          }
          reconnectMs = 500;
          const reader = response.body.getReader();
          let buffer = "";
          while (!stopped) {
            const chunk = await reader.read();
            if (chunk.done) {
              break;
            }
            buffer += decoder.decode(chunk.value, { stream: true });
            const parsed = parseFrames(buffer);
            buffer = parsed.tail;
            for (const frame of parsed.frames) {
              if (isRealEventId(frame.id)) {
                lastId = frame.id;
              }
              let data = frame.data;
              try {
                data = JSON.parse(frame.data || "{}");
              } catch (_err) {
                data = frame.data;
              }
              onEvent(Object.assign({}, frame, { data }));
            }
          }
        } catch (err) {
          if (stopped || err.name === "AbortError") {
            return;
          }
          const status = typeof err.status === "number" ? err.status : null;
          onEvent({ event: "error", data: { message: err.message, status } });
          if (status !== null && status >= 400 && status < 500) {
            stopped = true;
            return;
          }
        }
        await new Promise((resolve) => setTimeout(resolve, reconnectMs));
        reconnectMs = Math.min(reconnectMs * 2, 5000);
      }
    }

    connect();
    return function stop() {
      stopped = true;
      abortController.abort();
    };
  }

  function ageLabel(seconds) {
    if (seconds < 60) return Math.floor(seconds) + "s";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m";
    return Math.floor(seconds / 3600) + "h";
  }

  function utcDayMonth(lastEventTs) {
    const ts = parseTs(lastEventTs);
    if (ts === null) {
      return null;
    }
    const date = new Date(ts);
    const dd = String(date.getUTCDate()).padStart(2, "0");
    const mm = String(date.getUTCMonth() + 1).padStart(2, "0");
    return dd + "/" + mm;
  }

  // No year component — eval_event_raw currently holds only short recent history.
  function seatAgeLabel(lastEventTs, seatSource, nowMs) {
    const ts = parseTs(lastEventTs);
    if (ts === null) {
      return "—";
    }
    const secs = ((nowMs == null ? Date.now() : nowMs) - ts) / 1000;
    if (seatSource === "history" && secs >= 86400) {
      const dm = utcDayMonth(lastEventTs);
      if (dm !== null) {
        return dm;
      }
    }
    return ageLabel(secs);
  }

  function isStaleHistoryGen(requestGen, currentGen) {
    return requestGen !== currentGen;
  }

  function isScrolledNearBottom(scrollTop, clientHeight, scrollHeight, threshold) {
    return scrollTop + clientHeight >= scrollHeight - threshold;
  }

  function shouldAutoFetchHistoryPage({ seatSource, historyHasMore, historyLoading, historyCursor, scrollHeight, clientHeight, fullWidth }) {
    if (seatSource !== "history" || !historyHasMore || historyLoading || !historyCursor || fullWidth) {
      return false;
    }
    return scrollHeight <= clientHeight;
  }

  function agentOf(seatId) {
    const lower = String(seatId || "").toLowerCase();
    if (lower.startsWith("cold-opus-")) {
      return "opus";
    }
    const head = lower.split("-")[0];
    if (!head) {
      return "?";
    }
    const labels = {
      codex: "codex", agy: "agy", pi: "pi", gemini: "gemini",
      cursor: "cursor", grok: "grok", kimi: "kimi", claude: "claude",
    };
    return labels[head] || head;
  }

  function isOrchestratorSeat(seat) {
    return agentOf((seat && seat.seat_id) || "") === "claude";
  }

  function visibleSeats(seatMap, statusFilter, agentFilter) {
    return Object.values(seatMap).filter((seat) => {
      const state = seat.state || "unknown";
      if (statusFilter !== "all" && state !== statusFilter) {
        return false;
      }
      const agent = agentOf(seat.seat_id || "");
      if (agentFilter !== "all" && agent !== agentFilter) {
        return false;
      }
      return true;
    });
  }

  function deriveAgentOptions(seats) {
    const set = new Set();
    Object.values(seats).forEach((seat) => {
      set.add(agentOf(seat.seat_id || ""));
    });
    return ["all", ...Array.from(set).sort()];
  }

  function isLifecycleNoise(data) {
    // Ported from Go's isLifecycleNoise (reduce.go) — eval/audit events tee'd into the same seat
    // SSE stream as real transcript content (source !== "transcript") are noise, never rendered.
    return !data || data.source !== "transcript";
  }

  function isTruncatableOutput(data) {
    if (!data) {
      return false;
    }
    if (data.tool_name === "apply_patch" && data.meta && data.meta.file) {
      return false; // apply_patch always renders <details>-wrapped HTML.
    }
    return data.kind === "command_output" || data.kind === "command_finished" || data.kind === "tool_output";
  }

  const MAX_COLLAPSED_OUTPUT_LINES = 6; // verbatim from Go's maxCollapsedOutputLines

  function collapseOutput(data, rendered, expanded) {
    if (expanded || !isTruncatableOutput(data)) {
      return rendered;
    }
    const lines = rendered.split("\n");
    if (lines.length <= MAX_COLLAPSED_OUTPUT_LINES) {
      return rendered;
    }
    const hidden = lines.length - MAX_COLLAPSED_OUTPUT_LINES;
    const kept = lines.slice(0, MAX_COLLAPSED_OUTPUT_LINES).join("\n");
    return kept + "\n" + '<span class="dim">… +' + hidden + " line(s) — click Expand output to see more</span>";
  }

  function readPersisted(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : raw;
    } catch (_err) {
      return fallback;
    }
  }

  function writePersisted(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (_err) {
      // best-effort — privacy mode / disabled storage; page stays fully functional without it
    }
  }

  function init() {
    const tokenInput = document.getElementById("token");
    const clearToken = document.getElementById("clear-token");
    const orchestratorSelect = document.getElementById("orchestrator");
    const seatsEl = document.getElementById("seats");
    const orchestratorSeatsEl = document.getElementById("seats-orchestrator");
    const timelineEl = document.getElementById("timeline");
    const modeLiveButton = document.getElementById("mode-live");
    const modeHistoryButton = document.getElementById("mode-history");
    const authBannerEl = document.getElementById("auth-banner");
    const seatPanelEl = document.getElementById("seat-panel");
    const historyStatusEl = document.getElementById("history-status");
    const statusFilterEl = document.getElementById("status-filter");
    const agentFilterEl = document.getElementById("agent-filter");
    const dateFilterEl = document.getElementById("date-filter");
    const dateFilterLabel = document.getElementById("date-filter-label");
    const filterCountEl = document.getElementById("filter-count");
    const toggleTimestampsButton = document.getElementById("toggle-timestamps");
    const toggleLabelsButton = document.getElementById("toggle-labels");
    const toggleExpandButton = document.getElementById("toggle-expand");
    const toggleFullWidthButton = document.getElementById("toggle-fullwidth");
    const copyTranscriptButton = document.getElementById("copy-transcript");
    const seats = {};
    let selectedTaskId = "";
    let selectedOrchestratorId = "";
    let stopOrchestratorStream = null;
    let stopSeatStream = null;
    let seatSource = "live";
    let historyCursor = null;
    let historyHasMore = false;
    let historyLoading = false;
    let historyGen = 0;
    let historyStatusText = "";
    let statusFilter = readPersisted("visStatusFilter", "all");
    let agentFilter = readPersisted("visAgentFilter", "all");
    let dateFilter = "";  // history-only, transient (YYYY-MM-DD); empty = no date filter
    let showTimestamps = readPersisted("visShowTimestamps", "false") === "true";
    let showLabels = readPersisted("visShowLabels", "false") === "true";
    let expanded = readPersisted("visExpanded", "false") === "true";
    let fullWidth = readPersisted("visFullWidth", "false") === "true";
    let transcriptBuffer = [];
    let lastSeatStreamError = null;
    let loginRedirectStarted = false;

    function showAuthBanner() {
      authBannerEl.textContent = "Session expired — redirecting to sign in…";
      authBannerEl.hidden = false;
      if (loginRedirectStarted || window.location.pathname === "/login") {
        return;
      }
      loginRedirectStarted = true;
      window.location.assign("/login");
    }

    function hideAuthBanner() {
      authBannerEl.hidden = true;
    }

    function updateModeButtons() {
      modeLiveButton.setAttribute("aria-pressed", seatSource === "live" ? "true" : "false");
      modeHistoryButton.setAttribute("aria-pressed", seatSource === "history" ? "true" : "false");
    }

    modeLiveButton.addEventListener("click", () => setSeatSource("live"));
    modeHistoryButton.addEventListener("click", () => setSeatSource("history"));

    copyTranscriptButton.addEventListener("click", () => {
      const text = timelineEl.textContent;
      if (!navigator.clipboard || !navigator.clipboard.writeText) {
        return;
      }
      navigator.clipboard.writeText(text).then(
        () => {
          const original = copyTranscriptButton.textContent;
          copyTranscriptButton.textContent = "Copied";
          setTimeout(() => { copyTranscriptButton.textContent = original; }, 1200);
        },
        () => {} // clipboard denied/unavailable — button stays as "Copy", no crash
      );
    });

    tokenInput.value = localStorage.getItem("token") || localStorage.token || "";
    tokenInput.addEventListener("change", () => {
      localStorage.setItem("token", tokenInput.value.trim());
      loadOrchestrators();
    });
    clearToken.addEventListener("click", () => {
      if (stopOrchestratorStream) {
        stopOrchestratorStream();
        stopOrchestratorStream = null;
      }
      if (stopSeatStream) {
        stopSeatStream();
        stopSeatStream = null;
      }
      Object.keys(seats).forEach((key) => delete seats[key]);
      selectedTaskId = "";
      selectedOrchestratorId = "";
      historyGen += 1;
      seatSource = "live";
      historyCursor = null;
      historyHasMore = false;
      historyLoading = false;
      historyStatusText = "";
      modeLiveButton.disabled = true;
      modeHistoryButton.disabled = true;
      updateModeButtons();
      localStorage.removeItem("token");
      localStorage.token = "";
      tokenInput.value = "";
      orchestratorSelect.replaceChildren();
      seatsEl.replaceChildren();
      orchestratorSeatsEl.replaceChildren();
      timelineEl.textContent = "";
      transcriptBuffer.length = 0;
      hideAuthBanner();
    });

    statusFilterEl.addEventListener("change", () => {
      statusFilter = statusFilterEl.value;
      writePersisted("visStatusFilter", statusFilter);
      renderSeats();
    });

    agentFilterEl.addEventListener("change", () => {
      agentFilter = agentFilterEl.value;
      writePersisted("visAgentFilter", agentFilter);
      renderSeats();
    });

    dateFilterEl.addEventListener("change", () => {
      dateFilter = dateFilterEl.value;
      if (seatSource === "history") {
        historyGen += 1;
        historyCursor = null;
        historyHasMore = false;
        historyStatusText = "";
        selectedTaskId = "";
        Object.keys(seats).forEach((key) => delete seats[key]);
        timelineEl.textContent = "";
        transcriptBuffer.length = 0;
        historyLoading = true;
        renderSeats();
        fetchHistoryPage({ append: false });
        return;
      }
      renderSeats();
    });

    function rerenderTranscript() {
      timelineEl.replaceChildren();
      for (const frame of transcriptBuffer) {
        const rendered = formatTimelineFrame(frame, { escapeContent: true, showTimestamps, showLabels, expanded });
        const collapsed = collapseOutput(frame.data, rendered, expanded);
        timelineEl.insertAdjacentHTML("beforeend", collapsed + "<br><br>");
      }
      // A live stream error is appended to timelineEl directly (not buffered, since it isn't a
      // transcript frame) — without re-appending it here, a toggle click after a stream error
      // would silently erase the visible error message: replaceChildren() above wipes it, and the
      // buffer-only rebuild never had it to begin with. Verified bug (implementation review,
      // cursor spike): selectSeat's non-auth error branch appends "[error] ..." to timelineEl but
      // never touches transcriptBuffer, so any subsequent toggle lost it before this fix.
      if (lastSeatStreamError) {
        timelineEl.textContent += lastSeatStreamError;
      }
      timelineEl.scrollTop = timelineEl.scrollHeight;
    }

    toggleTimestampsButton.addEventListener("click", () => {
      showTimestamps = !showTimestamps;
      toggleTimestampsButton.setAttribute("aria-pressed", String(showTimestamps));
      writePersisted("visShowTimestamps", String(showTimestamps));
      rerenderTranscript();
    });

    toggleLabelsButton.addEventListener("click", () => {
      showLabels = !showLabels;
      toggleLabelsButton.setAttribute("aria-pressed", String(showLabels));
      writePersisted("visShowLabels", String(showLabels));
      rerenderTranscript();
    });

    toggleExpandButton.addEventListener("click", () => {
      expanded = !expanded;
      toggleExpandButton.setAttribute("aria-pressed", String(expanded));
      writePersisted("visExpanded", String(expanded));
      rerenderTranscript();
    });

    toggleFullWidthButton.addEventListener("click", () => {
      fullWidth = !fullWidth;
      toggleFullWidthButton.setAttribute("aria-pressed", String(fullWidth));
      writePersisted("visFullWidth", String(fullWidth));
      seatPanelEl.hidden = fullWidth;
    });

    let lastAgentOptionsKey = null;

    function renderSeats() {
      // The date filter is history-only (live seats are a live stream, not dated rows).
      dateFilterLabel.hidden = seatSource !== "history";
      const total = Object.keys(seats).length;
      const filtered = visibleSeats(seats, statusFilter, agentFilter);
      const runSeats = [];
      const orchestratorSeats = [];
      filtered.forEach((seat) => {
        if (isOrchestratorSeat(seat)) {
          orchestratorSeats.push(seat);
        } else {
          runSeats.push(seat);
        }
      });
      runSeats.sort((a, b) => {
        return String(b.last_event_ts || "").localeCompare(String(a.last_event_ts || ""));
      });
      const seatRow = (seat) => {
        const li = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.taskId = seat.task_id;
        button.setAttribute("aria-current", seat.task_id === selectedTaskId ? "true" : "false");
        button.innerHTML = [
          '<span class="seat-id"></span>',
          '<span class="state-cell"><span class="pulse-dot" aria-hidden="true"></span><span class="badge"></span></span>',
          '<span class="age"></span>',
          '<span class="run-id"></span>',
          '<span class="last-event"></span>',
        ].join("");
        button.querySelector(".seat-id").textContent = seat.seat_id || seat.task_id || "unknown";
        const stateCell = button.querySelector(".state-cell");
        const badge = stateCell.querySelector(".badge");
        badge.textContent = seat.state || "unknown";
        badge.dataset.state = seat.state || "unknown";
        stateCell.classList.toggle("is-running", seat.state === "running");
        if (seat.voted && seat.stance) {
          badge.textContent += " (" + seat.stance + ")";
        }
        button.querySelector(".age").textContent = seatAgeLabel(seat.last_event_ts, seatSource, Date.now());
        button.querySelector(".run-id").textContent = seat.run_id || "";
        button.querySelector(".last-event").textContent = seat.last_event || "";
        button.addEventListener("click", () => selectSeat(seat.task_id));
        li.appendChild(button);
        return li;
      };
      const orchestratorRows = [];
      if (orchestratorSeats.length > 0) {
        const divider = document.createElement("li");
        divider.className = "orchestrator-divider";
        divider.setAttribute("aria-hidden", "true");
        divider.textContent = "orchestrator";
        orchestratorRows.push(divider, ...orchestratorSeats.map(seatRow));
      }
      seatsEl.replaceChildren(...runSeats.map(seatRow));
      orchestratorSeatsEl.replaceChildren(...orchestratorRows);
      historyStatusEl.textContent = historyStatusLine();
      filterCountEl.textContent = filtered.length + "/" + total;
      updateAgentFilterOptions();
    }

    function updateAgentFilterOptions() {
      const options = deriveAgentOptions(seats);
      const optionsKey = options.join(" ");
      if (optionsKey !== lastAgentOptionsKey) {
        lastAgentOptionsKey = optionsKey;
        agentFilterEl.replaceChildren(
          ...options.map((value) => {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = value;
            return option;
          })
        );
      }
      if (options.includes(agentFilter)) {
        agentFilterEl.value = agentFilter;
      } else if (options.length > 1) {
        // Only reset+persist when there's real seat data to validate against — openOrchestrator
        // calls renderSeats() with an empty `seats` object before any SSE frame arrives, at which
        // point deriveAgentOptions({}) returns exactly ["all"]. Without this guard, a persisted
        // agentFilter like "codex" would be found "not in options" and immediately overwritten to
        // "all" in localStorage on every single page load, before real data ever proves it valid.
        agentFilter = "all";
        agentFilterEl.value = "all";
        writePersisted("visAgentFilter", "all");
      }
      // else: options === ["all"] only (no seats loaded yet) — leave agentFilter untouched.
    }

    function historyStatusLine() {
      if (seatSource !== "history") {
        return "";
      }
      if (historyLoading) {
        return "· history — loading…";
      }
      if (historyStatusText) {
        return "· history — " + historyStatusText;
      }
      return "· history";
    }

    function setSeatSource(nextSource) {
      if (nextSource === seatSource) {
        return;
      }
      historyGen += 1;
      if (stopSeatStream) {
        stopSeatStream();
        stopSeatStream = null;
      }
      selectedTaskId = "";
      Object.keys(seats).forEach((key) => delete seats[key]);
      timelineEl.textContent = "";
      transcriptBuffer.length = 0;

      seatSource = nextSource;
      historyCursor = null;
      historyHasMore = false;
      historyStatusText = "";
      updateModeButtons();

      if (nextSource === "history") {
        historyLoading = true;
        renderSeats();
        fetchHistoryPage({ append: false });
        return;
      }

      historyLoading = false;
      renderSeats();
      if (stopOrchestratorStream) {
        stopOrchestratorStream();
      }
      stopOrchestratorStream = startOrchestratorStream(selectedOrchestratorId);
    }

    function fetchHistoryPage({ append }) {
      const gen = historyGen;
      const params = new URLSearchParams();
      if (append && historyCursor) {
        params.set("cursor", historyCursor);
      } else if (dateFilter) {
        params.set("date", dateFilter);
      }
      const query = params.toString();
      const url = "/orchestrators/" + encodeURIComponent(selectedOrchestratorId) + "/seats/history" + (query ? "?" + query : "");
      historyLoading = true;
      renderSeats();
      fetch(url, { headers: authHeaders() })
        .then((response) => {
          if (isStaleHistoryGen(gen, historyGen)) {
            return null;
          }
          if (response.status === 401 || response.status === 403) {
            showAuthBanner();
            historyLoading = false;
            historyStatusText = "";
            renderSeats();
            return null;
          }
          if (!response.ok) {
            historyLoading = false;
            historyStatusText = "history unavailable";
            renderSeats();
            return null;
          }
          return response.json();
        })
        .then((payload) => {
          if (payload === null || isStaleHistoryGen(gen, historyGen)) {
            return;
          }
          applyHistoryPage(payload, append);
        })
        .catch(() => {
          if (isStaleHistoryGen(gen, historyGen)) {
            return;
          }
          historyLoading = false;
          historyStatusText = "history unavailable";
          renderSeats();
        });
    }

    function applyHistoryPage(payload, append) {
      historyLoading = false;
      historyStatusText = "";
      const rows = Array.isArray(payload.seats) ? payload.seats : [];
      if (!append) {
        Object.keys(seats).forEach((key) => delete seats[key]);
      }
      for (const seat of rows) {
        if (append && Object.prototype.hasOwnProperty.call(seats, seat.task_id)) {
          continue;
        }
        seats[seat.task_id] = seat;
      }
      historyCursor = payload.next_cursor || null;
      historyHasMore = Boolean(payload.has_more);
      renderSeats();
      if (
        shouldAutoFetchHistoryPage({
          seatSource,
          historyHasMore,
          historyLoading,
          historyCursor,
          scrollHeight: seatsEl.scrollHeight,
          clientHeight: seatsEl.clientHeight,
          fullWidth,
        })
      ) {
        fetchHistoryPage({ append: true });
      }
    }

    function selectSeat(taskId) {
      if (!taskId || taskId === selectedTaskId) {
        return;
      }
      selectedTaskId = taskId;
      timelineEl.textContent = "";
      transcriptBuffer.length = 0;
      lastSeatStreamError = null;
      if (stopSeatStream) {
        stopSeatStream();
      }
      stopSeatStream = streamSSE("/sse/seat/" + encodeURIComponent(taskId), (frame) => {
        if (frame.event === "error") {
          const status = frame.data && frame.data.status;
          if (status === 401 || status === 403) {
            showAuthBanner();
          } else {
            lastSeatStreamError = "[error] " + frame.data.message + "\n";
            timelineEl.textContent += lastSeatStreamError;
          }
          return;
        }
        lastSeatStreamError = null;
        if (isLifecycleNoise(frame.data)) {
          return;
        }
        transcriptBuffer.push(frame);
        appendTimelineFrame(timelineEl, frame, { escapeContent: true, showTimestamps, showLabels, expanded });
      });
      renderSeats();
    }

    function openOrchestrator(orchestratorId) {
      const isOrchestratorSwitch = selectedOrchestratorId !== "" && selectedOrchestratorId !== orchestratorId;
      selectedOrchestratorId = orchestratorId;
      orchestratorSelect.value = orchestratorId; // keep the header dropdown in sync when opened
      // from elsewhere (e.g. the left-pane orchestrator list), not just its own change event.
      historyGen += 1;
      seatSource = "live";
      historyCursor = null;
      historyHasMore = false;
      historyLoading = false;
      historyStatusText = "";
      updateModeButtons();
      modeLiveButton.disabled = false;
      modeHistoryButton.disabled = false;

      if (isOrchestratorSwitch) {
        statusFilter = "all";
        agentFilter = "all";
        writePersisted("visStatusFilter", "all");
        writePersisted("visAgentFilter", "all");
        statusFilterEl.value = "all";
      }

      Object.keys(seats).forEach((key) => delete seats[key]);
      selectedTaskId = "";
      timelineEl.textContent = "";
      transcriptBuffer.length = 0;
      if (stopOrchestratorStream) {
        stopOrchestratorStream();
      }
      if (stopSeatStream) {
        stopSeatStream();
        stopSeatStream = null;
      }
      renderSeats();
      stopOrchestratorStream = startOrchestratorStream(orchestratorId);
    }

    function startOrchestratorStream(orchestratorId) {
      return streamSSE("/sse/orchestrator/" + encodeURIComponent(orchestratorId), (frame) => {
        if (frame.event === "error") {
          const status = frame.data && frame.data.status;
          if (status === 401 || status === 403) {
            showAuthBanner();
          } else {
            timelineEl.textContent = "[error] " + frame.data.message + "\n";
            transcriptBuffer.length = 0;
          }
          return;
        }
        if (seatSource !== "live") {
          return;
        }
        const seat = frame.data;
        seats[seat.task_id] = seat;
        renderSeats();
      });
    }

    async function loadOrchestrators() {
      if (stopOrchestratorStream) {
        stopOrchestratorStream();
        stopOrchestratorStream = null;
      }
      const clearOrchestratorView = () => {
        orchestratorSelect.replaceChildren();
        seatsEl.replaceChildren();
        orchestratorSeatsEl.replaceChildren();
        timelineEl.textContent = "";
        transcriptBuffer.length = 0;
      };
      let response;
      try {
        response = await fetch("/orchestrators", { headers: authHeaders() });
      } catch (err) {
        // Network-level failure — surface it, never leave a silent blank dropdown.
        clearOrchestratorView();
        timelineEl.textContent = "[error] /orchestrators unreachable\n";
        return;
      }
      // An auth edge (e.g. Cloudflare Access) can 302 this XHR to an HTML login page that itself
      // resolves 200; fetch follows the redirect transparently. Treat a followed redirect the same
      // as a 401 — otherwise response.json() below throws on the HTML into a swallowed rejection
      // (loadOrchestrators is called un-awaited) and the dropdown stays silently empty.
      if (response.status === 401 || response.status === 403 || response.redirected) {
        showAuthBanner();
        clearOrchestratorView();
        return;
      }
      hideAuthBanner();
      if (!response.ok) {
        clearOrchestratorView();
        timelineEl.textContent = "[error] /orchestrators " + response.status + "\n";
        return;
      }
      let payload;
      try {
        payload = await response.json();
      } catch (err) {
        // 200 but a non-JSON body (an auth wall that didn't set response.redirected) — fail loud
        // via the auth banner instead of silently swallowing the JSON parse error.
        showAuthBanner();
        clearOrchestratorView();
        return;
      }
      renderTees(payload.tees || []);
      orchestratorSelect.replaceChildren(
        ...(payload.orchestrators || []).map((id) => {
          const option = document.createElement("option");
          option.value = id;
          option.textContent = id;
          return option;
        })
      );
      modeLiveButton.disabled = !orchestratorSelect.value;
      modeHistoryButton.disabled = !orchestratorSelect.value;
      if (orchestratorSelect.value) {
        openOrchestrator(orchestratorSelect.value);
      }
    }

    function renderTees(tees) {
      const el = document.getElementById("tees-status");
      if (!el) return;
      el.replaceChildren(
        ...tees.map((tee) => {
          const span = document.createElement("span");
          span.className = "tee-" + tee.state;
          if (tee.state === "fresh") {
            span.textContent = "tee ✓" + (tee.tailers === 0 ? " (0 tailers)" : "");
          } else if (tee.state === "stale") {
            span.textContent = "tee STALE since " + (tee.ts || "?");
          } else {
            span.textContent = "tee MISSING (" + tee.label + ")";
          }
          return span;
        })
      );
    }

    orchestratorSelect.addEventListener("change", () => openOrchestrator(orchestratorSelect.value));
    const HISTORY_SCROLL_THRESHOLD_PX = 40;
    seatsEl.addEventListener("scroll", () => {
      if (seatSource !== "history" || !historyHasMore || historyLoading || !historyCursor) {
        return;
      }
      if (isScrolledNearBottom(seatsEl.scrollTop, seatsEl.clientHeight, seatsEl.scrollHeight, HISTORY_SCROLL_THRESHOLD_PX)) {
        fetchHistoryPage({ append: true });
      }
    });

    const AGE_TICK_MS = 2000;
    setInterval(() => {
      renderSeats();
    }, AGE_TICK_MS);

    statusFilterEl.value = statusFilter;
    toggleTimestampsButton.setAttribute("aria-pressed", String(showTimestamps));
    toggleLabelsButton.setAttribute("aria-pressed", String(showLabels));
    toggleExpandButton.setAttribute("aria-pressed", String(expanded));
    toggleFullWidthButton.setAttribute("aria-pressed", String(fullWidth));
    seatPanelEl.hidden = fullWidth;

    loadOrchestrators();
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      authHeaders,
      appendTimelineFrame,
      escapeHtml,
      formatTimelineEvent,
      formatTimelineFrame,
      isRealEventId,
      parseFrames,
      reduceSeat,
      streamSSE,
      ageLabel,
      utcDayMonth,
      seatAgeLabel,
      isStaleHistoryGen,
      isScrolledNearBottom,
      shouldAutoFetchHistoryPage,
      agentOf,
      visibleSeats,
      deriveAgentOptions,
      collapseOutput,
      isTruncatableOutput,
      isLifecycleNoise,
    };
  }
  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", init);
  }
})();
