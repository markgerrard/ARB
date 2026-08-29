import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

from arb_memory.visibility import _reduce_seat


ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "src" / "arb_memory" / "static" / "app.js"
INDEX_HTML = ROOT / "src" / "arb_memory" / "static" / "index.html"
FIXTURE = ROOT / "tests" / "fixtures" / "sse_web_orchestrator.txt"

_FLUSH_JS = """
function flush(times) {
  return new Promise((resolve) => {
    let remaining = times;
    function step() {
      if (remaining <= 0) { resolve(); return; }
      remaining -= 1;
      setImmediate(step);
    }
    step();
  });
}
"""

_DOM_HARNESS_JS = """
class FakeClassList {
  constructor() { this._set = new Set(); }
  add(c) { for (const part of String(c).split(/\\s+/).filter(Boolean)) this._set.add(part); }
  remove(c) { this._set.delete(c); }
  toggle(c, force) {
    if (force === true) { this._set.add(c); return true; }
    if (force === false) { this._set.delete(c); return false; }
    if (this._set.has(c)) { this._set.delete(c); return false; }
    this._set.add(c); return true;
  }
  contains(c) { return this._set.has(c); }
}

class FakeElement {
  constructor(tag) {
    this.tagName = (tag || "div").toUpperCase();
    this._listeners = {};
    this._attrs = {};
    this.dataset = {};
    this.classList = new FakeClassList();
    this.children = [];
    this.textContent = "";
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
  }
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  dispatchEvent(type, evt) {
    (this._listeners[type] || []).forEach((fn) => fn(evt || {}));
  }
  setAttribute(name, value) { this._attrs[name] = String(value); }
  getAttribute(name) { return this._attrs[name]; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...nodes) {
    this.children = nodes;
    if (nodes.length && typeof nodes[0].value === "string") {
      this.value = nodes[0].value;
    } else if (!nodes.length) {
      this.value = "";
      this.textContent = "";
    }
  }
  set innerHTML(html) {
    this.children = [];
    const stack = [this];
    const tokenRe = /<\\/?span\\s+class="([^"]+)"[^>]*>|<\\/span>/g;
    let match;
    while ((match = tokenRe.exec(html))) {
      const full = match[0];
      const cls = match[1];
      if (full.startsWith("</")) {
        stack.pop();
      } else {
        const child = new FakeElement("span");
        child.classList.add(cls);
        stack[stack.length - 1].children.push(child);
        stack.push(child);
      }
    }
  }
  insertAdjacentHTML(where, html) {
    this.textContent += html;
  }
  querySelector(selector) {
    const cls = selector.replace(".", "");
    const direct = this.children.find((c) => c.classList.contains(cls));
    if (direct) return direct;
    for (const child of this.children) {
      const nested = child.querySelector ? child.querySelector(selector) : null;
      if (nested) return nested;
    }
    return null;
  }
}

function flush(times) {
  return new Promise((resolve) => {
    let remaining = times;
    function step() {
      if (remaining <= 0) { resolve(); return; }
      remaining -= 1;
      setImmediate(step);
    }
    step();
  });
}

function makeDom() {
  const elements = {};
  const get = (id) => elements[id] || (elements[id] = new FakeElement());
  let domReadyCallback = null;
  const assignedLocations = [];
  global.document = {
    getElementById: get,
    createElement: (tag) => new FakeElement(tag),
    addEventListener: (type, fn) => {
      if (type === "DOMContentLoaded") domReadyCallback = fn;
    },
  };
  global.window = {
    location: {
      pathname: "/",
      assign(url) { assignedLocations.push(url); },
    },
  };
  global.localStorage = {
    _store: { token: "test-token-123" },
    getItem(key) {
      return Object.prototype.hasOwnProperty.call(this._store, key) ? this._store[key] : null;
    },
    setItem(key, value) { this._store[key] = value; },
    removeItem(key) { delete this._store[key]; },
    token: "",
  };
  global.setInterval = () => 0;
  return { elements, get, ready: () => domReadyCallback, assignedLocations };
}

function makeSseResponse(signal) {
  let resolveNext = null;
  let rejectNext = null;
  const pending = [];
  signal.addEventListener("abort", () => {
    if (rejectNext) {
      const rj = rejectNext;
      resolveNext = null;
      rejectNext = null;
      const err = new Error("aborted");
      err.name = "AbortError";
      rj(err);
    }
  });
  const encoder = new TextEncoder();
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        return {
          read() {
            return new Promise((resolve, reject) => {
              if (pending.length) {
                resolve(pending.shift());
              } else {
                resolveNext = resolve;
                rejectNext = reject;
              }
            });
          },
        };
      },
    },
    push(text) {
      const chunk = { done: false, value: encoder.encode(text) };
      if (resolveNext) {
        const r = resolveNext;
        resolveNext = null;
        rejectNext = null;
        r(chunk);
      } else {
        pending.push(chunk);
      }
    },
  };
}

function seatTaskIds(dom) {
  return dom.get("seats").children.map((li) => li.children[0].dataset.taskId);
}
"""


def _node():
    node = shutil.which("node") or "/opt/homebrew/bin/node"
    if not Path(node).exists() and "/" in node:
        return None
    return node


def _parse_fixture_python(text):
    frames = []
    for raw in text.split("\n\n"):
        current = {}
        for line in raw.splitlines():
            if not line or line.startswith(":"):
                continue
            key, _, value = line.partition(":")
            current[key] = value[1:] if value.startswith(" ") else value
        if current:
            current["data"] = json.loads(current["data"])
            frames.append(current)
    return frames


def test_appjs_parser_and_reducer_match_visibility_reducer():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the web reducer contract test")
    fixture_text = FIXTURE.read_text()
    script = textwrap.dedent(
        f"""
        const {{ readFileSync }} = require("fs");
        const {{ reduceSeat, parseFrames }} = require({json.dumps(str(APP_JS))});
        const input = readFileSync({json.dumps(str(FIXTURE))}, "utf8");
        const firstCut = input.slice(0, 37);
        const secondCut = input.slice(37);
        let parsed = parseFrames(firstCut);
        let tail = parsed.tail;
        parsed = parseFrames(tail + secondCut);
        const frames = parsed.frames.map((frame) => ({{...frame, data: JSON.parse(frame.data)}}));
        const seats = {{}};
        for (const frame of frames) {{
          const entry = frame.data;
          seats[entry.task_id] = reduceSeat(seats[entry.task_id] || {{}}, entry);
        }}
        console.log(JSON.stringify({{frames, seats, tail: parsed.tail}}));
        """
    )

    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    js = json.loads(completed.stdout)

    expected_seats = {}
    for frame in _parse_fixture_python(fixture_text):
        entry = frame["data"]
        expected_seats[entry["task_id"]] = _reduce_seat(expected_seats.get(entry["task_id"], {}), entry)

    assert js["frames"] == _parse_fixture_python(fixture_text)
    assert js["seats"] == expected_seats
    assert js["tail"] == ""


def test_appjs_real_last_event_id_filter():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the web Last-Event-ID contract test")
    script = textwrap.dedent(
        f"""
        const {{ isRealEventId }} = require({json.dumps(str(APP_JS))});
        console.log(JSON.stringify({{
          real: isRealEventId("1719341000000-0"),
          composite: isRealEventId("e=1719341000000-0;t=1719341001000-0"),
          partial: isRealEventId("e=1719341000000-0;t=-"),
          backfill: isRealEventId("backfill-1"),
          stale: isRealEventId("stale-task-a"),
          empty: isRealEventId("")
        }}));
        """
    )

    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)

    assert json.loads(completed.stdout) == {
        "real": True,
        "composite": True,
        "partial": True,
        "backfill": False,
        "stale": False,
        "empty": False,
    }


def test_appjs_agent_of_ported_cases():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the agentOf ported-cases contract test")
    script = textwrap.dedent(
        f"""
        const {{ agentOf }} = require({json.dumps(str(APP_JS))});
        console.log(JSON.stringify({{
          coldOpus: agentOf("cold-opus-1"),
          empty: agentOf(""),
          codex: agentOf("codex-1"),
          agy: agentOf("agy-1"),
          pi: agentOf("pi-1"),
          gemini: agentOf("gemini-1"),
          cursor: agentOf("cursor-1"),
          grok: agentOf("grok-1"),
          kimi: agentOf("kimi-1"),
          claude: agentOf("claude-1"),
          unknownLowercased: agentOf("Foo-bar-1"),
        }}));
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    assert json.loads(completed.stdout) == {
        "coldOpus": "opus",
        "empty": "?",
        "codex": "codex",
        "agy": "agy",
        "pi": "pi",
        "gemini": "gemini",
        "cursor": "cursor",
        "grok": "grok",
        "kimi": "kimi",
        "claude": "claude",
        "unknownLowercased": "foo",
    }


def test_appjs_visible_seats_filters_by_status_and_agent():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the visibleSeats contract test")
    script = textwrap.dedent(
        f"""
        const {{ visibleSeats }} = require({json.dumps(str(APP_JS))});
        const seats = {{
          "t-1": {{ task_id: "t-1", seat_id: "codex-1", state: "running" }},
          "t-2": {{ task_id: "t-2", seat_id: "codex-2", state: "done" }},
          "t-3": {{ task_id: "t-3", seat_id: "agy-1", state: "failed" }},
          "t-4": {{ task_id: "t-4", seat_id: "Foo-1" }},
        }};
        console.log(JSON.stringify({{
          all: visibleSeats(seats, "all", "all").map((s) => s.task_id).sort(),
          running: visibleSeats(seats, "running", "all").map((s) => s.task_id),
          unknownStatus: visibleSeats(seats, "unknown", "all").map((s) => s.task_id),
          codexAgent: visibleSeats(seats, "all", "codex").map((s) => s.task_id).sort(),
        }}));
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["all"] == ["t-1", "t-2", "t-3", "t-4"]
    assert result["running"] == ["t-1"]
    assert result["unknownStatus"] == ["t-4"]
    assert result["codexAgent"] == ["t-1", "t-2"]


def test_appjs_derive_agent_options_dedupes_sorts_and_prepends_all():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the deriveAgentOptions contract test")
    script = textwrap.dedent(
        f"""
        const {{ deriveAgentOptions }} = require({json.dumps(str(APP_JS))});
        const seats = {{
          "t-1": {{ seat_id: "codex-1" }},
          "t-2": {{ seat_id: "codex-2" }},
          "t-3": {{ seat_id: "agy-1" }},
          "t-4": {{ seat_id: "Foo-1" }},
        }};
        console.log(JSON.stringify(deriveAgentOptions(seats)));
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    assert json.loads(completed.stdout) == ["all", "agy", "codex", "foo"]


def test_appjs_is_truncatable_output_field_scope():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the isTruncatableOutput field-scope contract test")
    script = textwrap.dedent(
        f"""
        const {{ isTruncatableOutput }} = require({json.dumps(str(APP_JS))});
        console.log(JSON.stringify({{
          commandOutput: isTruncatableOutput({{ kind: "command_output" }}),
          commandFinished: isTruncatableOutput({{ kind: "command_finished" }}),
          toolOutput: isTruncatableOutput({{ kind: "tool_output" }}),
          applyPatchWithMatchingKind: isTruncatableOutput({{
            kind: "command_finished", tool_name: "apply_patch", meta: {{ file: "x.py" }},
          }}),
          applyPatchKindLiteral: isTruncatableOutput({{ kind: "apply_patch" }}),
          applyPatchNoKind: isTruncatableOutput({{ tool_name: "apply_patch", meta: {{ file: "x" }} }}),
          applyPatchNoMetaFile: isTruncatableOutput({{ kind: "command_output", tool_name: "apply_patch", meta: {{}} }}),
          nullData: isTruncatableOutput(null),
          emptyData: isTruncatableOutput({{}}),
        }}));
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["commandOutput"] is True
    assert result["commandFinished"] is True
    assert result["toolOutput"] is True
    assert result["applyPatchWithMatchingKind"] is False
    assert result["applyPatchKindLiteral"] is False
    assert result["applyPatchNoKind"] is False
    assert result["applyPatchNoMetaFile"] is True
    assert result["nullData"] is False
    assert result["emptyData"] is False


def test_appjs_is_lifecycle_noise_matches_go_reduce_go():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the isLifecycleNoise contract test")
    script = textwrap.dedent(
        f"""
        const {{ isLifecycleNoise }} = require({json.dumps(str(APP_JS))});
        console.log(JSON.stringify({{
          transcript: isLifecycleNoise({{ source: "transcript", kind: "model_text" }}),
          eval: isLifecycleNoise({{ source: "eval", event_type: "command_started" }}),
          missingSource: isLifecycleNoise({{ kind: "model_text" }}),
          nullData: isLifecycleNoise(null),
          emptyData: isLifecycleNoise({{}}),
        }}));
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["transcript"] is False
    assert result["eval"] is True
    assert result["missingSource"] is True
    assert result["nullData"] is True
    assert result["emptyData"] is True


def test_appjs_collapse_output_truncates_long_plain_text_output():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the collapseOutput truncation contract test")
    script = textwrap.dedent(
        f"""
        const {{ collapseOutput }} = require({json.dumps(str(APP_JS))});
        const data = {{ kind: "command_output" }};
        const nineLines = ["l1","l2","l3","l4","l5","l6","l7","l8","l9"].join("\\n");
        const shortLines = "one\\ntwo";
        console.log(JSON.stringify({{
          truncated: collapseOutput(data, nineLines, false),
          expanded: collapseOutput(data, nineLines, true),
          short: collapseOutput(data, shortLines, false),
        }}));
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["truncated"] == (
        "l1\nl2\nl3\nl4\nl5\nl6\n"
        '<span class="dim">… +3 line(s) — click Expand output to see more</span>'
    )
    assert result["expanded"] == "l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9"
    assert result["short"] == "one\ntwo"


def test_appjs_collapse_output_excludes_apply_patch_and_model_thinking():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the collapseOutput exclusion contract test")
    script = textwrap.dedent(
        f"""
        const {{ collapseOutput }} = require({json.dumps(str(APP_JS))});
        const applyPatchData = {{
          kind: "command_finished", tool_name: "apply_patch", meta: {{ file: "foo.py" }},
        }};
        const applyPatchRendered = [
          "edited `foo.py` +3/-1",
          "<details><summary>diff</summary>",
          "line1", "line2", "line3", "line4", "line5", "line6",
          "</details>",
        ].join("\\n");
        const thinkingData = {{ kind: "model_thinking" }};
        const thinkingRendered = [
          "<details><summary>thinking</summary>",
          "line1", "line2", "line3", "line4", "line5", "line6",
          "</details>",
        ].join("\\n");
        console.log(JSON.stringify({{
          applyPatch: collapseOutput(applyPatchData, applyPatchRendered, false),
          thinking: collapseOutput(thinkingData, thinkingRendered, false),
        }}));
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    apply_patch_rendered = "\n".join([
        "edited `foo.py` +3/-1",
        "<details><summary>diff</summary>",
        "line1", "line2", "line3", "line4", "line5", "line6",
        "</details>",
    ])
    thinking_rendered = "\n".join([
        "<details><summary>thinking</summary>",
        "line1", "line2", "line3", "line4", "line5", "line6",
        "</details>",
    ])
    assert result["applyPatch"] == apply_patch_rendered
    assert result["thinking"] == thinking_rendered


def test_appjs_formats_transcript_timeline_kinds():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the web transcript render contract test")
    samples = [
        {
            "event": "transcript",
            "data": {
                "source": "transcript",
                "ts": "2026-06-25T10:00:00+00:00",
                "kind": "model_text",
                "content": "hello ‹redacted›",
            },
        },
        {
            "event": "transcript",
            "data": {
                "source": "transcript",
                "ts": "2026-06-25T10:00:01+00:00",
                "kind": "model_thinking",
                "content": "checking plan",
            },
        },
        {
            "event": "transcript",
            "data": {
                "source": "transcript",
                "ts": "2026-06-25T10:00:02+00:00",
                "kind": "command_finished",
                "tool_name": "apply_patch",
                "content": "patch",
                "meta": {"file": "foo.py", "added": 3, "removed": 1},
            },
        },
        {
            "event": "transcript",
            "data": {
                "source": "transcript",
                "ts": "2026-06-25T10:00:03+00:00",
                "kind": "command_output",
                "tool_name": "bash",
                "content": "$ pytest\npassed",
            },
        },
        {
            "event": "transcript",
            "data": {
                "source": "transcript",
                "ts": "2026-06-25T10:00:04+00:00",
                "kind": "Read",
                "tool_name": "Read",
                "content": "",
            },
        },
    ]
    script = textwrap.dedent(
        f"""
        const {{ formatTimelineFrame }} = require({json.dumps(str(APP_JS))});
        const samples = {json.dumps(samples)};
        console.log(JSON.stringify(samples.map(formatTimelineFrame)));
        """
    )

    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)

    assert json.loads(completed.stdout) == [
        "hello ‹redacted›",
        "<details><summary>thinking</summary>\nchecking plan\n</details>",
        "edited `foo.py` +3/-1\n<details><summary>diff</summary>\npatch\n</details>",
        "bash\n$ pytest\npassed",
        "Read",
    ]


def test_appjs_appends_transcript_details_as_html():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the web transcript render contract test")
    script = textwrap.dedent(
        f"""
        const {{ appendTimelineFrame }} = require({json.dumps(str(APP_JS))});
        const calls = [];
        const timeline = {{
          textContent: "",
          scrollTop: 0,
          scrollHeight: 10,
          insertAdjacentHTML: (where, html) => calls.push([where, html])
        }};
        appendTimelineFrame(timeline, {{
          event: "transcript",
          data: {{
            source: "transcript",
            ts: "2026-06-25T10:00:01+00:00<img>",
            kind: "model_thinking",
            content: "checking <plan> ‹redacted›"
          }}
        }}, {{ escapeContent: true }});
        appendTimelineFrame(timeline, {{
          event: "transcript",
          data: {{
            source: "transcript",
            ts: "2026-06-25T10:00:02+00:00",
            kind: "command_finished<script>",
            tool_name: "apply_patch",
            content: "patch <body>",
            meta: {{file: "<script>alert(1)</script>", added: "3<", removed: "1>"}}
          }}
        }}, {{ escapeContent: true }});
        console.log(JSON.stringify({{textContent: timeline.textContent, calls}}));
        """
    )

    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)

    assert json.loads(completed.stdout) == {
        "textContent": "",
        "calls": [
            [
                "beforeend",
                "<details><summary>thinking</summary>\nchecking &lt;plan&gt; ‹redacted›\n</details><br><br>",
            ],
            [
                "beforeend",
                "edited `&lt;script&gt;alert(1)&lt;/script&gt;` +3/-1\n<details><summary>diff</summary>\npatch &lt;body&gt;\n</details><br><br>",
            ],
        ],
    }


def test_appjs_streamsse_stops_retrying_after_4xx():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the streamSSE fatal-4xx contract test")
    script = _FLUSH_JS + textwrap.dedent(
        f"""
        global.localStorage = {{ getItem: () => null, token: "" }};
        global.setTimeout = (fn) => setImmediate(fn);
        const {{ streamSSE }} = require({json.dumps(str(APP_JS))});

        let fetchCalls = 0;
        global.fetch = () => {{
          fetchCalls += 1;
          return Promise.resolve({{ ok: false, status: 401 }});
        }};

        const events = [];
        streamSSE("http://example/sse", (frame) => events.push(frame));

        flush(20).then(() => {{
          console.log(JSON.stringify({{ fetchCalls, events }}));
        }});
        """
    )
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        text=True,
        capture_output=True,
        timeout=10,
    )
    result = json.loads(completed.stdout)
    assert result["fetchCalls"] == 1
    assert result["events"] == [
        {"event": "error", "data": {"message": "SSE 401", "status": 401}}
    ]


def test_appjs_streamsse_keeps_retrying_after_5xx():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the streamSSE non-fatal-5xx contract test")
    script = _FLUSH_JS + textwrap.dedent(
        f"""
        global.localStorage = {{ getItem: () => null, token: "" }};
        global.setTimeout = (fn) => setImmediate(fn);
        const {{ streamSSE }} = require({json.dumps(str(APP_JS))});

        let fetchCalls = 0;
        global.fetch = () => {{
          fetchCalls += 1;
          return Promise.resolve({{ ok: false, status: 500 }});
        }};

        const stop = streamSSE("http://example/sse", () => {{}});

        flush(20).then(() => {{
          stop();
          console.log(JSON.stringify({{ fetchCalls }}));
        }});
        """
    )
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        text=True,
        capture_output=True,
        timeout=10,
    )
    result = json.loads(completed.stdout)
    assert result["fetchCalls"] > 1


def test_appjs_utc_dd_mm_formatter_ignores_local_timezone():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the UTC dd/mm formatter contract test")
    script = textwrap.dedent(
        f"""
        const {{ seatAgeLabel }} = require({json.dumps(str(APP_JS))});
        const result = seatAgeLabel(
          "2026-06-25T23:50:00Z",
          "history",
          Date.parse("2026-06-27T00:00:00Z")
        );
        console.log(JSON.stringify({{ result }}));
        """
    )
    env = dict(os.environ)
    env["TZ"] = "Pacific/Kiritimati"
    completed = subprocess.run(
        [node, "-e", script], check=True, text=True, capture_output=True, env=env
    )
    assert json.loads(completed.stdout)["result"] == "25/06"


def test_appjs_history_gen_guard_discards_stale_fetch():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the history-gen guard contract test")
    script = textwrap.dedent(
        f"""
        const {{ isStaleHistoryGen }} = require({json.dumps(str(APP_JS))});
        console.log(JSON.stringify({{
          stale: isStaleHistoryGen(1, 2),
          current: isStaleHistoryGen(2, 2),
        }}));
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    assert json.loads(completed.stdout) == {"stale": True, "current": False}


def test_appjs_scroll_threshold_near_bottom():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the scroll-threshold contract test")
    script = textwrap.dedent(
        f"""
        const {{ isScrolledNearBottom }} = require({json.dumps(str(APP_JS))});
        console.log(JSON.stringify({{
          atThreshold: isScrolledNearBottom(360, 400, 800, 40),
          aboveThreshold: isScrolledNearBottom(300, 400, 800, 40),
        }}));
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    assert json.loads(completed.stdout) == {"atThreshold": True, "aboveThreshold": False}


def test_appjs_viewport_fill_fallback_triggers_additional_fetch():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the viewport-fill fallback contract test")
    script = textwrap.dedent(
        f"""
        const {{ shouldAutoFetchHistoryPage }} = require({json.dumps(str(APP_JS))});
        const base = {{
          seatSource: "history", historyHasMore: true, historyLoading: false,
          historyCursor: "cur-1", scrollHeight: 100, clientHeight: 400, fullWidth: false,
        }};
        console.log(JSON.stringify({{
          fitsViewport: shouldAutoFetchHistoryPage(base),
          overflowsViewport: shouldAutoFetchHistoryPage({{...base, scrollHeight: 800}}),
          stillLoading: shouldAutoFetchHistoryPage({{...base, historyLoading: true}}),
          liveMode: shouldAutoFetchHistoryPage({{...base, seatSource: "live"}}),
          noMorePages: shouldAutoFetchHistoryPage({{...base, historyHasMore: false}}),
          noCursor: shouldAutoFetchHistoryPage({{...base, historyCursor: null}}),
          fullWidthHidesFetch: shouldAutoFetchHistoryPage({{...base, fullWidth: true}}),
        }}));
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    assert json.loads(completed.stdout) == {
        "fitsViewport": True,
        "overflowsViewport": False,
        "stillLoading": False,
        "liveMode": False,
        "noMorePages": False,
        "noCursor": False,
        "fullWidthHidesFetch": False,
    }


def test_appjs_exports_full_contract_surface():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the exports contract test")
    script = textwrap.dedent(
        f"""
        const mod = require({json.dumps(str(APP_JS))});
        const names = Object.keys(mod).sort();
        const types = {{}};
        for (const name of names) {{ types[name] = typeof mod[name]; }}
        console.log(JSON.stringify({{ names, types }}));
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    expected = sorted([
        "authHeaders", "appendTimelineFrame", "escapeHtml", "formatTimelineEvent",
        "formatTimelineFrame", "isRealEventId", "parseFrames", "reduceSeat", "streamSSE",
        "ageLabel", "utcDayMonth", "seatAgeLabel", "isStaleHistoryGen",
        "isScrolledNearBottom", "shouldAutoFetchHistoryPage",
        "agentOf", "visibleSeats", "deriveAgentOptions", "collapseOutput", "isTruncatableOutput",
        "isLifecycleNoise",
    ])
    assert result["names"] == expected
    assert all(t == "function" for t in result["types"].values())


def test_appjs_reduce_seat_guard_comment_states_verified_mechanism():
    text = APP_JS.read_text()
    assert (
        "// Do NOT call from the live path. It expects raw redis fields (event_type/sent_at); a live\n"
        "// frame is already reduced server-side and carries last_event/last_event_ts instead — none of\n"
        "// this function's event_type branches would ever fire, and state would never be set at all\n"
        "// (verified: feeding it real already-reduced frames leaves `state` absent from every output,\n"
        '// not frozen at a prior value — the seat would render "unknown" forever). Kept unwired, exported\n'
        "// only for the JS/Python parity contract test (test_appjs_parser_and_reducer_match_visibility_\n"
        "// reducer), which asserts this function stays byte-identical to Python's _reduce_seat over a\n"
        "// shared fixture.\n"
        "function reduceSeat(state, entry) {"
    ) in text


def test_appjs_clear_token_stops_streams_and_prevents_repopulation():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the Clear full-reset regression test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        let orchestratorSse = null;
        global.fetch = (url, opts) => {{
          if (url === "/orchestrators") {{
            return Promise.resolve({{
              ok: true, status: 200,
              json: () => Promise.resolve({{ orchestrators: ["orch-1"] }}),
            }});
          }}
          if (url.startsWith("/sse/orchestrator/")) {{
            orchestratorSse = makeSseResponse(opts.signal);
            return Promise.resolve(orchestratorSse);
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          const seatsBefore1 = dom.get("seats").children.length;

          orchestratorSse.push(
            "id: 1-0\\nevent: seat_appear\\ndata: " +
            JSON.stringify({{
              task_id: "seat-a", seat_id: "codex-1", run_id: "run-1",
              state: "running", last_event: "task_started",
              last_event_ts: new Date().toISOString(),
            }}) + "\\n\\n"
          );
          await flush(10);
          const seatsAfterFrame = dom.get("seats").children.length;
          const badgeText = dom.get("seats").children[0]
            .querySelector(".state-cell")
            .querySelector(".badge")
            .textContent;

          dom.get("clear-token").dispatchEvent("click");
          const seatsRightAfterClear = dom.get("seats").children.length;

          orchestratorSse.push(
            "id: 2-0\\nevent: seat_appear\\ndata: " +
            JSON.stringify({{
              task_id: "seat-b", seat_id: "codex-2", run_id: "run-2",
              state: "running", last_event: "task_started",
              last_event_ts: new Date().toISOString(),
            }}) + "\\n\\n"
          );
          await flush(10);
          const seatsAfterPostClearFrame = dom.get("seats").children.length;

          console.log(JSON.stringify({{
            seatsBefore1, seatsAfterFrame, badgeText, seatsRightAfterClear, seatsAfterPostClearFrame,
          }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["seatsBefore1"] == 0
    assert result["seatsAfterFrame"] == 1
    assert result["badgeText"] == "running"
    assert result["seatsRightAfterClear"] == 0
    assert result["seatsAfterPostClearFrame"] == 0


def test_appjs_auth_failure_navigates_to_login():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the auth failure navigation test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        global.fetch = (url) => {{
          if (url === "/orchestrators") {{
            return Promise.resolve({{ ok: false, status: 401 }});
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          console.log(JSON.stringify({{
            assignedLocations: dom.assignedLocations,
            authBannerHidden: dom.get("auth-banner").hidden,
            authBannerText: dom.get("auth-banner").textContent,
          }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["assignedLocations"] == ["/login"]
    assert result["authBannerHidden"] is False
    assert result["authBannerText"] == "Session expired — redirecting to sign in…"


def test_appjs_auth_failure_does_not_loop_when_already_on_login():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the auth failure loop guard test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        window.location.pathname = "/login";
        global.fetch = (url) => {{
          if (url === "/orchestrators") {{
            return Promise.resolve({{ ok: false, status: 403 }});
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          console.log(JSON.stringify({{
            assignedLocations: dom.assignedLocations,
            authBannerHidden: dom.get("auth-banner").hidden,
          }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["assignedLocations"] == []
    assert result["authBannerHidden"] is False


def test_appjs_orchestrators_redirect_to_login_html_fails_loud():
    # Regression: an auth edge (e.g. Cloudflare Access) can 302 the /orchestrators XHR to an
    # HTML login page that resolves 200 (fetch follows the redirect transparently). The old code
    # called response.json() unguarded -> SyntaxError on HTML -> swallowed rejection (loadOrchestrators
    # is an un-awaited async call) -> the dropdown stayed silently empty with no banner and no error.
    # It must now FAIL LOUD: show the auth banner and navigate to /login, exactly like a 401.
    node = _node()
    if node is None:
        raise AssertionError("node is required for the orchestrators redirect-to-HTML fail-loud test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        global.fetch = (url) => {{
          if (url === "/orchestrators") {{
            return Promise.resolve({{
              ok: true,
              status: 200,
              redirected: true,
              url: "https://example.cloudflareaccess.com/cdn-cgi/access/login/arb-visibility.example.com",
              json: () => Promise.reject(new SyntaxError("Unexpected token < in JSON at position 0")),
              text: () => Promise.resolve("<html>login</html>"),
            }});
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          console.log(JSON.stringify({{
            assignedLocations: dom.assignedLocations,
            authBannerHidden: dom.get("auth-banner").hidden,
            authBannerText: dom.get("auth-banner").textContent,
            optionCount: dom.get("orchestrator").children.length,
          }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["assignedLocations"] == ["/login"]
    assert result["authBannerHidden"] is False
    assert result["authBannerText"] == "Session expired — redirecting to sign in…"
    assert result["optionCount"] == 0


def test_appjs_init_loads_orchestrators_without_token():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the no-token init orchestrator load test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        delete global.localStorage._store.token;
        global.localStorage.token = "";
        const fetches = [];
        global.fetch = (url, opts) => {{
          fetches.push({{ url, headers: opts && opts.headers }});
          if (url === "/orchestrators") {{
            return Promise.resolve({{
              ok: true, status: 200,
              json: () => Promise.resolve({{ orchestrators: ["orch-1"] }}),
            }});
          }}
          if (url.startsWith("/sse/orchestrator/")) {{
            return Promise.resolve(makeSseResponse(opts.signal));
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          console.log(JSON.stringify({{
            fetches,
            tokenValue: dom.get("token").value,
            orchestratorValue: dom.get("orchestrator").value,
            optionCount: dom.get("orchestrator").children.length,
          }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["tokenValue"] == ""
    assert result["fetches"][0] == {"url": "/orchestrators", "headers": {}}
    assert result["orchestratorValue"] == "orch-1"
    assert result["optionCount"] == 1


def test_appjs_initial_toggle_and_filter_state_reflects_persistence():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the initial-state persistence contract test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        global.fetch = () => Promise.resolve({{
          ok: true, status: 200, json: () => Promise.resolve({{ orchestrators: [] }}),
        }});
        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(5);
          console.log(JSON.stringify({{
            statusFilterValue: dom.get("status-filter").value,
            timestampsPressed: dom.get("toggle-timestamps").getAttribute("aria-pressed"),
            labelsPressed: dom.get("toggle-labels").getAttribute("aria-pressed"),
            expandPressed: dom.get("toggle-expand").getAttribute("aria-pressed"),
            fullWidthPressed: dom.get("toggle-fullwidth").getAttribute("aria-pressed"),
            seatPanelHidden: dom.get("seat-panel").hidden,
          }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["statusFilterValue"] == "all"
    assert result["timestampsPressed"] == "false"
    assert result["labelsPressed"] == "false"
    assert result["expandPressed"] == "false"
    assert result["fullWidthPressed"] == "false"
    assert result["seatPanelHidden"] is False


def test_appjs_render_seats_filters_and_updates_count_and_agent_options():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the renderSeats filtering contract test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        let orchestratorSse = null;
        global.fetch = (url, opts) => {{
          if (url === "/orchestrators") {{
            return Promise.resolve({{
              ok: true, status: 200,
              json: () => Promise.resolve({{ orchestrators: ["orch-1"] }}),
            }});
          }}
          if (url.startsWith("/sse/orchestrator/")) {{
            orchestratorSse = makeSseResponse(opts.signal);
            return Promise.resolve(orchestratorSse);
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          orchestratorSse.push(
            "id: 1-0\\nevent: seat_appear\\ndata: " +
            JSON.stringify({{
              task_id: "seat-a", seat_id: "codex-1", run_id: "run-1",
              state: "running", last_event: "task_started",
              last_event_ts: new Date().toISOString(),
            }}) + "\\n\\n"
          );
          orchestratorSse.push(
            "id: 2-0\\nevent: seat_appear\\ndata: " +
            JSON.stringify({{
              task_id: "seat-b", seat_id: "agy-1", run_id: "run-2",
              state: "done", last_event: "task_finished",
              last_event_ts: new Date().toISOString(),
            }}) + "\\n\\n"
          );
          await flush(10);
          const countAll = dom.get("filter-count").textContent;
          const agentOptionsAll = dom.get("agent-filter").children.map((o) => o.value);

          dom.get("status-filter").value = "running";
          dom.get("status-filter").dispatchEvent("change");
          const countRunning = dom.get("filter-count").textContent;
          const seatsShown = seatTaskIds(dom);

          console.log(JSON.stringify({{ countAll, agentOptionsAll, countRunning, seatsShown }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["countAll"] == "2/2"
    assert result["agentOptionsAll"] == ["all", "agy", "codex"]
    assert result["countRunning"] == "1/2"
    assert result["seatsShown"] == ["seat-a"]


def test_appjs_render_seats_partitions_orchestrators_after_worker_seats():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the orchestrator-partition contract test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        let orchestratorSse = null;
        global.fetch = (url, opts) => {{
          if (url === "/orchestrators") {{
            return Promise.resolve({{
              ok: true, status: 200,
              json: () => Promise.resolve({{ orchestrators: ["claude-test-dev"] }}),
            }});
          }}
          if (url.startsWith("/sse/orchestrator/")) {{
            orchestratorSse = makeSseResponse(opts.signal);
            return Promise.resolve(orchestratorSse);
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          orchestratorSse.push(
            "id: 1-0\\nevent: seat_appear\\ndata: " +
            JSON.stringify({{
              task_id: "run-codex-1", seat_id: "codex-1", orchestrator: "claude-test-dev",
              state: "running", last_event: "task_started",
              last_event_ts: new Date().toISOString(),
            }}) + "\\n\\n"
          );
          orchestratorSse.push(
            "id: 2-0\\nevent: seat_appear\\ndata: " +
            JSON.stringify({{
              task_id: "run-claude-test-dev", seat_id: "claude-test-dev", orchestrator: "claude-test-dev",
              last_event: "command_finished", last_event_ts: new Date().toISOString(),
            }}) + "\\n\\n"
          );
          await flush(10);

          const workerRows = dom.get("seats").children.map((li) => {{
            return li.children[0].querySelector(".seat-id").textContent;
          }});
          const orchestratorRows = dom.get("seats-orchestrator").children.map((li) => {{
            if (li.className === "orchestrator-divider") {{
              return "<divider:" + li.textContent + ">";
            }}
            return li.children[0].querySelector(".seat-id").textContent;
          }});
          const filterCount = dom.get("filter-count").textContent;
          const agentOptions = dom.get("agent-filter").children.map((o) => o.value);

          console.log(JSON.stringify({{ workerRows, orchestratorRows, filterCount, agentOptions }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["workerRows"] == ["codex-1"]
    assert result["orchestratorRows"] == ["<divider:orchestrator>", "claude-test-dev"]
    assert result["filterCount"] == "2/2"
    assert result["agentOptions"] == ["all", "claude", "codex"]


def test_appjs_copy_transcript_button_writes_full_timeline_text_to_clipboard():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the copy-transcript contract test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        let written = null;
        navigator.clipboard = {{ writeText: (text) => {{ written = text; return Promise.resolve(); }} }};
        global.fetch = () => Promise.resolve({{
          ok: true, status: 200, json: () => Promise.resolve({{ orchestrators: [] }}),
        }});

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          dom.get("timeline").textContent = "line one\\nline two\\n";
          dom.get("copy-transcript").dispatchEvent("click");
          await flush(5);
          console.log(JSON.stringify({{ written, buttonText: dom.get("copy-transcript").textContent }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["written"] == "line one\nline two\n"
    assert result["buttonText"] == "Copied"


def test_appjs_update_agent_filter_options_preserves_persisted_filter_before_seats_load():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the empty-seats agent-filter guard test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        global.localStorage._store.visAgentFilter = "codex";
        let orchestratorSse = null;
        global.fetch = (url, opts) => {{
          if (url === "/orchestrators") {{
            return Promise.resolve({{
              ok: true, status: 200,
              json: () => Promise.resolve({{ orchestrators: ["orch-1"] }}),
            }});
          }}
          if (url.startsWith("/sse/orchestrator/")) {{
            orchestratorSse = makeSseResponse(opts.signal);
            return Promise.resolve(orchestratorSse);
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};
        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          // Orchestrator auto-selected and its SSE stream opened, but no seat_appear event has
          // been pushed yet — this is the exact empty-seats window the guard exists for.
          await flush(10);
          console.log(JSON.stringify({{
            agentOptionsWithNoSeats: dom.get("agent-filter").children.map((o) => o.value),
            persistedAgentFilter: global.localStorage._store.visAgentFilter,
          }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["agentOptionsWithNoSeats"] == ["all"]
    assert result["persistedAgentFilter"] == "codex"


def test_appjs_filter_and_toggle_click_handlers_update_state_and_rerender():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the filter/toggle click-handler contract test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        global.fetch = () => Promise.resolve({{
          ok: true, status: 200, json: () => Promise.resolve({{ orchestrators: [] }}),
        }});
        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          dom.get("status-filter").value = "running";
          dom.get("status-filter").dispatchEvent("change");
          dom.get("toggle-timestamps").dispatchEvent("click");
          dom.get("toggle-labels").dispatchEvent("click");
          dom.get("toggle-expand").dispatchEvent("click");
          dom.get("toggle-fullwidth").dispatchEvent("click");

          console.log(JSON.stringify({{
            persistedStatus: global.localStorage._store.visStatusFilter,
            timestampsPressed: dom.get("toggle-timestamps").getAttribute("aria-pressed"),
            labelsPressed: dom.get("toggle-labels").getAttribute("aria-pressed"),
            expandPressed: dom.get("toggle-expand").getAttribute("aria-pressed"),
            fullWidthPressed: dom.get("toggle-fullwidth").getAttribute("aria-pressed"),
            seatPanelHidden: dom.get("seat-panel").hidden,
          }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["persistedStatus"] == "running"
    assert result["timestampsPressed"] == "true"
    assert result["labelsPressed"] == "true"
    assert result["expandPressed"] == "true"
    assert result["fullWidthPressed"] == "true"
    assert result["seatPanelHidden"] is True


def test_appjs_transcript_buffer_rerenders_on_toggle():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the transcript buffer/rerender contract test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        let orchestratorSse = null;
        let seatSse = null;
        global.fetch = (url, opts) => {{
          if (url === "/orchestrators") {{
            return Promise.resolve({{
              ok: true, status: 200,
              json: () => Promise.resolve({{ orchestrators: ["orch-1"] }}),
            }});
          }}
          if (url.startsWith("/sse/orchestrator/")) {{
            orchestratorSse = makeSseResponse(opts.signal);
            return Promise.resolve(orchestratorSse);
          }}
          if (url.startsWith("/sse/seat/")) {{
            seatSse = makeSseResponse(opts.signal);
            return Promise.resolve(seatSse);
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        function pushTranscript(id, ts, text) {{
          seatSse.push(
            "id: " + id + "\\nevent: transcript\\ndata: " +
            JSON.stringify({{ source: "transcript", ts: ts, kind: "model_text", content: text }}) +
            "\\n\\n"
          );
        }}

        (async () => {{
          await flush(10);
          orchestratorSse.push(
            "id: 1-0\\nevent: seat_appear\\ndata: " +
            JSON.stringify({{
              task_id: "seat-a", seat_id: "codex-1", run_id: "run-1",
              state: "running", last_event: "task_started",
              last_event_ts: new Date().toISOString(),
            }}) + "\\n\\n"
          );
          await flush(10);
          dom.get("seats").children[0].children[0].dispatchEvent("click");
          await flush(10);

          pushTranscript("1-0", "2026-06-25T10:00:00+00:00", "first line");
          pushTranscript("2-0", "2026-06-25T10:00:01+00:00", "second line");
          await flush(10);
          const beforeToggle = dom.get("timeline").textContent;

          dom.get("toggle-timestamps").dispatchEvent("click");
          const afterToggle = dom.get("timeline").textContent;

          pushTranscript("3-0", "2026-06-25T10:00:02+00:00", "third line");
          await flush(10);
          const afterNewFrame = dom.get("timeline").textContent;

          console.log(JSON.stringify({{ beforeToggle, afterToggle, afterNewFrame }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert "2026-06-25T10:00:00" not in result["beforeToggle"]
    assert "2026-06-25T10:00:01" not in result["beforeToggle"]
    assert "first line" in result["beforeToggle"]
    assert "second line" in result["beforeToggle"]
    assert "2026-06-25T10:00:00" in result["afterToggle"]
    assert "2026-06-25T10:00:01" in result["afterToggle"]
    assert "2026-06-25T10:00:02" in result["afterNewFrame"]


def test_appjs_seat_stream_error_survives_toggle_rerender():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the seat-stream-error survival contract test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        let orchestratorSse = null;
        let seatSse = null;
        global.fetch = (url, opts) => {{
          if (url === "/orchestrators") {{
            return Promise.resolve({{
              ok: true, status: 200,
              json: () => Promise.resolve({{ orchestrators: ["orch-1"] }}),
            }});
          }}
          if (url.startsWith("/sse/orchestrator/")) {{
            orchestratorSse = makeSseResponse(opts.signal);
            return Promise.resolve(orchestratorSse);
          }}
          if (url.startsWith("/sse/seat/")) {{
            seatSse = makeSseResponse(opts.signal);
            return Promise.resolve(seatSse);
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          orchestratorSse.push(
            "id: 1-0\\nevent: seat_appear\\ndata: " +
            JSON.stringify({{
              task_id: "seat-a", seat_id: "codex-1", run_id: "run-1",
              state: "running", last_event: "task_started",
              last_event_ts: new Date().toISOString(),
            }}) + "\\n\\n"
          );
          await flush(10);
          dom.get("seats").children[0].children[0].dispatchEvent("click");
          await flush(10);

          seatSse.push(
            "id: 1-0\\nevent: transcript\\ndata: " +
            JSON.stringify({{ source: "transcript", kind: "model_text", content: "before error" }}) +
            "\\n\\n"
          );
          await flush(10);

          seatSse.push(
            "id: 2-0\\nevent: error\\ndata: " +
            JSON.stringify({{ status: 502, message: "seat stream disconnected" }}) + "\\n\\n"
          );
          await flush(10);
          const beforeToggle = dom.get("timeline").textContent;

          dom.get("toggle-timestamps").dispatchEvent("click");
          const afterToggle = dom.get("timeline").textContent;

          console.log(JSON.stringify({{ beforeToggle, afterToggle }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert "seat stream disconnected" in result["beforeToggle"]
    assert "seat stream disconnected" in result["afterToggle"]


def test_appjs_eval_source_frames_are_lifecycle_noise_never_rendered_or_buffered():
    # Real-world shape: eval/audit "backfill" events are tee'd into the same seat SSE stream as
    # transcript content (source: "eval", not "transcript") — Go's isLifecycleNoise drops these
    # before ever reaching the transcript. Without the matching client-side filter, these showed
    # up as raw "[backfill] {...}" JSON dumps in the web transcript.
    node = _node()
    if node is None:
        raise AssertionError("node is required for the lifecycle-noise contract test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        let orchestratorSse = null;
        let seatSse = null;
        global.fetch = (url, opts) => {{
          if (url === "/orchestrators") {{
            return Promise.resolve({{
              ok: true, status: 200,
              json: () => Promise.resolve({{ orchestrators: ["orch-1"] }}),
            }});
          }}
          if (url.startsWith("/sse/orchestrator/")) {{
            orchestratorSse = makeSseResponse(opts.signal);
            return Promise.resolve(orchestratorSse);
          }}
          if (url.startsWith("/sse/seat/")) {{
            seatSse = makeSseResponse(opts.signal);
            return Promise.resolve(seatSse);
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          orchestratorSse.push(
            "id: 1-0\\nevent: seat_appear\\ndata: " +
            JSON.stringify({{
              task_id: "seat-a", seat_id: "codex-1", run_id: "run-1",
              state: "running", last_event: "task_started",
              last_event_ts: new Date().toISOString(),
            }}) + "\\n\\n"
          );
          await flush(10);
          dom.get("seats").children[0].children[0].dispatchEvent("click");
          await flush(10);

          seatSse.push(
            "id: 1-0\\nevent: backfill\\ndata: " +
            JSON.stringify({{
              source: "eval", run_id: "run-1", task_id: "seat-a", seat_id: "codex-1",
              event_type: "command_started", ts: "2026-07-03T20:31:56+00:00",
              payload: {{ exit_code: null }},
            }}) + "\\n\\n"
          );
          seatSse.push(
            "id: 2-0\\nevent: transcript\\ndata: " +
            JSON.stringify({{ source: "transcript", kind: "model_text", content: "real content" }}) +
            "\\n\\n"
          );
          await flush(10);
          const afterLive = dom.get("timeline").textContent;

          dom.get("toggle-timestamps").dispatchEvent("click");
          const afterToggle = dom.get("timeline").textContent;

          console.log(JSON.stringify({{ afterLive, afterToggle }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert "real content" in result["afterLive"]
    assert "backfill" not in result["afterLive"]
    assert "command_started" not in result["afterLive"]
    assert "real content" in result["afterToggle"]
    assert "backfill" not in result["afterToggle"]
    assert "command_started" not in result["afterToggle"]


def test_appjs_transcript_buffer_cleared_on_seat_and_orchestrator_switch():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the transcript-buffer-clearing regression test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        let orchestratorSse = null;
        let seatSse = null;
        global.fetch = (url, opts) => {{
          if (url === "/orchestrators") {{
            return Promise.resolve({{
              ok: true, status: 200,
              json: () => Promise.resolve({{ orchestrators: ["orch-1", "orch-2"] }}),
            }});
          }}
          if (url.startsWith("/sse/orchestrator/")) {{
            orchestratorSse = makeSseResponse(opts.signal);
            return Promise.resolve(orchestratorSse);
          }}
          if (url.startsWith("/sse/seat/")) {{
            seatSse = makeSseResponse(opts.signal);
            return Promise.resolve(seatSse);
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};

        function pushSeat(taskId, seatId) {{
          orchestratorSse.push(
            "id: 1-0\\nevent: seat_appear\\ndata: " +
            JSON.stringify({{
              task_id: taskId, seat_id: seatId, run_id: "run-" + taskId,
              state: "running", last_event: "task_started",
              last_event_ts: new Date().toISOString(),
            }}) + "\\n\\n"
          );
        }}

        function pushTranscript(text) {{
          seatSse.push(
            "id: 1-0\\nevent: transcript\\ndata: " +
            JSON.stringify({{ source: "transcript", kind: "model_text", content: text }}) + "\\n\\n"
          );
        }}

        function selectSeatByTaskId(taskId) {{
          const ids = seatTaskIds(dom);
          const idx = ids.indexOf(taskId);
          dom.get("seats").children[idx].children[0].dispatchEvent("click");
        }}

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          pushSeat("seat-a", "codex-1");
          await flush(10);
          selectSeatByTaskId("seat-a");
          await flush(10);
          pushTranscript("seat-a-content");
          await flush(10);

          pushSeat("seat-b", "codex-2");
          await flush(10);
          selectSeatByTaskId("seat-b");
          await flush(10);
          pushTranscript("seat-b-content");
          await flush(10);

          dom.get("toggle-timestamps").dispatchEvent("click");
          dom.get("toggle-timestamps").dispatchEvent("click");
          const afterSeatSwitch = dom.get("timeline").textContent;

          dom.get("orchestrator").value = "orch-2";
          dom.get("orchestrator").dispatchEvent("change");
          await flush(10);
          pushSeat("seat-c", "codex-3");
          await flush(10);
          selectSeatByTaskId("seat-c");
          await flush(10);
          pushTranscript("seat-c-content");
          await flush(10);

          dom.get("toggle-timestamps").dispatchEvent("click");
          dom.get("toggle-timestamps").dispatchEvent("click");
          const afterOrchestratorSwitch = dom.get("timeline").textContent;

          dom.get("clear-token").dispatchEvent("click");
          dom.get("toggle-timestamps").dispatchEvent("click");
          const afterClear = dom.get("timeline").textContent;

          console.log(JSON.stringify({{ afterSeatSwitch, afterOrchestratorSwitch, afterClear }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert "seat-b-content" in result["afterSeatSwitch"]
    assert "seat-a-content" not in result["afterSeatSwitch"]
    assert "seat-c-content" in result["afterOrchestratorSwitch"]
    assert "seat-b-content" not in result["afterOrchestratorSwitch"]
    assert "seat-a-content" not in result["afterOrchestratorSwitch"]
    assert result["afterClear"] == ""


def test_appjs_open_orchestrator_preserves_filters_on_first_call_resets_on_switch():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the openOrchestrator filter-reset contract test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        global.localStorage._store.visStatusFilter = "running";
        let orchestratorSse = null;
        global.fetch = (url, opts) => {{
          if (url === "/orchestrators") {{
            return Promise.resolve({{
              ok: true, status: 200,
              json: () => Promise.resolve({{ orchestrators: ["orch-1", "orch-2"] }}),
            }});
          }}
          if (url.startsWith("/sse/orchestrator/")) {{
            orchestratorSse = makeSseResponse(opts.signal);
            return Promise.resolve(orchestratorSse);
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          const afterFirstOpen = dom.get("status-filter").value;

          dom.get("orchestrator").value = "orch-2";
          dom.get("orchestrator").dispatchEvent("change");
          await flush(10);
          const afterSwitch = dom.get("status-filter").value;
          const persistedAfterSwitch = global.localStorage._store.visStatusFilter;

          console.log(JSON.stringify({{ afterFirstOpen, afterSwitch, persistedAfterSwitch }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["afterFirstOpen"] == "running"
    assert result["afterSwitch"] == "all"
    assert result["persistedAfterSwitch"] == "all"


def test_appjs_full_width_hides_seat_panel_and_blocks_fetch_burst():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the full-width fetch-suppression contract test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        dom.get("seats").scrollHeight = 100;
        dom.get("seats").clientHeight = 400;
        let orchestratorSse = null;
        const historyUrls = [];
        global.fetch = (url, opts) => {{
          if (url === "/orchestrators") {{
            return Promise.resolve({{
              ok: true, status: 200,
              json: () => Promise.resolve({{ orchestrators: ["orch-1"] }}),
            }});
          }}
          if (url.startsWith("/sse/orchestrator/")) {{
            orchestratorSse = makeSseResponse(opts.signal);
            return Promise.resolve(orchestratorSse);
          }}
          if (url.startsWith("/orchestrators/orch-1/seats/history")) {{
            historyUrls.push(url);
            const second = url.includes("cursor=cur-2");
            return Promise.resolve({{
              ok: true, status: 200,
              json: () => Promise.resolve(second ? {{
                seats: [],
                next_cursor: null,
                has_more: false,
              }} : {{
                seats: [
                  {{ task_id: "seat-a", seat_id: "hist-a", run_id: "run-a", state: "done", last_event: "task_finished", last_event_ts: "2026-06-25T12:00:00Z" }},
                ],
                next_cursor: "cur-2",
                has_more: true,
              }}),
            }});
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          dom.get("toggle-fullwidth").dispatchEvent("click");
          const hiddenAfterToggleOn = dom.get("seat-panel").hidden;

          dom.get("mode-history").dispatchEvent("click");
          await flush(10);
          const historyUrlCountWhileFullWidth = historyUrls.length;

          dom.get("toggle-fullwidth").dispatchEvent("click");
          const hiddenAfterToggleOff = dom.get("seat-panel").hidden;

          console.log(JSON.stringify({{
            hiddenAfterToggleOn, historyUrlCountWhileFullWidth, hiddenAfterToggleOff,
          }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["hiddenAfterToggleOn"] is True
    assert result["historyUrlCountWhileFullWidth"] == 1
    assert result["hiddenAfterToggleOff"] is False


def test_appjs_history_toggle_fetches_and_paginates_without_live_rows_or_duplicates():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the history toggle regression test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        dom.get("seats").scrollHeight = 800;
        dom.get("seats").clientHeight = 400;
        let orchestratorSse = null;
        const historyUrls = [];
        global.fetch = (url, opts) => {{
          if (url === "/orchestrators") {{
            return Promise.resolve({{
              ok: true, status: 200,
              json: () => Promise.resolve({{ orchestrators: ["orch-1"] }}),
            }});
          }}
          if (url.startsWith("/sse/orchestrator/")) {{
            orchestratorSse = makeSseResponse(opts.signal);
            return Promise.resolve(orchestratorSse);
          }}
          if (url.startsWith("/orchestrators/orch-1/seats/history")) {{
            historyUrls.push(url);
            const second = url.includes("cursor=cur-2");
            return Promise.resolve({{
              ok: true,
              status: 200,
              json: () => Promise.resolve(second ? {{
                seats: [
                  {{ task_id: "seat-a", seat_id: "hist-a-duplicate", run_id: "run-a2", state: "done", last_event: "task_finished", last_event_ts: "2026-06-24T12:00:00Z" }},
                  {{ task_id: "seat-c", seat_id: "hist-c", run_id: "run-c", state: "failed", last_event: "task_finished", last_event_ts: "2026-06-26T12:00:00Z" }},
                ],
                next_cursor: null,
                has_more: false,
              }} : {{
                seats: [
                  {{ task_id: "seat-a", seat_id: "hist-a", run_id: "run-a", state: "done", last_event: "task_finished", last_event_ts: "2026-06-25T12:00:00Z" }},
                ],
                next_cursor: "cur-2",
                has_more: true,
              }}),
            }});
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          dom.get("mode-history").dispatchEvent("click");
          const loadingText = dom.get("history-status").textContent;
          orchestratorSse.push(
            "id: 1-0\\nevent: seat_appear\\ndata: " +
            JSON.stringify({{
              task_id: "live-only", seat_id: "live-only", run_id: "run-live",
              state: "running", last_event: "task_started",
              last_event_ts: "2026-06-27T00:00:00Z",
            }}) + "\\n\\n"
          );
          await flush(10);
          const afterFirstPage = seatTaskIds(dom);
          const anchorRowsInHistory = dom.get("seats-orchestrator").children.length;
          const loadedText = dom.get("history-status").textContent;

          dom.get("seats").scrollTop = 360;
          dom.get("seats").dispatchEvent("scroll");
          await flush(10);
          const afterSecondPage = seatTaskIds(dom);

          console.log(JSON.stringify({{
            loadingText,
            loadedText,
            afterFirstPage,
            anchorRowsInHistory,
            afterSecondPage,
            historyUrls,
          }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["loadingText"] == "· history — loading…"
    assert result["loadedText"] == "· history"
    assert result["afterFirstPage"] == ["seat-a"]
    assert result["anchorRowsInHistory"] == 0
    assert "live-only" not in result["afterSecondPage"]
    assert sorted(result["afterSecondPage"]) == ["seat-a", "seat-c"]
    assert len(result["afterSecondPage"]) == len(set(result["afterSecondPage"]))
    assert result["historyUrls"] == [
        "/orchestrators/orch-1/seats/history",
        "/orchestrators/orch-1/seats/history?cursor=cur-2",
    ]


def test_appjs_history_date_filter_reloads_from_server_and_clearing_removes_date():
    node = _node()
    if node is None:
        raise AssertionError("node is required for the history date-filter server contract test")
    script = _DOM_HARNESS_JS + textwrap.dedent(
        f"""
        const dom = makeDom();
        dom.get("seats").scrollHeight = 800;
        dom.get("seats").clientHeight = 400;
        const historyUrls = [];
        global.fetch = (url, opts) => {{
          if (url === "/orchestrators") {{
            return Promise.resolve({{
              ok: true, status: 200,
              json: () => Promise.resolve({{ orchestrators: ["orch-1"] }}),
            }});
          }}
          if (url.startsWith("/sse/orchestrator/")) {{
            return Promise.resolve(makeSseResponse(opts.signal));
          }}
          if (url.startsWith("/orchestrators/orch-1/seats/history")) {{
            historyUrls.push(url);
            const second = url.includes("cursor=cur-2");
            return Promise.resolve({{
              ok: true,
              status: 200,
              json: () => Promise.resolve(second ? {{
                seats: [],
                next_cursor: null,
                has_more: false,
              }} : {{
                seats: [
                  {{ task_id: "seat-a", seat_id: "hist-a", run_id: "run-a", state: "done", last_event: "task_finished", last_event_ts: "2026-07-04T12:00:00Z" }},
                ],
                next_cursor: "cur-2",
                has_more: true,
              }}),
            }});
          }}
          return Promise.reject(new Error("unexpected fetch " + url));
        }};

        require({json.dumps(str(APP_JS))});
        dom.ready()();

        (async () => {{
          await flush(10);
          dom.get("mode-history").dispatchEvent("click");
          await flush(10);
          dom.get("date-filter").value = "2026-07-04";
          dom.get("date-filter").dispatchEvent("change");
          await flush(10);
          dom.get("seats").scrollTop = 360;
          dom.get("seats").dispatchEvent("scroll");
          await flush(10);
          dom.get("date-filter").value = "";
          dom.get("date-filter").dispatchEvent("change");
          await flush(10);

          console.log(JSON.stringify({{ historyUrls }}));
        }})();
        """
    )
    completed = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    result = json.loads(completed.stdout)
    assert result["historyUrls"] == [
        "/orchestrators/orch-1/seats/history",
        "/orchestrators/orch-1/seats/history?date=2026-07-04",
        "/orchestrators/orch-1/seats/history?cursor=cur-2",
        "/orchestrators/orch-1/seats/history",
    ]


def test_index_html_head_gets_the_door_page_font_link_pair():
    html = INDEX_HTML.read_text()
    assert '<link rel="preconnect" href="https://fonts.googleapis.com">' in html
    assert '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>' in html
    assert (
        '<link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;'
        '6..72,500;6..72,600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">'
    ) in html


def test_index_html_body_has_mode_toggle_auth_banner_and_history_status():
    html = INDEX_HTML.read_text()
    assert "<h1>ARB · Visibility</h1>" in html
    assert '<div class="segmented" role="group" aria-label="Seat source">' in html
    assert '<button id="mode-live" type="button" aria-pressed="true" disabled>Live</button>' in html
    assert '<button id="mode-history" type="button" aria-pressed="false" disabled>History</button>' in html
    assert '<div id="auth-banner" hidden role="alert">' in html
    assert "Session expired — redirecting to sign in…" in html
    assert "check your token" not in html
    assert '<aside id="seat-panel">' in html
    assert '<ul id="seats-orchestrator"></ul>' in html
    assert '<div id="history-status" aria-live="polite"></div>' in html


def test_index_html_filter_bar_markup_and_css():
    html = INDEX_HTML.read_text()
    assert '<div id="filter-bar">' in html
    assert '<select id="status-filter">' in html
    for value in ("all", "running", "incomplete", "done", "failed", "voted", "stale", "unknown"):
        assert f'<option value="{value}">{value}</option>' in html
    status_order = [
        html.index(f'<option value="{v}">{v}</option>')
        for v in ("all", "running", "incomplete", "done", "failed", "voted", "stale", "unknown")
    ]
    assert status_order == sorted(status_order)
    assert '<select id="agent-filter">' in html
    assert '<option value="all">all</option>' in html
    assert '<span id="filter-count">' in html
    # History-only date filter: hidden by default, styled like the selects, with a [hidden] override
    # so it actually hides (the #filter-bar label ID selector would otherwise beat native [hidden]).
    assert '<label id="date-filter-label" hidden>' in html
    assert '<input type="date" id="date-filter">' in html
    for selector in (
        "#filter-bar{",
        "#filter-bar label{",
        "#filter-bar label[hidden]{ display:none; }",
        '#filter-bar select, #filter-bar input[type="date"]{',
        '#filter-bar select:focus, #filter-bar input[type="date"]:focus{',
        "#filter-count{",
    ):
        assert selector in html, f"missing selector/rule: {selector!r}"


def test_index_html_transcript_toolbar_markup_and_css():
    html = INDEX_HTML.read_text()
    toolbar_idx = html.index('<div id="transcript-toolbar">')
    timeline_idx = html.index('<pre id="timeline">')
    assert toolbar_idx < timeline_idx
    assert html.index("<section>") < toolbar_idx < html.index("</section>")
    for button_id, label in (
        ("toggle-timestamps", "Timestamps"),
        ("toggle-labels", "Labels"),
        ("toggle-expand", "Expand output"),
        ("toggle-fullwidth", "Full width"),
    ):
        assert f'<button id="{button_id}" type="button" aria-pressed="false">{label}</button>' in html
        assert f'id="{button_id}" type="button" aria-pressed="false" disabled' not in html
    for selector in (
        "#transcript-toolbar{",
        "#transcript-toolbar button{",
        '#transcript-toolbar button[aria-pressed="true"]{',
        ".dim{",
    ):
        assert selector in html, f"missing selector/rule: {selector!r}"


def test_index_html_css_layout_restructure():
    html = INDEX_HTML.read_text()
    assert "calc(100vh - 73px)" not in html
    assert "html, body{ height:100%; }" in html
    assert html.count("flex:1 1 auto; min-height:0;") >= 2
    assert html.count("display:flex; flex-direction:column;") >= 2


def test_index_html_fullwidth_hidden_seat_panel_expands_section():
    html = INDEX_HTML.read_text()
    assert "aside#seat-panel[hidden] + section{ grid-column:1 / -1; }" in html


def test_index_html_seat_panel_and_section_confine_scroll_to_own_pane():
    # Grid items default to min-height:auto (sized to fit content) unless overridden — without
    # min-height:0 on BOTH direct children of `main`, either pane growing past the viewport
    # (many seats, or a long transcript) drags the whole page into scroll, taking the sticky
    # header and the other pane along with it. Both panes already have overflow:auto on the
    # right element; this is what lets that overflow:auto actually take effect instead of never
    # being reached.
    html = INDEX_HTML.read_text()
    aside_rule = re.search(r"aside#seat-panel\{([^}]*)\}", html)
    seats_rule = re.search(r"#seats\{([^}]*)\}", html)
    anchor_rule = re.search(r"#seats-orchestrator\{([^}]*)\}", html)
    section_rule = re.search(r"\n\s*section\{([^}]*)\}", html)
    assert aside_rule and "min-height:0" in aside_rule.group(1)
    assert aside_rule and "overflow:hidden" in aside_rule.group(1)
    assert seats_rule and "flex:1 1 auto" in seats_rule.group(1)
    assert seats_rule and "min-height:0" in seats_rule.group(1)
    assert seats_rule and "overflow-y:auto" in seats_rule.group(1)
    assert anchor_rule and "flex:0 0 auto" in anchor_rule.group(1)
    assert section_rule and "min-height:0" in section_rule.group(1)


def test_index_html_css_root_tokens_light_and_dark():
    html = INDEX_HTML.read_text()
    assert "--paper:#FAF8F4; --paper-sunk:#F3EFE8; --card:#FFFFFF;" in html
    assert "--ink-900:#1F1D1A; --ink-700:#45413B; --ink-500:#6E6960; --ink-400:#918B80;" in html
    assert "--clay-600:#9E4A2E; --clay-700:#823A22; --clay-100:#EFE0D6;" in html
    assert "@media (prefers-color-scheme: dark)" in html
    assert html.count("--ink-500:#A8A299;") == 1
    assert "--ink-500:#918B80;" not in html
    assert "--paper:#1F1D1A; --paper-sunk:#28251F; --card:#322E27;" in html
    assert "--clay-600:#C97350; --clay-700:#E0916B; --clay-100:#3D2A20;" in html
    assert "color-scheme: light dark" not in html


def test_index_html_css_key_selectors_present():
    html = INDEX_HTML.read_text()
    for selector in (
        ".segmented", ".segmented button", '.segmented button[aria-pressed="true"]',
        "#auth-banner", "#auth-banner[hidden]",
        ".state-cell", ".badge", '.badge[data-state="stale"]', '.badge[data-state="failed"]',
        ".pulse-dot", "@keyframes seat-pulse", "@media (prefers-reduced-motion: reduce)",
        "#seats-orchestrator", ".orchestrator-divider", "#history-status", "#history-status:empty",
        "grid-template-columns: minmax(140px,1fr) minmax(260px,auto) auto auto",
        ".orchestrator-field{",
        ".orchestrator-field select{",
    ):
        assert selector in html, f"missing selector/rule: {selector!r}"


def test_index_html_header_hides_token_and_inlines_orchestrator_label():
    html = INDEX_HTML.read_text()
    assert '<input id="token" type="hidden" autocomplete="off">' in html
    assert "<label>\n        Token" not in html
    assert '<label class="orchestrator-field">' in html
    assert html.index('<label class="orchestrator-field">') < html.index('<select id="orchestrator">')


def test_index_html_seat_panel_hidden_display_override():
    html = INDEX_HTML.read_text()
    normalized = "".join(html.split())
    assert "aside#seat-panel[hidden]{display:none;}" in normalized
