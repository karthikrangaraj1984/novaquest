package main

import (
	"crypto/rand"
	"encoding/hex"
	"os"
	"strings"
)

const secretPlaceholder = "change-me-to-a-long-random-string"

// updateEnv sets the given KEY=value pairs in .env text, keeping every other line (including the Claude API
// key, which the installer never touches). Commented-out examples ("# KEY=...") are replaced in place.
// A missing or placeholder NQ_SECRET_KEY gets a random value.
func updateEnv(text string, set map[string]string) string {
	lines := strings.Split(strings.ReplaceAll(strings.TrimRight(text, "\r\n"), "\r\n", "\n"), "\n")
	if text == "" {
		lines = nil
	}
	done := map[string]bool{}
	for i, line := range lines {
		trimmed := strings.TrimSpace(line)
		uncommented := strings.TrimSpace(strings.TrimPrefix(trimmed, "#"))
		key, value, ok := strings.Cut(uncommented, "=")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		if v, want := set[key]; want && !done[key] {
			lines[i] = key + "=" + v
			done[key] = true
		} else if key == "NQ_SECRET_KEY" && !strings.HasPrefix(trimmed, "#") {
			if v := strings.TrimSpace(value); v == "" || v == secretPlaceholder {
				lines[i] = "NQ_SECRET_KEY=" + randomHex(32)
			}
			done[key] = true
		}
	}
	for _, key := range sortedKeys(set) {
		if !done[key] {
			lines = append(lines, key+"="+set[key])
		}
	}
	if !done["NQ_SECRET_KEY"] {
		if _, given := set["NQ_SECRET_KEY"]; !given {
			lines = append(lines, "NQ_SECRET_KEY="+randomHex(32))
		}
	}
	return strings.Join(lines, "\n") + "\n"
}

func sortedKeys(m map[string]string) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	for i := 1; i < len(keys); i++ {
		for j := i; j > 0 && keys[j] < keys[j-1]; j-- {
			keys[j], keys[j-1] = keys[j-1], keys[j]
		}
	}
	return keys
}

func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

// writeEnvFile creates or updates the app's .env (from .env.example when missing).
func writeEnvFile(envPath, examplePath string, set map[string]string) error {
	text := ""
	if b, err := os.ReadFile(envPath); err == nil {
		text = string(b)
	} else if b, err := os.ReadFile(examplePath); err == nil {
		text = string(b)
	}
	if err := os.WriteFile(envPath, []byte(updateEnv(text, set)), 0o600); err != nil {
		return err
	}
	restrictToOwner(envPath)
	return nil
}
