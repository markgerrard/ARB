package main

import (
	"flag"
	"fmt"
	"os"

	tea "github.com/charmbracelet/bubbletea"
)

func main() {
	baseURL := flag.String("base-url", "http://127.0.0.1:8000", "visibility gateway base URL")
	token := flag.String("token", os.Getenv("ARB_VISIBILITY_TOKEN"), "visibility bearer token")
	orchestrator := flag.String("orchestrator", "", "orchestrator session to open directly")
	noMouse := flag.Bool("no-mouse", false, "disable mouse capture (lets the terminal handle native text selection / scrollback)")
	flag.Parse()

	if *token == "" {
		fmt.Fprintln(os.Stderr, "--token or ARB_VISIBILITY_TOKEN is required")
		os.Exit(2)
	}

	// AltScreen so the app owns the display (no terminal scrollback), and mouse capture
	// so the scroll wheel scrolls the transcript instead of the terminal scrolling the
	// app out — matches Textual's behaviour. --no-mouse omits the capture (parity with
	// Python's --no-mouse) so mosh/iTerm2 native selection works.
	opts := []tea.ProgramOption{tea.WithAltScreen()}
	if !*noMouse {
		opts = append(opts, tea.WithMouseCellMotion())
	}
	program := tea.NewProgram(newModel(*baseURL, *token, *orchestrator), opts...)
	if _, err := program.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "arb-watch-go: %v\n", err)
		os.Exit(1)
	}
}
