package main

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
)

const staleGrace = 120 * time.Second

var shellWrapper = regexp.MustCompile(`(?s)^\S*sh\s+-l?c\s+(.*)$`)

func reduceSeat(prev, entry map[string]any) map[string]any {
	out := map[string]any{}
	for k, v := range prev {
		out[k] = v
	}
	if len(entry) == 0 {
		if isStale(out) {
			out["state"] = "stale"
		}
		return out
	}

	eventType := getString(entry, "event_type")
	data := entryData(entry)
	copyField(out, entry, "run_id")
	copyField(out, entry, "task_id")
	copyField(out, entry, "seat_id")
	copyField(out, entry, "orchestrator")
	if model := getString(entry, "model"); model != "" {
		out["model"] = model
	}
	if engineModel := getString(entry, "engine_model"); engineModel != "" {
		out["engine_model"] = engineModel
	}
	if sentAt := getString(entry, "sent_at"); sentAt != "" {
		out["last_event_ts"] = sentAt
	}
	if eventType != "" {
		out["last_event"] = eventType
	}
	if stalledAt := getString(entry, "stalled_at"); stalledAt != "" {
		out["stalled_at"] = stalledAt
	} else {
		delete(out, "stalled_at")
	}

	switch eventType {
	case "task_started", "task_continuing":
		out["state"] = "running"
	case "task_finished":
		if ok, exists := data["ok"].(bool); exists && !ok {
			out["state"] = "failed"
		} else {
			out["state"] = "done"
		}
	case "vote":
		out["voted"] = true
		if stance, ok := data["stance"]; ok {
			out["stance"] = stance
		}
		state := getString(out, "state")
		if state != "done" && state != "failed" {
			out["state"] = "voted"
		}
	}
	if isStale(out) {
		out["state"] = "stale"
	}
	return out
}

func copyField(out, entry map[string]any, key string) {
	if value := getString(entry, key); value != "" {
		out[key] = value
	}
}

func entryData(entry map[string]any) map[string]any {
	raw, ok := entry["data"]
	if !ok {
		return map[string]any{}
	}
	if data, ok := raw.(map[string]any); ok {
		return data
	}
	if text, ok := raw.(string); ok {
		var data map[string]any
		if err := json.Unmarshal([]byte(text), &data); err == nil {
			return data
		}
	}
	return map[string]any{}
}

func isStale(state map[string]any) bool {
	if getString(state, "state") != "running" {
		return false
	}
	raw := getString(state, "last_event_ts")
	if raw == "" {
		return false
	}
	ts, err := time.Parse(time.RFC3339Nano, strings.Replace(raw, "Z", "+00:00", 1))
	if err != nil {
		return false
	}
	return time.Since(ts) > staleGrace
}

func agentOf(seatID string) string {
	lower := strings.ToLower(seatID)
	if strings.HasPrefix(lower, "cold-opus-") {
		return "opus"
	}
	head := strings.ToLower(strings.SplitN(seatID, "-", 2)[0])
	if head == "" {
		return "?"
	}
	labels := map[string]string{
		"codex":  "codex",
		"agy":    "agy",
		"pi":     "pi",
		"gemini": "gemini",
		"cursor": "cursor",
		"grok":   "grok",
		"kimi":   "kimi",
		"claude": "claude",
	}
	if label, ok := labels[head]; ok {
		return label
	}
	return head
}

func isLifecycleNoise(ev map[string]any) bool {
	return getString(ev, "source") != "transcript"
}

func formatCommand(raw string) string {
	raw = strings.TrimSpace(raw)
	match := shellWrapper.FindStringSubmatch(raw)
	if match == nil {
		return raw
	}
	inner := strings.TrimSpace(match[1])
	if len(inner) >= 2 && (inner[0] == '\'' || inner[0] == '"') && inner[len(inner)-1] == inner[0] {
		inner = inner[1 : len(inner)-1]
	}
	return "Bash(" + inner + ")"
}

func renderTranscriptLine(ev map[string]any) string {
	kind := getString(ev, "kind")
	content := strings.TrimSpace(getString(ev, "content"))
	toolName := strings.TrimSpace(getString(ev, "tool_name"))
	meta := getMap(ev, "meta")
	if kind == "model_text" {
		return "⏺ " + content
	}
	if kind == "model_thinking" {
		if content == "" {
			content = "thinking"
		}
		return "⏺ " + content
	}
	if toolName == "apply_patch" && getString(meta, "file") != "" {
		line := fmt.Sprintf("⏺ Update(%s) +%d/-%d", getString(meta, "file"), toInt(meta["added"]), toInt(meta["removed"]))
		if content != "" {
			line += "\n  ⎿ " + content
		}
		return line
	}
	if kind == "command_started" || kind == "tool_call" {
		target := toolName
		if target == "" {
			target = content
		}
		return "⏺ " + formatCommand(target)
	}
	if kind == "command_output" || kind == "command_finished" || kind == "tool_output" {
		body := content
		if body == "" {
			body = toolName
		}
		body = strings.TrimRight(body, "\n")
		return "  ⎿ " + strings.ReplaceAll(body, "\n", "\n    ")
	}
	return "⏺ " + content
}

func ageLabel(seconds float64) string {
	s := int(seconds)
	if s < 60 {
		return fmt.Sprintf("%ds", s)
	}
	if s < 3600 {
		return fmt.Sprintf("%dm", s/60)
	}
	return fmt.Sprintf("%dh", s/3600)
}

func stallLabel(seat map[string]any) string {
	raw := getString(seat, "stalled_at")
	if raw == "" {
		return ""
	}
	ts, err := time.Parse(time.RFC3339Nano, strings.Replace(raw, "Z", "+00:00", 1))
	if err != nil {
		return "STALL"
	}
	return "STALL " + ageLabel(time.Since(ts).Seconds())
}

func ageStyle(seconds float64, state string) lipgloss.Style {
	style := lipgloss.NewStyle()
	if state != "running" {
		return style.Faint(true)
	}
	if seconds >= 90 {
		return style.Bold(true).Foreground(lipgloss.Color("9"))
	}
	if seconds >= 30 {
		return style.Foreground(lipgloss.Color("11"))
	}
	return style.Faint(true)
}

func getString(values map[string]any, key string) string {
	if values == nil {
		return ""
	}
	switch value := values[key].(type) {
	case string:
		return value
	case fmt.Stringer:
		return value.String()
	default:
		return ""
	}
}

func getMap(values map[string]any, key string) map[string]any {
	if values == nil {
		return map[string]any{}
	}
	if got, ok := values[key].(map[string]any); ok {
		return got
	}
	return map[string]any{}
}

func toInt(value any) int {
	switch v := value.(type) {
	case int:
		return v
	case int64:
		return int(v)
	case float64:
		return int(v)
	case json.Number:
		i, _ := v.Int64()
		return int(i)
	default:
		return 0
	}
}
