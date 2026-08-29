package main

import (
	"fmt"
	"io"
)

type stallPrintState struct {
	lastPrinted string
}

// maybePrintStallLineOK prints one [stall] line per distinct episode. `ok`
// reports whether the status poll actually succeeded: on a failed poll
// (ok=false) the state is left untouched, so a transient Redis error can
// neither clear a live episode nor re-arm a duplicate line for it (GO-1).
func maybePrintStallLineOK(w io.Writer, state *stallPrintState, status map[string]string, ok bool) {
	if !ok {
		return
	}
	stalledAt := status["stalled_at"]
	if stalledAt == "" {
		state.lastPrinted = ""
		return
	}
	if stalledAt == state.lastPrinted {
		return
	}
	state.lastPrinted = stalledAt
	fmt.Fprintf(w, "[stall] task %s seat %s: no progress since %s\n", status["task_id"], status["seat_id"], stalledAt)
}

// maybePrintStallLine is the successful-poll shorthand (kept for the existing
// episode/never-stalled tests).
func maybePrintStallLine(w io.Writer, state *stallPrintState, status map[string]string) {
	maybePrintStallLineOK(w, state, status, true)
}

// pollTaskStatus returns the seat's status hash and whether the poll succeeded.
// A false second return means "could not determine" — NOT "no stall" — so the
// caller must not treat it as a cleared episode (GO-1).
func pollTaskStatus(c redisConn, cfg *Config, taskID string) (map[string]string, bool) {
	reply, err := c.Do("HGETALL", cfg.statusKey(taskID))
	if err != nil {
		return nil, false
	}
	arr, _ := reply.([]interface{})
	return stringMapFromHgetall(arr), true
}
