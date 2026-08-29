package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
)

type Frame struct {
	Event string
	ID    string
	Data  map[string]any
}

// streamFatalEvent is an in-band sentinel streamSSE emits on a 4xx response so the model
// can stop reconnecting (auth/not-found won't fix itself) — matches Python's 4xx loop break.
const streamFatalEvent = "__arb_fatal__"

var (
	redisIDRE     = regexp.MustCompile(`^\d+-\d+$`)
	seatResumeRE  = regexp.MustCompile(`^e=([^;]*);t=([^;]*)$`)
	defaultHTTPDo = http.DefaultClient.Do
)

func fetchOrchestrators(baseURL, token string) ([]string, error) {
	endpoint, err := url.JoinPath(strings.TrimRight(baseURL, "/"), "orchestrators")
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequest(http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("fetch orchestrators: %s", resp.Status)
	}
	var payload struct {
		Orchestrators []string `json:"orchestrators"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, err
	}
	return payload.Orchestrators, nil
}

type historyResponse struct {
	Seats      []map[string]any `json:"seats"`
	NextCursor *string          `json:"next_cursor"`
	HasMore    bool             `json:"has_more"`
}

func fetchSeatsHistory(baseURL, token, orchestrator, cursor string) (historyResponse, error) {
	endpoint, err := url.JoinPath(strings.TrimRight(baseURL, "/"), "orchestrators", orchestrator, "seats", "history")
	if err != nil {
		return historyResponse{}, err
	}
	if cursor != "" {
		q := url.Values{}
		q.Set("cursor", cursor)
		endpoint += "?" + q.Encode()
	}
	req, err := http.NewRequest(http.MethodGet, endpoint, nil)
	if err != nil {
		return historyResponse{}, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return historyResponse{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return historyResponse{}, fmt.Errorf("fetch seats history: %s", resp.Status)
	}
	var payload historyResponse
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return historyResponse{}, err
	}
	return payload, nil
}

func fetchHistoryCmd(baseURL, token, orchestrator, cursor string, appendPage bool, gen int) tea.Cmd {
	return func() tea.Msg {
		resp, err := fetchSeatsHistory(baseURL, token, orchestrator, cursor)
		if err != nil {
			return historyErrMsg{gen: gen, err: err}
		}
		next := ""
		if resp.NextCursor != nil {
			next = *resp.NextCursor
		}
		return historyPageMsg{gen: gen, seats: resp.Seats, nextCursor: next, hasMore: resp.HasMore, append: appendPage}
	}
}

func streamSSE(ctx context.Context, rawURL, token, lastID string, ch chan<- Frame) {
	defer close(ch)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return
	}
	req.Header.Set("Authorization", "Bearer "+token)
	if isResumableID(lastID) {
		req.Header.Set("Last-Event-ID", lastID)
	}
	resp, err := defaultHTTPDo(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 && resp.StatusCode < 500 {
		// Auth/not-found — reconnecting won't help. Signal the model to stop (Python breaks).
		select {
		case ch <- Frame{Event: streamFatalEvent, Data: map[string]any{"status": float64(resp.StatusCode)}}:
		case <-ctx.Done():
		}
		return
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return
	}
	parseSSE(ctx, resp.Body, ch)
}

func parseSSE(ctx context.Context, reader io.Reader, ch chan<- Frame) {
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	var lines []string
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if frame, ok := parseFrame(lines); ok {
				select {
				case ch <- frame:
				case <-ctx.Done():
					return
				}
			}
			lines = nil
			continue
		}
		lines = append(lines, line)
	}
}

func parseFrame(lines []string) (Frame, bool) {
	var frame Frame
	var data bytes.Buffer
	for _, line := range lines {
		if line == "" || strings.HasPrefix(line, ":") {
			continue
		}
		field, value, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		value = strings.TrimPrefix(value, " ")
		switch field {
		case "id":
			frame.ID = value
		case "event":
			frame.Event = value
		case "data":
			if data.Len() > 0 {
				data.WriteByte('\n')
			}
			data.WriteString(value)
		}
	}
	if frame.ID == "" && frame.Event == "" && data.Len() == 0 {
		return Frame{}, false
	}
	if data.Len() > 0 {
		var decoded map[string]any
		if err := json.Unmarshal(data.Bytes(), &decoded); err != nil {
			return Frame{}, false
		}
		frame.Data = decoded
	} else {
		frame.Data = map[string]any{}
	}
	return frame, true
}

func isResumableID(id string) bool {
	if redisIDRE.MatchString(id) {
		return true
	}
	match := seatResumeRE.FindStringSubmatch(id)
	if match == nil {
		return false
	}
	return match[1] != "-" || match[2] != "-"
}
