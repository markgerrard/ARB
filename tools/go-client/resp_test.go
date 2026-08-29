package main

import (
	"bufio"
	"strings"
	"testing"
)

func TestEncodeCommand(t *testing.T) {
	// RESP2: a command is an array of bulk strings.
	got := string(encodeCommand("GET", "k"))
	want := "*2\r\n$3\r\nGET\r\n$1\r\nk\r\n"
	if got != want {
		t.Errorf("encodeCommand:\n got %q\nwant %q", got, want)
	}
}

func TestEncodeCommandBinarySafe(t *testing.T) {
	// bulk strings are length-prefixed, so an arg with \r\n inside survives intact.
	got := string(encodeCommand("RPUSH", "inbox", "a\r\nb"))
	want := "*3\r\n$5\r\nRPUSH\r\n$5\r\ninbox\r\n$4\r\na\r\nb\r\n"
	if got != want {
		t.Errorf("binary-safe encode:\n got %q\nwant %q", got, want)
	}
}

func TestReadReplySimpleString(t *testing.T) {
	v, err := readReply(bufio.NewReader(strings.NewReader("+OK\r\n")))
	if err != nil {
		t.Fatal(err)
	}
	if v != "OK" {
		t.Errorf("simple string: got %v want OK", v)
	}
}

func TestReadReplyError(t *testing.T) {
	_, err := readReply(bufio.NewReader(strings.NewReader("-ERR no such key\r\n")))
	if err == nil || !strings.Contains(err.Error(), "no such key") {
		t.Errorf("error reply: got err=%v, want one containing 'no such key'", err)
	}
}

func TestReadReplyInteger(t *testing.T) {
	v, err := readReply(bufio.NewReader(strings.NewReader(":7\r\n")))
	if err != nil {
		t.Fatal(err)
	}
	if v != int64(7) {
		t.Errorf("integer: got %v (%T) want int64 7", v, v)
	}
}

func TestReadReplyBulkString(t *testing.T) {
	// length-prefixed; the payload may itself contain \r\n.
	v, err := readReply(bufio.NewReader(strings.NewReader("$6\r\nfo\r\nob\r\n")))
	if err != nil {
		t.Fatal(err)
	}
	if v != "fo\r\nob" {
		t.Errorf("bulk: got %q want %q", v, "fo\r\nob")
	}
}

func TestReadReplyNilBulk(t *testing.T) {
	v, err := readReply(bufio.NewReader(strings.NewReader("$-1\r\n")))
	if err != nil {
		t.Fatal(err)
	}
	if v != nil {
		t.Errorf("nil bulk: got %v want nil", v)
	}
}

func TestReadReplyArray(t *testing.T) {
	// BLPOP returns a 2-element array [key, value].
	v, err := readReply(bufio.NewReader(strings.NewReader("*2\r\n$5\r\ninbox\r\n$3\r\nval\r\n")))
	if err != nil {
		t.Fatal(err)
	}
	arr, ok := v.([]interface{})
	if !ok || len(arr) != 2 || arr[0] != "inbox" || arr[1] != "val" {
		t.Errorf("array: got %#v want [inbox val]", v)
	}
}

func TestReadReplyNilArray(t *testing.T) {
	// BLPOP timeout returns a nil array.
	v, err := readReply(bufio.NewReader(strings.NewReader("*-1\r\n")))
	if err != nil {
		t.Fatal(err)
	}
	if v != nil {
		t.Errorf("nil array: got %v want nil", v)
	}
}
