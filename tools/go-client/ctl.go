package main

import (
	"encoding/json"
	"fmt"
	"os"
)

// ctlStatus prints the task status hash as a JSON object (mirror ctl.py status,
// reading task:<id>:status). Returns 0 if found, 1 if absent.
func ctlStatus(cfg *Config, taskID string) (int, error) {
	c, err := dial(cfg)
	if err != nil {
		return 1, err
	}
	defer c.Close()
	reply, err := c.Do("HGETALL", cfg.statusKey(taskID))
	if err != nil {
		return 1, err
	}
	arr, _ := reply.([]interface{})
	if len(arr) == 0 {
		fmt.Fprintf(os.Stderr, "no status for task %s\n", taskID)
		return 1, nil
	}
	m := stringMapFromHgetall(arr)
	b, _ := json.MarshalIndent(m, "", "  ")
	fmt.Println(string(b))
	return 0, nil
}

func stringMapFromHgetall(arr []interface{}) map[string]string {
	m := map[string]string{}
	for i := 0; i+1 < len(arr); i += 2 {
		k, _ := arr[i].(string)
		v, _ := arr[i+1].(string)
		m[k] = v
	}
	return m
}

// ctlResult prints the task result body (mirror ctl.py result, reading
// task:<id>:result). Returns 0 if present, 1 if not yet written.
func ctlResult(cfg *Config, taskID string) (int, error) {
	c, err := dial(cfg)
	if err != nil {
		return 1, err
	}
	defer c.Close()
	reply, err := c.Do("GET", cfg.resultKey(taskID))
	if err != nil {
		return 1, err
	}
	if reply == nil {
		fmt.Fprintf(os.Stderr, "no result yet for task %s\n", taskID)
		return 1, nil
	}
	body, ok := reply.(string)
	if !ok {
		return 1, fmt.Errorf("unexpected result type %T for task %s", reply, taskID)
	}
	fmt.Println(body)
	return 0, nil
}
