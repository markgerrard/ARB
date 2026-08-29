package main

import (
	"bufio"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"net"
	"strconv"
	"time"
)

// Minimal RESP2 client — just the handful of commands the client edge needs
// (LLEN, HSET, EXPIRE, RPUSH, BLPOP, HGETALL, GET, plus AUTH/SELECT on connect).
// Hand-rolled (no external dep) so the client compiles to a zero-dependency static
// binary, which is the whole point of the Go client edge: dispatch from any host.

// encodeCommand renders args as a RESP array of bulk strings. Bulk strings are
// length-prefixed, so arbitrary bytes (incl. \r\n in a task body) survive intact.
func encodeCommand(args ...string) []byte {
	var b []byte
	b = append(b, '*')
	b = strconv.AppendInt(b, int64(len(args)), 10)
	b = append(b, '\r', '\n')
	for _, a := range args {
		b = append(b, '$')
		b = strconv.AppendInt(b, int64(len(a)), 10)
		b = append(b, '\r', '\n')
		b = append(b, a...)
		b = append(b, '\r', '\n')
	}
	return b
}

// readReply reads exactly one RESP2 reply. Returns string (simple/bulk), int64
// (integer), []interface{} (array), or nil (nil bulk/array); an error reply (-)
// becomes a Go error.
func readReply(r *bufio.Reader) (interface{}, error) {
	prefix, err := r.ReadByte()
	if err != nil {
		return nil, err
	}
	line, err := readLine(r)
	if err != nil {
		return nil, err
	}
	switch prefix {
	case '+': // simple string
		return line, nil
	case '-': // error
		return nil, errors.New(line)
	case ':': // integer
		return strconv.ParseInt(line, 10, 64)
	case '$': // bulk string
		n, err := strconv.Atoi(line)
		if err != nil {
			return nil, err
		}
		if n < 0 {
			return nil, nil // nil bulk
		}
		buf := make([]byte, n+2) // payload + trailing \r\n
		if _, err := io.ReadFull(r, buf); err != nil {
			return nil, err
		}
		return string(buf[:n]), nil
	case '*': // array
		n, err := strconv.Atoi(line)
		if err != nil {
			return nil, err
		}
		if n < 0 {
			return nil, nil // nil array (e.g. BLPOP timeout)
		}
		out := make([]interface{}, n)
		for i := 0; i < n; i++ {
			el, err := readReply(r)
			if err != nil {
				return nil, err
			}
			out[i] = el
		}
		return out, nil
	default:
		return nil, fmt.Errorf("resp: unknown reply type %q", prefix)
	}
}

// readLine reads up to and including \r\n and returns the content without it.
func readLine(r *bufio.Reader) (string, error) {
	s, err := r.ReadString('\n')
	if err != nil {
		return "", err
	}
	// strip trailing \r\n (or bare \n defensively)
	s = s[:len(s)-1]
	if len(s) > 0 && s[len(s)-1] == '\r' {
		s = s[:len(s)-1]
	}
	return s, nil
}

// Client is a single-connection RESP client.
type Client struct {
	conn net.Conn
	r    *bufio.Reader
}

// dial connects (TLS optional), authenticates, and SELECTs the db.
func dial(cfg *Config) (*Client, error) {
	addr := net.JoinHostPort(cfg.Host, cfg.Port)
	var conn net.Conn
	var err error
	d := &net.Dialer{Timeout: 10 * time.Second}
	if cfg.TLS {
		conn, err = tls.DialWithDialer(d, "tcp", addr, &tls.Config{ServerName: cfg.Host})
	} else {
		conn, err = d.Dial("tcp", addr)
	}
	if err != nil {
		return nil, fmt.Errorf("connect %s: %w", addr, err)
	}
	c := &Client{conn: conn, r: bufio.NewReader(conn)}
	if cfg.Password != "" {
		var reply interface{}
		if cfg.User != "" {
			reply, err = c.Do("AUTH", cfg.User, cfg.Password)
		} else {
			reply, err = c.Do("AUTH", cfg.Password)
		}
		if err != nil {
			c.Close()
			return nil, fmt.Errorf("AUTH: %w", err)
		}
		_ = reply
	}
	if cfg.DB != "" && cfg.DB != "0" {
		if _, err := c.Do("SELECT", cfg.DB); err != nil {
			c.Close()
			return nil, fmt.Errorf("SELECT %s: %w", cfg.DB, err)
		}
	}
	return c, nil
}

// Do sends one command and reads one reply.
func (c *Client) Do(args ...string) (interface{}, error) {
	if _, err := c.conn.Write(encodeCommand(args...)); err != nil {
		return nil, err
	}
	return readReply(c.r)
}

// DoBlocking sends a blocking command (BLPOP) and reads the reply, with a read
// deadline slightly past the server-side block so a dead connection can't hang.
func (c *Client) DoBlocking(blockSeconds int, args ...string) (interface{}, error) {
	if _, err := c.conn.Write(encodeCommand(args...)); err != nil {
		return nil, err
	}
	_ = c.conn.SetReadDeadline(time.Now().Add(time.Duration(blockSeconds+5) * time.Second))
	defer c.conn.SetReadDeadline(time.Time{})
	return readReply(c.r)
}

func (c *Client) Close() error { return c.conn.Close() }
