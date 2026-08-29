package main

import (
	"context"
	"encoding/base64"
	"fmt"
	"net/url"
	"os"
	"os/exec"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

const (
	viewOrchestrators = "orchestrators"
	viewSeats         = "seats"
	baseBackoff       = 500 * time.Millisecond
	maxBackoff        = 5 * time.Second
)

// Lip Gloss styles — the variable under test (this is the looks-comparison spike).
var (
	styleBullet    = lipgloss.NewStyle().Foreground(lipgloss.Color("42"))               // green ⏺ (model text)
	styleText      = lipgloss.NewStyle().Foreground(lipgloss.Color("252"))              // bright body text
	styleCmd       = lipgloss.NewStyle().Foreground(lipgloss.Color("39"))               // cyan (tool call)
	styleOutput    = lipgloss.NewStyle().Foreground(lipgloss.Color("245"))              // dim (⎿ output)
	styleThinking  = lipgloss.NewStyle().Foreground(lipgloss.Color("245")).Italic(true) // dim italic
	stylePatch     = lipgloss.NewStyle().Foreground(lipgloss.Color("170"))              // magenta (apply_patch)
	accent         = lipgloss.Color("#0178D4")                                          // Textual default theme $accent
	styleHeaderBar = lipgloss.NewStyle().Background(accent).Foreground(lipgloss.Color("231")).Bold(true).Padding(0, 2)
	styleTopBar    = lipgloss.NewStyle().Background(lipgloss.Color("236")).Foreground(lipgloss.Color("231")).Bold(true).Padding(0, 1)
	styleSubTitle  = lipgloss.NewStyle().Background(lipgloss.Color("236")).Foreground(lipgloss.Color("250"))
	stylePane      = lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(lipgloss.Color("240"))
	stylePaneFocus = lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(accent) // active pane
	styleTitle     = lipgloss.NewStyle().Foreground(accent).Bold(true)
	styleColHdr    = lipgloss.NewStyle().Foreground(lipgloss.Color("244")).Bold(true)
	styleCursor    = lipgloss.NewStyle().Background(lipgloss.Color("237")).Foreground(lipgloss.Color("231")).Bold(true)
	styleHint      = lipgloss.NewStyle().Foreground(lipgloss.Color("244"))
	styleDim       = lipgloss.NewStyle().Foreground(lipgloss.Color("241"))
	styleLabel     = lipgloss.NewStyle().Foreground(lipgloss.Color("37")) // dim cyan (matches Python labels)
	styleFootKey   = lipgloss.NewStyle().Background(accent).Foreground(lipgloss.Color("231")).Bold(true)
	styleFootDesc  = lipgloss.NewStyle().Foreground(lipgloss.Color("250"))
	styleFilterBox = lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(lipgloss.Color("240")).Padding(0, 1)
	styleFilterVal = lipgloss.NewStyle().Foreground(accent).Bold(true)
	styleLineNo    = lipgloss.NewStyle().Foreground(lipgloss.Color("240")) // copy-mode line-number gutter
	styleCopyBar   = lipgloss.NewStyle().Background(accent).Foreground(lipgloss.Color("231")).Bold(true).Padding(0, 1)
)

// keyBindings is shown in the footer. Mirrors the Python BINDINGS plus Go-side additions:
// 'e' (expand output), 's'/'a' (cycle the seat-pane status/agent filters).
var keyBindings = []struct{ key, desc string }{
	{"q", "Quit"}, {"←→", "Focus pane"}, {"c", "Copy transcript"}, {"^C", "Copy lines"},
	{"t", "Timestamps"}, {"l", "Labels"}, {"e", "Expand output"}, {"s", "Status filter"},
	{"a", "Agent filter"}, {"h", "History"}, {"f", "Full width"}, {"m", "Fleet menu"},
}

// filterAll is the "no filter" sentinel for the seat-pane status/agent filters.
const filterAll = "all"

// statusCycle is the fixed order the 's' key walks the status filter through.
var statusCycle = []string{filterAll, "running", "incomplete", "done", "failed", "voted", "stale"}

var stateColors = map[string]lipgloss.Color{
	"running": "42", "incomplete": "141", "done": "244", "failed": "203", "voted": "39", "stale": "208",
}

// styledTranscriptLine applies per-kind Lip Gloss colour to the plain render output
// (renderTranscriptLine stays plain for the golden parity tests).
func styledTranscriptLine(data map[string]any) string {
	plain := renderTranscriptLine(data)
	kind := getString(data, "kind")
	tool := getString(data, "tool_name")
	switch {
	case kind == "model_thinking":
		return styleThinking.Render(plain)
	case kind == "command_output" || kind == "command_finished" || kind == "tool_output":
		return styleOutput.Render(plain)
	case tool == "apply_patch":
		return stylePatch.Render(plain)
	case kind == "command_started" || kind == "tool_call":
		return styleCmd.Render(plain)
	default: // model_text + fallback: green ⏺ + bright body
		if body, ok := strings.CutPrefix(plain, "⏺ "); ok {
			return styleBullet.Render("⏺") + " " + styleText.Render(body)
		}
		return styleText.Render(plain)
	}
}

// Two concurrent SSE streams, exactly like the Python control: the orchestrator stream
// (the live seat list) keeps running while the seat stream (the transcript) is switched —
// so drilling into a seat does NOT freeze the seat list.
const (
	streamOrch = "orch"
	streamSeat = "seat"
)

// Pane focus: which pane the arrow keys drive. Left pane = seat list, right pane = transcript.
const (
	focusSeats      = "seats"
	focusTranscript = "transcript"
)

type streamState struct {
	cancel  context.CancelFunc
	ch      chan Frame
	gen     int
	url     string
	lastID  string
	backoff time.Duration
	fatal   bool // a 4xx (auth/not-found) closed the stream — do not reconnect (matches Python)
}

// stop cancels the stream and invalidates any in-flight listen (gen bump) so no late
// frame or reconnect for it can mutate state.
func (s *streamState) stop() {
	if s.cancel != nil {
		s.cancel()
		s.cancel = nil
	}
	s.gen++
	s.url = ""
	s.lastID = ""
	s.backoff = baseBackoff
	s.fatal = false
}

type model struct {
	baseURL      string
	token        string
	orchestrator string
	view         string

	orchestrators []string
	orchCursor    int
	seats         map[string]map[string]any
	seatOrder     []string
	seatCursor    int
	seatScroll    int // first visible row of the seat list when it exceeds the pane height
	selectedTask  string
	vp            viewport.Model
	transcript    []map[string]any // raw frame data, re-rendered on toggle (matches Python timeline_events)
	subTitle      string

	showTimestamps bool
	showLabels     bool
	fullscreen     bool
	expanded       bool // tool output truncated by default; 'e' expands it

	statusFilter string // "all" or a seat state; 's' cycles
	agentFilter  string // "all" or an agent (codex/agy/pi/…); 'a' cycles

	copyMode  bool   // ctrl+c: line-numbered transcript + range-input overlay
	copyInput string // the line range being typed, e.g. "10-20"
	focus     string // which pane the arrow keys drive: focusSeats (left) | focusTranscript (right)

	orch streamState // the seat list (kept live while a seat is open)
	seat streamState // the selected seat's transcript

	seatSource     string // "live" | "history"
	historyCursor  string
	historyHasMore bool
	historyLoading bool
	historyGen     int

	width  int
	height int
	status string

	startStream func(m *model, which, url, lastID string) tea.Cmd
}

func (m *model) stream(which string) *streamState {
	if which == streamSeat {
		return &m.seat
	}
	return &m.orch
}

type orchestratorsMsg []string
type frameMsg struct {
	which string
	gen   int
	f     Frame
}
type streamClosedMsg struct {
	which string
	gen   int
}
type reconnectMsg struct {
	which string
	gen   int
}
type tickMsg time.Time
type historyPageMsg struct {
	gen        int
	seats      []map[string]any
	nextCursor string
	hasMore    bool
	append     bool
}
type historyErrMsg struct {
	gen int
	err error
}
type errMsg error

func newModel(baseURL, token, orchestrator string) *model {
	m := &model{
		baseURL:      strings.TrimRight(baseURL, "/"),
		token:        token,
		orchestrator: orchestrator,
		view:         viewOrchestrators,
		seats:        map[string]map[string]any{},
		vp:           viewport.New(80, 20),
		orch:         streamState{backoff: baseBackoff},
		seat:         streamState{backoff: baseBackoff},
		seatSource:   "live",
		statusFilter: filterAll,
		agentFilter:  filterAll,
		focus:        focusSeats,
	}
	m.startStream = realStartStream
	if orchestrator != "" {
		m.view = viewSeats
	}
	return m
}

func (m *model) Init() tea.Cmd {
	if m.orchestrator != "" {
		return tea.Batch(m.enterSeats(m.orchestrator), tickCmd())
	}
	return tea.Batch(fetchOrchestratorsCmd(m.baseURL, m.token), tickCmd())
}

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case orchestratorsMsg:
		m.orchestrators = []string(msg)
		m.view = viewOrchestrators
		if m.orchCursor >= len(m.orchestrators) {
			m.orchCursor = max(0, len(m.orchestrators)-1)
		}
		return m, nil
	case frameMsg:
		s := m.stream(msg.which)
		if msg.gen != s.gen { // stale stream (switched since) — drop
			return m, nil
		}
		if msg.f.Event == streamFatalEvent {
			// 4xx — mark fatal so the close handler won't reconnect, and surface it.
			s.fatal = true
			m.status = fmt.Sprintf("%s stream stopped: HTTP %s (check token / orchestrator)", msg.which, statusOf(msg.f))
			return m, nil // stop listening; the channel close that follows won't reconnect
		}
		if isResumableID(msg.f.ID) {
			s.lastID = msg.f.ID
		}
		s.backoff = baseBackoff
		if msg.which == streamOrch {
			if m.seatSource == "live" {
				m.upsertSeat(msg.f)
				m.syncCursorToSelection()
			}
		} else if m.view == viewSeats {
			m.appendTranscript(msg.f)
		}
		return m, listen(s.ch, msg.which, msg.gen)
	case streamClosedMsg:
		s := m.stream(msg.which)
		if msg.gen != s.gen || s.url == "" || s.fatal {
			return m, nil // stale, stopped, or fatal (4xx) — do not reconnect
		}
		delay := s.backoff
		s.backoff *= 2
		if s.backoff > maxBackoff {
			s.backoff = maxBackoff
		}
		if msg.which == streamSeat {
			m.status = fmt.Sprintf("transcript disconnected; reconnecting in %s", delay)
		}
		// Gate the reconnect behind the backoff (don't busy-spin against a down gateway).
		which, gen := msg.which, s.gen
		return m, tea.Tick(delay, func(time.Time) tea.Msg { return reconnectMsg{which, gen} })
	case reconnectMsg:
		s := m.stream(msg.which)
		if msg.gen != s.gen || s.url == "" {
			return m, nil // stream was switched (drill-in / menu) — stale reconnect, drop it
		}
		return m, m.startStream(m, msg.which, s.url, s.lastID)
	case tickMsg:
		return m, tickCmd()
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.resizeViewport()
		m.rerenderTranscript() // re-wrap to the new width
		return m, nil
	case tea.KeyMsg:
		return m.handleKey(msg)
	case tea.MouseMsg:
		// Forward the scroll wheel to the transcript viewport (it handles wheel up/down),
		// so the wheel scrolls the transcript rather than the terminal scrolling the app.
		if m.view == viewSeats {
			var cmd tea.Cmd
			m.vp, cmd = m.vp.Update(msg)
			return m, cmd
		}
		return m, nil
	case historyPageMsg:
		if msg.gen != m.historyGen {
			return m, nil
		}
		m.historyLoading = false
		if msg.append {
			m.appendHistoryPage(msg.seats)
		} else {
			m.replaceSeatsWithHistory(msg.seats)
		}
		m.historyCursor = msg.nextCursor
		m.historyHasMore = msg.hasMore
		m.clampSeatCursor()
		return m, nil
	case historyErrMsg:
		if msg.gen != m.historyGen {
			return m, nil
		}
		m.historyLoading = false
		m.status = msg.err.Error()
		return m, nil
	case errMsg:
		m.status = error(msg).Error()
		return m, nil
	}
	return m, nil
}

func (m *model) View() string {
	if m.view == viewOrchestrators {
		return m.renderOrchestrators()
	}
	var body string
	paneH := m.vp.Height + 2 // header row + margin row + viewport, inside the border
	if m.view == viewOrchestrators {
		body = m.renderOrchestrators()
	} else {
		left := m.width * 40 / 100
		if left < 28 {
			left = 28
		}
		detailW := m.width - 2
		if !m.fullscreen {
			detailW = m.width - left - 2
		}
		// Highlight the active pane's border (accent) vs the inactive (dim).
		seatStyle, detailStyle := stylePane, stylePane
		if m.transcriptActive() {
			detailStyle = stylePaneFocus
		} else {
			seatStyle = stylePaneFocus
		}
		detail := detailStyle.Width(detailW).Height(paneH).Render(lipgloss.JoinVertical(lipgloss.Left, m.renderHeader(detailW), m.vp.View()))
		if m.fullscreen {
			body = detail
		} else {
			seatInner := lipgloss.JoinVertical(lipgloss.Left, m.renderFilterBar(left-4), m.renderSeatTable())
			// Pin the pane content to exactly its height (paneH minus the bottom padding) so it
			// can never overflow and grow the bordered box — the belt to seatViewportRows's braces.
			seatInner = normalizeHeight(seatInner, paneH-1)
			// PaddingBottom keeps the last row off the bottom border (a little breathing room).
			seatPane := seatStyle.Width(left - 2).Height(paneH).PaddingBottom(1).Render(seatInner)
			body = lipgloss.JoinHorizontal(lipgloss.Top, seatPane, detail)
		}
	}
	foot := m.renderFooter()
	if m.copyMode {
		foot = m.renderCopyPrompt()
	}
	return lipgloss.JoinVertical(lipgloss.Left, m.renderTopBar(), body, foot)
}

// renderCopyPrompt is the range-input overlay shown in place of the footer in copy mode.
func (m *model) renderCopyPrompt() string {
	total := len(m.transcriptStyledLines())
	bar := styleCopyBar.Render(fmt.Sprintf("Copy lines (1-%d): %s▌", total, m.copyInput))
	hint := styleFootDesc.Render("  Enter copy · Esc cancel · ↑↓ scroll · ⇧↑↓ page")
	line := bar + hint
	if m.width > lipgloss.Width(line) {
		line += styleFootDesc.Render(strings.Repeat(" ", m.width-lipgloss.Width(line)))
	}
	return line
}

// resizeViewport sizes the transcript pane: 66% width (34% seat list) normally, full
// width in fullscreen; height = total minus top bar, footer, pane border, and header.
func (m *model) resizeViewport() {
	if m.width == 0 {
		return
	}
	inner := m.width - 4 // detail border (2) + padding (2)
	if !m.fullscreen {
		left := m.width * 40 / 100
		if left < 28 {
			left = 28
		}
		inner = m.width - left - 4
	}
	m.vp.Width = max(20, inner)
	m.vp.Height = max(3, m.height-7) // top bar + footer (2) + pane border (2) + header (1) + header margin (1) + slack
}

// renderTopBar mirrors Textual's Header: the app title + the live sub_title.
func (m *model) renderTopBar() string {
	bar := styleTopBar.Render("arb-watch")
	if m.subTitle != "" {
		bar = lipgloss.JoinHorizontal(lipgloss.Top, bar, styleSubTitle.Render("  "+m.subTitle))
	}
	if m.width > lipgloss.Width(bar) {
		bar += styleSubTitle.Render(strings.Repeat(" ", m.width-lipgloss.Width(bar)))
	}
	return bar
}

// renderFooter mirrors Textual's Footer: each binding as a highlighted key + description.
func (m *model) renderFooter() string {
	var parts []string
	for _, b := range keyBindings {
		parts = append(parts, styleFootKey.Render(" "+b.key+" ")+styleFootDesc.Render(" "+b.desc+" "))
	}
	footer := strings.Join(parts, "")
	if m.width > lipgloss.Width(footer) {
		footer += styleFootDesc.Render(strings.Repeat(" ", m.width-lipgloss.Width(footer)))
	}
	return footer
}

func (m *model) handleKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	if m.copyMode {
		return m.handleCopyKey(msg)
	}
	switch msg.String() {
	case "q":
		return m, tea.Quit
	case "ctrl+c":
		m.enterCopyMode() // line-numbered copy-range overlay (no-op without a transcript)
		return m, nil
	case "left":
		if m.view == viewSeats {
			m.focus = focusSeats // focus the seat list; up/down navigate seats again
		}
	case "right":
		if m.view == viewSeats {
			var cmd tea.Cmd
			if m.selectedTask == "" {
				cmd = m.autoSelectSeat() // open the cursor seat so there's a transcript to focus
			}
			if m.selectedTask != "" {
				m.focus = focusTranscript // up/down now scroll the transcript
			}
			return m, cmd
		}
	case "up":
		if m.view == viewOrchestrators {
			if m.orchCursor > 0 {
				m.orchCursor--
			}
		} else if m.transcriptActive() {
			m.vp.LineUp(1) // right pane focused → scroll the transcript
		} else if len(m.visibleSeats()) > 0 {
			if m.seatCursor > 0 {
				m.seatCursor--
			}
			m.clampSeatScroll()
			return m, m.autoSelectSeat() // auto-load transcript, no enter needed
		}
	case "down":
		if m.view == viewOrchestrators {
			if m.orchCursor < len(m.orchestrators)-1 {
				m.orchCursor++
			}
		} else if m.transcriptActive() {
			m.vp.LineDown(1)
		} else if vis := m.visibleSeats(); len(vis) > 0 {
			if m.seatCursor < len(vis)-1 {
				m.seatCursor++
			}
			m.clampSeatScroll()
			return m, tea.Batch(m.autoSelectSeat(), m.maybeFetchNextHistoryPage()) // auto-load transcript, no enter needed
		}
	case "shift+up", "pgup":
		if m.transcriptActive() {
			m.vp.ViewUp() // page up (shift+arrows for keyboards without PgUp/PgDn)
		} else if vis := m.visibleSeats(); len(vis) > 0 { // left pane focused → page the seat list
			m.seatCursor -= max(1, m.seatViewportRows()-1)
			if m.seatCursor < 0 {
				m.seatCursor = 0
			}
			m.clampSeatScroll()
			return m, m.autoSelectSeat()
		}
	case "shift+down", "pgdown":
		if m.transcriptActive() {
			m.vp.ViewDown()
		} else if vis := m.visibleSeats(); len(vis) > 0 {
			m.seatCursor += max(1, m.seatViewportRows()-1)
			if m.seatCursor > len(vis)-1 {
				m.seatCursor = len(vis) - 1
			}
			m.clampSeatScroll()
			return m, tea.Batch(m.autoSelectSeat(), m.maybeFetchNextHistoryPage())
		}
	case "enter":
		if m.view == viewOrchestrators && len(m.orchestrators) > 0 {
			return m, m.enterSeats(m.orchestrators[m.orchCursor])
		}
		if vis := m.visibleSeats(); m.view == viewSeats && m.seatCursor < len(vis) {
			return m, m.enterSeat(vis[m.seatCursor])
		}
	case "s":
		if m.view == viewSeats {
			m.statusFilter = cycleNext(statusCycle, m.statusFilter)
			m.clampSeatCursor()
			m.status = "status filter: " + m.statusFilter
		}
	case "a":
		if m.view == viewSeats {
			m.agentFilter = cycleNext(m.agentCycle(), m.agentFilter)
			m.clampSeatCursor()
			m.status = "agent filter: " + m.agentFilter
		}
	case "h":
		if m.view == viewSeats {
			return m, m.toggleHistoryMode()
		}
	case "m":
		return m, m.enterMenu()
	case "t":
		m.showTimestamps = !m.showTimestamps
		m.renderTranscript()
		m.status = "timestamps " + onOff(m.showTimestamps)
	case "l":
		m.showLabels = !m.showLabels
		m.renderTranscript()
		m.status = "labels " + onOff(m.showLabels)
	case "e":
		m.expanded = !m.expanded
		m.renderTranscript()
		if m.expanded {
			m.status = "tool output expanded"
		} else {
			m.status = "tool output truncated"
		}
	case "f":
		m.fullscreen = !m.fullscreen
		m.resizeViewport()
		m.rerenderTranscript() // re-wrap to the new pane width
		m.status = "full-width transcript " + onOff(m.fullscreen)
	case "c":
		if m.view == viewSeats && m.selectedTask != "" && len(m.transcript) > 0 {
			m.status = fmt.Sprintf("copied transcript (%d lines)", len(m.transcript))
			return m, copyToClipboard(m.transcriptPlain())
		}
		m.status = "no transcript selected — pick a seat first" // matches Python's warning
	}
	return m, nil
}

// handleCopyKey routes keys while the copy-range overlay is open: digits/'-' edit the range,
// Enter copies, Esc/Ctrl+C cancels, and the scroll keys still drive the transcript viewport
// (so the right pane stays scrollable while you pick a range).
func (m *model) handleCopyKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch msg.String() {
	case "esc", "ctrl+c":
		m.exitCopyMode()
	case "enter":
		return m, m.copyRange()
	case "backspace":
		if len(m.copyInput) > 0 {
			m.copyInput = m.copyInput[:len(m.copyInput)-1]
		}
	case "up", "down", "home", "end":
		var cmd tea.Cmd
		m.vp, cmd = m.vp.Update(msg) // keep the transcript scrollable under the overlay
		return m, cmd
	case "shift+up", "pgup":
		m.vp.ViewUp()
	case "shift+down", "pgdown":
		m.vp.ViewDown()
	default:
		if s := msg.String(); len(s) == 1 && (s[0] >= '0' && s[0] <= '9' || s[0] == '-') {
			m.copyInput += s
		}
	}
	return m, nil
}

// enterCopyMode shows the transcript line-numbered (fully expanded, so every line is pickable)
// and opens the range overlay. No-op without a selected seat's transcript.
func (m *model) enterCopyMode() {
	if m.view != viewSeats || m.selectedTask == "" || len(m.transcript) == 0 {
		m.status = "no transcript to copy — pick a seat first"
		return
	}
	m.copyMode = true
	m.copyInput = ""
	m.status = ""
	m.renderTranscriptNumbered()
}

func (m *model) exitCopyMode() {
	m.copyMode = false
	m.copyInput = ""
	m.renderTranscript() // back to the normal (collapsed-aware) view
	m.vp.GotoBottom()
}

// copyRange copies the plain text of the requested visual line range (1-indexed, inclusive).
func (m *model) copyRange() tea.Cmd {
	text, a, b, ok := m.selectedCopyText()
	if !ok {
		m.status = "invalid range — e.g. 10-20, 12, or 10- (to end)"
		return nil
	}
	m.exitCopyMode()
	m.status = fmt.Sprintf("copied lines %d-%d (%d line(s))", a, b, b-a+1)
	return copyToClipboard(text)
}

// selectedCopyText resolves copyInput against the fully-expanded plain visual lines.
func (m *model) selectedCopyText() (string, int, int, bool) {
	plain := m.transcriptPlainLines()
	a, b, ok := parseRange(m.copyInput, len(plain))
	if !ok {
		return "", 0, 0, false
	}
	return strings.Join(plain[a-1:b], "\n"), a, b, true
}

// parseRange parses "A-B", "A", "A-" (to end), or "-B" (from start) against a 1..max line set.
func parseRange(s string, max int) (a, b int, ok bool) {
	s = strings.TrimSpace(s)
	if s == "" || max <= 0 {
		return 0, 0, false
	}
	lo, hi, isRange := strings.Cut(s, "-")
	a, aok := atoiTrim(lo)
	if !isRange {
		if !aok || a < 1 || a > max {
			return 0, 0, false
		}
		return a, a, true
	}
	b, bok := atoiTrim(hi)
	if strings.TrimSpace(lo) == "" { // "-B" → from line 1
		a, aok = 1, true
	}
	if strings.TrimSpace(hi) == "" { // "A-" → to the last line
		b, bok = max, true
	}
	if !aok || !bok {
		return 0, 0, false
	}
	if a < 1 {
		a = 1
	}
	if b > max {
		b = max
	}
	if a > b || a > max {
		return 0, 0, false
	}
	return a, b, true
}

func atoiTrim(s string) (int, bool) {
	n, err := strconv.Atoi(strings.TrimSpace(s))
	return n, err == nil
}

// transcriptActive reports whether the transcript (right pane) has the arrow keys — explicitly
// focused, or implicitly in fullscreen where the seat list isn't shown.
func (m *model) transcriptActive() bool {
	return m.view == viewSeats && (m.fullscreen || m.focus == focusTranscript)
}

func onOff(b bool) string {
	if b {
		return "on"
	}
	return "off"
}

// statusOf reads the HTTP status carried by a streamFatalEvent sentinel frame.
func statusOf(f Frame) string {
	if code, ok := f.Data["status"].(float64); ok {
		return fmt.Sprintf("%d", int(code))
	}
	return "4xx"
}

// copyToClipboard puts text on the user's clipboard. It does BOTH a local OS-clipboard write AND an
// OSC-52 emit, because neither alone is sufficient: pbcopy/xclip/xsel/clip only reach the machine
// arb-watch runs ON — useless when the TUI is viewed over ssh/mosh, where the user's real clipboard
// lives on the *client*. "pbcopy succeeded" does NOT mean "I'm local" (the mac-mini always has pbcopy
// even when reached remotely), so OSC-52 — which travels up the terminal to the client's clipboard,
// local iTerm2 or remote alike — must ALWAYS fire. OSC-52 is written in one shot to /dev/tty (NOT
// os.Stderr, which interleaved with bubbletea's renderer and leaked the base64 onto the screen).
// Doing both is harmless: locally both target the same clipboard; remotely the local-tool write hits
// the (unused) server clipboard while OSC-52 reaches the client.
func copyToClipboard(text string) tea.Cmd {
	return func() tea.Msg {
		copyViaOSClipboard(text) // best-effort: helps a LOCAL terminal with OSC-52 disabled
		emitOSC52(text)          // always: the only path that reaches the client over ssh/mosh
		return nil
	}
}

// copyViaOSClipboard pipes text into the platform clipboard utility. Returns false if no utility
// is available (so the caller can fall back to OSC-52).
func copyViaOSClipboard(text string) bool {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("pbcopy")
	case "windows":
		cmd = exec.Command("clip")
	default: // linux/bsd
		if _, err := exec.LookPath("xclip"); err == nil {
			cmd = exec.Command("xclip", "-selection", "clipboard")
		} else if _, err := exec.LookPath("xsel"); err == nil {
			cmd = exec.Command("xsel", "--clipboard", "--input")
		} else if _, err := exec.LookPath("wl-copy"); err == nil {
			cmd = exec.Command("wl-copy")
		}
	}
	if cmd == nil {
		return false
	}
	cmd.Stdin = strings.NewReader(text)
	return cmd.Run() == nil
}

// emitOSC52 is the remote/SSH fallback: write the clipboard escape in a single tty write so it
// can't interleave with the bubbletea renderer. Best-effort — terminals without OSC-52 ignore it.
func emitOSC52(text string) {
	b64 := base64.StdEncoding.EncodeToString([]byte(text))
	tty, err := os.OpenFile("/dev/tty", os.O_WRONLY, 0)
	if err != nil {
		return
	}
	defer tty.Close()
	fmt.Fprintf(tty, "\x1b]52;c;%s\x07", b64)
}

// syncCursorToSelection keeps the cursor on the SELECTED seat across activity reorders:
// when a frame re-sorts the list, the index that matched selectedTask may have moved, so
// re-find it. No-op when nothing is selected (the cursor stays put — e.g. at the top, the
// most-recent seat). This makes the highlight follow the seat, not a fixed row.
func (m *model) syncCursorToSelection() {
	if m.selectedTask == "" {
		return
	}
	for i, id := range m.visibleSeats() {
		if id == m.selectedTask {
			m.seatCursor = i
			m.clampSeatScroll()
			return
		}
	}
}

// autoSelectSeat loads the cursor seat's transcript if it isn't already open — so arrow
// navigation in the seat list live-previews each seat without pressing enter.
func (m *model) autoSelectSeat() tea.Cmd {
	vis := m.visibleSeats()
	if m.seatCursor < 0 || m.seatCursor >= len(vis) {
		return nil
	}
	task := vis[m.seatCursor]
	if task == m.selectedTask {
		return nil
	}
	return m.enterSeat(task)
}

func (m *model) maybeFetchNextHistoryPage() tea.Cmd {
	if m.seatSource != "history" || !m.historyHasMore || m.historyLoading {
		return nil
	}
	vis := m.visibleSeats()
	if len(vis) == 0 || m.seatCursor < len(vis)-1 {
		return nil
	}
	m.historyLoading = true
	return fetchHistoryCmd(m.baseURL, m.token, m.orchestrator, m.historyCursor, true, m.historyGen)
}

func (m *model) toggleHistoryMode() tea.Cmd {
	m.seatCursor = 0
	m.seatScroll = 0
	m.historyGen++
	if m.seatSource == "history" {
		m.seatSource = "live"
		m.historyCursor = ""
		m.historyHasMore = false
		m.historyLoading = false
		m.seats = map[string]map[string]any{}
		m.seatOrder = nil
		m.status = ""
		streamURL := m.baseURL + "/sse/orchestrator/" + url.PathEscape(m.orchestrator)
		return m.startStream(m, streamOrch, streamURL, "")
	}
	m.seatSource = "history"
	m.historyCursor = ""
	m.historyHasMore = false
	m.historyLoading = true
	m.status = "loading history…"
	return fetchHistoryCmd(m.baseURL, m.token, m.orchestrator, "", false, m.historyGen)
}

func (m *model) enterMenu() tea.Cmd {
	m.orch.stop()
	m.seat.stop()
	m.view = viewOrchestrators
	m.orchestrator = ""
	m.selectedTask = ""
	m.subTitle = "fleet · select an orchestrator session"
	m.seats = map[string]map[string]any{}
	m.seatOrder = nil
	m.seatCursor = 0
	m.seatSource = "live"
	m.historyCursor = ""
	m.historyHasMore = false
	m.historyLoading = false
	m.historyGen++
	m.statusFilter = filterAll
	m.agentFilter = filterAll
	m.focus = focusSeats
	m.transcript = nil
	m.vp.SetContent("")
	return fetchOrchestratorsCmd(m.baseURL, m.token)
}

func (m *model) enterSeats(orchestrator string) tea.Cmd {
	m.seat.stop() // no seat selected in the newly-entered orchestrator
	m.view = viewSeats
	m.orchestrator = orchestrator
	m.selectedTask = ""
	m.subTitle = orchestrator + " · select a seat  (m: fleet menu)"
	m.seats = map[string]map[string]any{}
	m.seatOrder = nil
	m.seatCursor = 0
	m.seatSource = "live"
	m.historyCursor = ""
	m.historyHasMore = false
	m.historyLoading = false
	m.historyGen++
	m.statusFilter = filterAll
	m.agentFilter = filterAll
	m.focus = focusSeats
	m.transcript = nil
	m.vp.SetContent("")
	streamURL := m.baseURL + "/sse/orchestrator/" + url.PathEscape(orchestrator)
	return m.startStream(m, streamOrch, streamURL, "") // (re)start the seat-list stream
}

// enterSeat opens a seat's transcript WITHOUT touching the orchestrator stream, so the
// seat list stays live (matches Python test_selecting_seat_does_not_cancel_orchestrator_stream).
func (m *model) enterSeat(taskID string) tea.Cmd {
	m.selectedTask = taskID
	m.transcript = nil
	m.vp.SetContent("")
	streamURL := m.baseURL + "/sse/seat/" + url.PathEscape(taskID)
	return m.startStream(m, streamSeat, streamURL, "")
}

func (m *model) replaceSeatsWithHistory(seats []map[string]any) {
	m.seats = map[string]map[string]any{}
	m.seatOrder = make([]string, 0, len(seats))
	m.appendHistoryPage(seats)
}

func (m *model) appendHistoryPage(seats []map[string]any) {
	for _, seat := range seats {
		taskID := getString(seat, "task_id")
		if taskID == "" {
			continue
		}
		if _, exists := m.seats[taskID]; exists {
			continue
		}
		m.seatOrder = append(m.seatOrder, taskID)
		m.seats[taskID] = seat
	}
}

func (m *model) upsertSeat(frame Frame) {
	taskID := getString(frame.Data, "task_id")
	if taskID == "" {
		return
	}
	previous := m.seats[taskID]
	var seat map[string]any
	if getString(frame.Data, "state") != "" {
		seat = copyMap(frame.Data)
	} else {
		seat = reduceSeat(previous, frame.Data)
	}
	if m.seats == nil {
		m.seats = map[string]map[string]any{}
	}
	if _, exists := m.seats[taskID]; !exists {
		m.seatOrder = append(m.seatOrder, taskID)
		sort.Strings(m.seatOrder)
	}
	m.seats[taskID] = seat
}

func (m *model) appendTranscript(frame Frame) {
	if isLifecycleNoise(frame.Data) {
		return
	}
	m.transcript = append(m.transcript, frame.Data)
	if m.copyMode {
		m.renderTranscriptNumbered() // keep the gutter; don't yank the scroll position mid-select
		return
	}
	m.renderTranscript()
	m.vp.GotoBottom()
}

// rerenderTranscript re-wraps the transcript to the current viewport width after a resize or
// fullscreen toggle, honouring copy mode. No-op when no seat/transcript is open.
func (m *model) rerenderTranscript() {
	if m.selectedTask == "" || len(m.transcript) == 0 {
		return
	}
	if m.copyMode {
		m.renderTranscriptNumbered()
	} else {
		m.renderTranscript()
	}
}

// renderTranscript rebuilds the viewport content from the stored frame data, honouring
// the timestamp/label toggles (matches Python _rerender_timeline).
func (m *model) renderTranscript() {
	lines := make([]string, 0, len(m.transcript))
	for _, data := range m.transcript {
		lines = append(lines, m.wrapBlock(m.renderTranscriptLineFull(data)))
	}
	m.vp.SetContent(strings.Join(lines, "\n\n"))
}

// renderTranscriptLineFull = optional dim timestamp + optional dim-cyan source/kind label
// + the per-kind styled body (matches Python _render_event).
func (m *model) renderTranscriptLineFull(data map[string]any) string {
	return m.styledBlock(data, m.expanded)
}

// wrapTo word-wraps text to w columns (at word boundaries, ANSI-safe via lipgloss),
// stripping the trailing padding lipgloss adds so copied text stays clean. Used for the
// DISPLAY only — the copy overlay numbers/extracts LOGICAL lines (a wrapped line counts as 1).
func wrapTo(s string, w int) string {
	if w < 2 {
		return s
	}
	lines := strings.Split(lipgloss.NewStyle().Width(w).Render(s), "\n")
	for i := range lines {
		lines[i] = strings.TrimRight(lines[i], " ")
	}
	return strings.Join(lines, "\n")
}

func (m *model) wrapBlock(s string) string { return wrapTo(s, m.vp.Width) }

// styledBlock is one transcript entry rendered styled, honouring the timestamp/label toggles
// and (unless expanded) output collapse.
func (m *model) styledBlock(data map[string]any, expanded bool) string {
	var prefix string
	if m.showTimestamps {
		if ts := fallback(getString(data, "ts"), getString(data, "sent_at")); ts != "" {
			prefix += styleDim.Render(ts) + " "
		}
	}
	if m.showLabels {
		if label := transcriptLabel(data); label != "" {
			prefix += styleLabel.Render(label) + " "
		}
	}
	return collapseOutput(data, prefix+styledTranscriptLine(data), expanded)
}

// plainBlock is the un-styled twin of styledBlock — same toggles and collapse so the plain and
// styled visual-line lists stay 1:1 (the copy-range overlay relies on that alignment).
func (m *model) plainBlock(data map[string]any, expanded bool) string {
	var prefix string
	if m.showTimestamps {
		if ts := fallback(getString(data, "ts"), getString(data, "sent_at")); ts != "" {
			prefix += ts + " "
		}
	}
	if m.showLabels {
		if label := transcriptLabel(data); label != "" {
			prefix += label + " "
		}
	}
	return collapseOutput(data, prefix+renderTranscriptLine(data), expanded)
}

func transcriptLabel(data map[string]any) string {
	return strings.TrimSpace(fallback(getString(data, "source"), "event") + " " +
		fallback(getString(data, "kind"), getString(data, "event_type")))
}

// transcriptStyledLines / transcriptPlainLines are the fully-expanded visual lines (split on
// newlines, blank separators included) used by the copy-range overlay. Both index identically.
func (m *model) transcriptStyledLines() []string {
	return m.visualLines(true)
}

func (m *model) transcriptPlainLines() []string {
	return m.visualLines(false)
}

func (m *model) visualLines(styled bool) []string {
	blocks := make([]string, 0, len(m.transcript))
	for _, data := range m.transcript {
		if styled {
			blocks = append(blocks, m.styledBlock(data, true))
		} else {
			blocks = append(blocks, m.plainBlock(data, true))
		}
	}
	return strings.Split(strings.Join(blocks, "\n\n"), "\n")
}

// renderTranscriptNumbered sets the viewport to the fully-expanded transcript with a 1-indexed
// line-number gutter (copy mode).
func (m *model) renderTranscriptNumbered() {
	styled := m.transcriptStyledLines() // LOGICAL lines — one number each (a wrapped line counts as 1)
	gutter := len(strconv.Itoa(len(styled)))
	indent := gutter + 2 // width of the "<num>│ " prefix
	var b strings.Builder
	for i, line := range styled {
		num := styleLineNo.Render(fmt.Sprintf("%*d│", gutter, i+1)) + " "
		// wrap the logical line for display; continuation rows align under the text, but the
		// line is still ONE numbered/copyable unit (copyRange extracts the logical line).
		segs := strings.Split(wrapTo(line, m.vp.Width-indent), "\n")
		for j, seg := range segs {
			if j == 0 {
				b.WriteString(num + seg + "\n")
			} else {
				b.WriteString(strings.Repeat(" ", indent) + seg + "\n")
			}
		}
	}
	m.vp.SetContent(strings.TrimRight(b.String(), "\n"))
}

// maxCollapsedOutputLines is how many lines of a tool-output block survive when collapsed.
const maxCollapsedOutputLines = 6

// collapseOutput truncates a long tool-output block to maxCollapsedOutputLines (with a hint)
// unless expanded. Only tool/command output and apply_patch diffs collapse — model prose and
// command invocations are always shown in full. Splitting the styled block is safe because
// Lip Gloss styles each line independently.
func collapseOutput(data map[string]any, rendered string, expanded bool) string {
	if expanded || !isTruncatableOutput(data) {
		return rendered
	}
	lines := strings.Split(rendered, "\n")
	if len(lines) <= maxCollapsedOutputLines {
		return rendered
	}
	hidden := len(lines) - maxCollapsedOutputLines
	kept := strings.Join(lines[:maxCollapsedOutputLines], "\n")
	return kept + "\n" + styleDim.Render(fmt.Sprintf("    … +%d line(s) — e to expand", hidden))
}

// isTruncatableOutput reports whether a transcript entry is tool output (collapsible) rather
// than model prose or a command invocation.
func isTruncatableOutput(data map[string]any) bool {
	switch getString(data, "kind") {
	case "command_output", "command_finished", "tool_output":
		return true
	}
	if getString(data, "tool_name") == "apply_patch" && getString(getMap(data, "meta"), "file") != "" {
		return true
	}
	return false
}

// transcriptPlain returns the full un-styled transcript (for the 'c' whole-copy), honouring the
// timestamp/label toggles. Always fully expanded — a copy shouldn't drop truncated output.
func (m *model) transcriptPlain() string {
	blocks := make([]string, 0, len(m.transcript))
	for _, data := range m.transcript {
		blocks = append(blocks, m.plainBlock(data, true))
	}
	return strings.Join(blocks, "\n\n")
}

func listen(ch chan Frame, which string, gen int) tea.Cmd {
	return func() tea.Msg {
		frame, ok := <-ch
		if !ok {
			return streamClosedMsg{which: which, gen: gen}
		}
		return frameMsg{which: which, gen: gen, f: frame}
	}
}

func realStartStream(m *model, which, streamURL, lastID string) tea.Cmd {
	s := m.stream(which)
	if s.cancel != nil {
		s.cancel()
	}
	ctx, cancel := context.WithCancel(context.Background())
	s.cancel = cancel
	s.ch = make(chan Frame)
	s.gen++
	s.url = streamURL
	// A fresh stream to a (possibly new) target starts with fresh backoff;
	// enterSeat/enterSeats reach here without stop(), so without this a stale
	// backoff from the previous target would delay the new one's reconnect (GO-3).
	s.backoff = baseBackoff
	go streamSSE(ctx, streamURL, m.token, lastID, s.ch)
	return listen(s.ch, which, s.gen)
}

func fetchOrchestratorsCmd(baseURL, token string) tea.Cmd {
	return func() tea.Msg {
		orchestrators, err := fetchOrchestrators(baseURL, token)
		if err != nil {
			return errMsg(err)
		}
		return orchestratorsMsg(orchestrators)
	}
}

func tickCmd() tea.Cmd {
	return tea.Tick(2*time.Second, func(t time.Time) tea.Msg {
		return tickMsg(t)
	})
}

func (m *model) renderOrchestrators() string {
	var b strings.Builder
	b.WriteString(styleTitle.Render("arb-watch · fleet") + "\n\n")
	if len(m.orchestrators) == 0 {
		b.WriteString(styleHint.Render("No orchestrator sessions found in the fleet."))
		return stylePane.Render(b.String())
	}
	for i, orchestrator := range m.orchestrators {
		row := "  " + orchestrator
		if i == m.orchCursor {
			row = styleCursor.Render("▸ " + orchestrator)
		}
		b.WriteString(row + "\n")
	}
	b.WriteString("\n" + styleHint.Render("↑/↓ select · enter drill · q quit"))
	return stylePane.Render(b.String())
}

// parseSeatTime parses a seat's last_event_ts (RFC3339, tolerating a trailing Z).
func parseSeatTime(seat map[string]any) (time.Time, bool) {
	ts := getString(seat, "last_event_ts")
	if ts == "" {
		return time.Time{}, false
	}
	parsed, err := time.Parse(time.RFC3339Nano, strings.Replace(ts, "Z", "+00:00", 1))
	if err != nil {
		return time.Time{}, false
	}
	return parsed, true
}

func seatAge(seat map[string]any) (string, float64, bool) {
	parsed, ok := parseSeatTime(seat)
	if !ok {
		return "—", 0, false
	}
	secs := time.Since(parsed).Seconds()
	return ageLabel(secs), secs, true
}

// effectiveState is the seat's state for filtering/display intent: a "running" seat whose
// last event is older than staleGrace reads as "stale" even before a tick re-reduces it, so
// the status filter agrees with the live age column.
func effectiveState(seat map[string]any) string {
	state := getString(seat, "state")
	if state == "running" {
		if _, secs, ok := seatAge(seat); ok && secs > staleGrace.Seconds() {
			return "stale"
		}
	}
	return state
}

// visibleSeats is seatOrder narrowed by the active status/agent filters. The seat cursor,
// auto-select, Enter, and the rendered table all index THIS list so navigation matches what's shown.
func (m *model) visibleSeats() []string {
	runSeats := make([]string, 0, len(m.seatOrder))
	orchestrators := make([]string, 0)
	for _, id := range m.seatOrder {
		seat := m.seats[id]
		if m.statusFilter != filterAll && effectiveState(seat) != m.statusFilter {
			continue
		}
		agent := agentOf(getString(seat, "seat_id"))
		if m.agentFilter != filterAll && agent != m.agentFilter {
			continue
		}
		if agent == "claude" {
			orchestrators = append(orchestrators, id)
			continue
		}
		runSeats = append(runSeats, id)
	}
	// Order by last activity, most recent first. Stable so seats without a parseable
	// last_event_ts keep their insertion order at the bottom (and tests with no
	// timestamps see the original order preserved).
	sort.SliceStable(runSeats, func(i, j int) bool {
		_, ai, oki := seatAge(m.seats[runSeats[i]])
		_, aj, okj := seatAge(m.seats[runSeats[j]])
		if oki != okj {
			return oki // a seat with a timestamp sorts above one without
		}
		if !oki {
			return false // neither has one → keep stable order
		}
		return ai < aj // smaller age = more recent = higher
	})
	return m.dedupSeatRuns(append(runSeats, orchestrators...))
}

// dedupSeatRuns collapses phantom duplicate rows: the run_id<->task_id dual-tee
// (bridge roster + turn-heartbeat falling back run_id->task_id) can register one seat under two
// task_ids that share the same (seat_id, run_id). Keep the first occurrence — for runSeats that's
// the most-recent after the recency sort. Distinct run_ids on one seat (real parallel/sequential
// work) and entries missing seat_id/run_id are never collapsed.
//
// GO-4 (panel 2026-07-08): a phantom duplicate and a genuine same-label sequential retry are
// INDISTINGUISHABLE by (seat_id, run_id), so this necessarily collapses a retry too, showing the
// latest attempt (recency sort). Intentional — a task-aware key would defeat the phantom collapse;
// the older attempt's transcript still lives in the store. Pinned by
// TestVisibleSeatsSameRunLabelRetryShowsLatestAttemptOnly.
func (m *model) dedupSeatRuns(ids []string) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(ids))
	for _, id := range ids {
		seat := getString(m.seats[id], "seat_id")
		run := getString(m.seats[id], "run_id")
		if seat != "" && run != "" {
			key := seat + "\x00" + run
			if seen[key] {
				continue
			}
			seen[key] = true
		}
		out = append(out, id)
	}
	return out
}

// distinctSeatTotal counts seats after collapsing phantom (seat_id, run_id) duplicates, ignoring
// status/agent filters — the denominator for the "shown/total" header so a phantom doesn't read as
// a hidden seat.
func (m *model) distinctSeatTotal() int {
	return len(m.dedupSeatRuns(m.seatOrder))
}

// runDisplay renders the roster "Run" column: the meaningful run label plus a short task-id handle.
// Un-tagged runs (run_id fell back to the task_id GUID) show just the short id once; tagged runs
// (run_id is a label != task_id) show "<label> · <short-task>" so you can see what the seat was
// doing AND tell apart seats sharing a run label.
func runDisplay(seat map[string]any) string {
	run := getString(seat, "run_id")
	task := getString(seat, "task_id")
	short := task
	if len(short) > 8 {
		short = short[:8]
	}
	if run == "" || run == task {
		return short
	}
	if short == "" {
		return run
	}
	return run + " · " + short
}

// agentCycle is "all" plus the distinct agents currently present (sorted), so 'a' only offers
// filters that match real seats.
func (m *model) agentCycle() []string {
	set := map[string]bool{}
	for _, id := range m.seatOrder {
		set[agentOf(getString(m.seats[id], "seat_id"))] = true
	}
	agents := make([]string, 0, len(set))
	for a := range set {
		agents = append(agents, a)
	}
	sort.Strings(agents)
	return append([]string{filterAll}, agents...)
}

// cycleNext returns the value after current in values (wrapping); current absent → first value.
func cycleNext(values []string, current string) string {
	for i, v := range values {
		if v == current {
			return values[(i+1)%len(values)]
		}
	}
	return values[0]
}

// clampSeatCursor keeps the cursor within the currently-visible seat list after a filter change.
func (m *model) clampSeatCursor() {
	n := len(m.visibleSeats())
	if m.seatCursor >= n {
		m.seatCursor = n - 1
	}
	if m.seatCursor < 0 {
		m.seatCursor = 0
	}
	m.clampSeatScroll()
}

// leftPaneWidth is the seat pane's outer width (40% of the terminal, floored). Shared by
// View, the row-column sizing, and seatViewportRows so they all agree on the geometry.
func (m *model) leftPaneWidth() int {
	left := m.width * 40 / 100
	if left < 28 {
		left = 28
	}
	return left
}

// seatViewportRows is how many seat rows fit in the left pane BELOW the filter bar: the pane
// content height (paneH minus its bottom padding) minus the filter-bar box, the column header,
// the two ALWAYS-reserved scroll-hint lines, and the status line. The previous version returned
// vp.Height-2 and forgot to subtract the filter bar, so a full list overflowed the pane and the
// hint lines appearing/disappearing on scroll made the layout jump. Reserving those lines (see
// renderSeatTable) keeps the table a fixed height as you scroll.
func (m *model) seatViewportRows() int {
	paneH := m.vp.Height + 2
	fbH := lipgloss.Height(m.renderFilterBar(m.leftPaneWidth() - 4))
	rows := paneH - 1 - fbH - 1 - 2 - 1 // -paddingBottom -header -2 hint lines -status
	if rows < 3 {
		rows = 3
	}
	return rows
}

// clampSeatScroll keeps seatScroll in range and the cursor inside the visible window, so a
// seat list longer than the pane scrolls to follow the selection instead of overflowing.
func (m *model) clampSeatScroll() {
	rows := m.seatViewportRows()
	n := len(m.visibleSeats())
	maxScroll := n - rows
	if maxScroll < 0 {
		maxScroll = 0
	}
	if m.seatScroll > maxScroll {
		m.seatScroll = maxScroll
	}
	if m.seatCursor < m.seatScroll {
		m.seatScroll = m.seatCursor
	}
	if m.seatCursor >= m.seatScroll+rows {
		m.seatScroll = m.seatCursor - rows + 1
	}
	if m.seatScroll < 0 {
		m.seatScroll = 0
	}
}

// renderFilterBar is the separate Filters area above the seat list (status + agent + counts).
func (m *model) renderFilterBar(width int) string {
	shown, total := len(m.visibleSeats()), m.distinctSeatTotal()
	body := "Filters" +
		"\nstatus: " + styleFilterVal.Render(m.statusFilter) + styleDim.Render(" (s)") +
		"\nagent:  " + styleFilterVal.Render(m.agentFilter) + styleDim.Render(" (a)") +
		styleDim.Render(fmt.Sprintf("   %d/%d", shown, total))
	if m.seatSource == "history" {
		body += "\n" + styleFilterVal.Render("· history") + styleDim.Render(" (h)")
	}
	return styleFilterBox.Width(width).Render(body)
}

// seatColWidths sizes the Seat and Run columns to the current seat-pane width so a row
// always fits on ONE line (no mid-table wrap at the pane edge), wider on wide terminals
// and graceful on narrow ones. State (10) and Age (5) are fixed; Seat and Run split the rest.
func (m *model) seatColWidths() (seatW, runW int) {
	left := m.leftPaneWidth()
	// pane inner = left-2 (border); a row is cursor(1) + seat + sp + state(10) + sp + age(5) + sp + run.
	avail := (left - 2) - 1 - 10 - 5 - 3
	if avail < 16 {
		avail = 16
	}
	seatW = avail * 55 / 100
	if seatW < 10 {
		seatW = 10
	}
	if seatW > 28 {
		seatW = 28
	}
	runW = avail - seatW
	if runW < 6 {
		runW = 6
	}
	return seatW, runW
}

func (m *model) renderSeatTable() string {
	seatW, runW := m.seatColWidths()
	header := styleColHdr.Render(fmt.Sprintf("%-*s %-10s %-5s %s", seatW, "Seat", "State", "Age", "Run"))
	vis := m.visibleSeats()
	m.clampSeatScroll() // keep the window valid even right after a resize shrank the pane
	rows := m.seatViewportRows()
	start := m.seatScroll
	end := start + rows
	if end > len(vis) {
		end = len(vis)
	}

	// The table renders at a FIXED line count regardless of scroll position: a reserved
	// top-hint line, exactly `rows` body lines (blank-padded), a reserved bottom-hint line, and
	// a reserved status line. That invariance is what stops the pane — and the filter bar above
	// it — from jumping as the ↑/↓ affordances come and go while you scroll a full list.
	lines := make([]string, 0, rows+4)
	lines = append(lines, header)

	topHint := ""
	if start > 0 {
		topHint = styleDim.Render(fmt.Sprintf("  ↑ %d more", start))
	}
	lines = append(lines, topHint)

	body := make([]string, 0, rows)
	orchestratorDivider := false
	for i := start; i < end && len(body) < rows; i++ {
		seat := m.seats[vis[i]]
		if agentOf(getString(seat, "seat_id")) == "claude" && !orchestratorDivider {
			orchestratorDivider = true
			body = append(body, styleDim.Render("── orchestrator ──"))
			if len(body) >= rows {
				break
			}
		}
		state := effectiveState(seat) // a quiet running seat reads "stale" once past staleGrace
		ageLbl, ageSecs, ok := seatAge(seat)
		// In history a day-old seat would read a useless "72h"/"240h"; show its dd/mm date
		// instead once past 24h. Live seats keep the relative age.
		if ok && m.seatSource == "history" && ageSecs >= 24*3600 {
			if ts, ok2 := parseSeatTime(seat); ok2 {
				ageLbl = ts.Format("02/01")
			}
		}
		age := ageLbl
		if ok {
			age = ageStyle(ageSecs, state).Render(ageLbl)
		}
		styledState := state
		if c, found := stateColors[state]; found {
			styledState = lipgloss.NewStyle().Foreground(c).Render(state)
		}
		// Columns are width-fit so each row is a single clean line; Age is padded to its
		// visible width (padState, ANSI-aware) so styling can't skew the Run column.
		runText := runDisplay(seat)
		if stalled := stallLabel(seat); stalled != "" {
			runText = stalled + " " + runText
		}
		row := fmt.Sprintf("%-*s %-10s %s %s",
			seatW, truncate(getString(seat, "seat_id"), seatW), padState(styledState, state, 10), padState(age, ageLbl, 5), truncate(runText, runW))
		if i == m.seatCursor {
			row = styleCursor.Render("▸" + row)
		} else {
			row = " " + row
		}
		body = append(body, row)
	}
	for len(body) < rows {
		body = append(body, "")
	}
	lines = append(lines, body...)

	bottomHint := ""
	if end < len(vis) {
		bottomHint = styleDim.Render(fmt.Sprintf("  ↓ %d more", len(vis)-end))
	}
	lines = append(lines, bottomHint)

	status := ""
	if m.status != "" {
		status = styleHint.Render(m.status)
	}
	lines = append(lines, status)

	return strings.Join(lines, "\n")
}

// renderHeader paints the seat-header bar. width is the detail-pane content width so the
// accent background spans the full pane (Python's #seat-header padding 0 2), and MarginBottom
// adds the row of space under it (Python's #timeline padding-top 1).
func (m *model) renderHeader(width int) string {
	bar := styleHeaderBar.Width(width).MarginBottom(1)
	if m.selectedTask == "" {
		return bar.Render("select a seat")
	}
	seat := m.seats[m.selectedTask]
	parts := []string{getString(seat, "seat_id"), agentOf(getString(seat, "seat_id"))}
	if modelName := fallback(getString(seat, "model"), getString(seat, "engine_model")); modelName != "" {
		parts = append(parts, modelName)
	}
	parts = append(parts, "run "+fallback(runDisplay(seat), "—"), getString(seat, "state"))
	if ageLbl, _, ok := seatAge(seat); ok {
		parts = append(parts, ageLbl)
	}
	return bar.Render(strings.Join(parts, " · "))
}

// normalizeHeight forces s to exactly n lines: truncating extra lines, or padding with blanks.
// Used to pin a pane's content to a fixed height so it can't overflow its bordered box.
func normalizeHeight(s string, n int) string {
	if n < 0 {
		n = 0
	}
	lines := strings.Split(s, "\n")
	if len(lines) > n {
		lines = lines[:n]
	}
	for len(lines) < n {
		lines = append(lines, "")
	}
	return strings.Join(lines, "\n")
}

// padState pads a colour-styled state string to width n using its UNSTYLED length
// (so ANSI escape codes don't break column alignment).
func padState(styled, plain string, n int) string {
	if pad := n - len([]rune(plain)); pad > 0 {
		return styled + strings.Repeat(" ", pad)
	}
	return styled
}

func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n-1]) + "…"
}

func copyMap(input map[string]any) map[string]any {
	out := map[string]any{}
	for k, v := range input {
		out[k] = v
	}
	return out
}

func fallback(value, otherwise string) string {
	if value == "" {
		return otherwise
	}
	return value
}
