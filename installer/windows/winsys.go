package main

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// psQuote makes a PowerShell single-quoted string literal.
func psQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }

func powershell(script string) (string, error) {
	out, err := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
		"-Command", script).CombinedOutput()
	return strings.TrimSpace(string(out)), err
}

// signature describes a file's Authenticode signature.
type signature struct{ Status, Subject string }

func authenticode(path string) (signature, error) {
	out, err := powershell("$s = Get-AuthenticodeSignature -LiteralPath " + psQuote(path) +
		"; Write-Output ($s.Status.ToString() + '|' + $s.SignerCertificate.Subject)")
	if err != nil {
		return signature{}, fmt.Errorf("checking the signature of %s: %v %s", filepath.Base(path), err, out)
	}
	return parseSignature(out), nil
}

func parseSignature(out string) signature {
	lines := strings.Split(strings.TrimSpace(out), "\n")
	status, subject, _ := strings.Cut(strings.TrimSpace(lines[len(lines)-1]), "|")
	return signature{strings.TrimSpace(status), strings.TrimSpace(subject)}
}

// checkSigned enforces a valid signature from the expected publisher. With allowUnsigned, a file with no
// signature at all is accepted (used for vendors that don't sign every file); a broken or foreign signature never is.
func checkSigned(sig signature, publisher string, allowUnsigned bool) error {
	switch {
	case sig.Status == "Valid" && strings.Contains(sig.Subject, "O="+publisher):
		return nil
	case sig.Status == "Valid":
		return fmt.Errorf("signed by an unexpected publisher (%s), expected %s", sig.Subject, publisher)
	case sig.Status == "NotSigned" && allowUnsigned:
		return nil
	default:
		return fmt.Errorf("signature check failed (%s), expected a valid signature from %s", sig.Status, publisher)
	}
}

func verifySigned(path, publisher string, allowUnsigned bool, log io.Writer) error {
	sig, err := authenticode(path)
	if err != nil {
		return err
	}
	if err := checkSigned(sig, publisher, allowUnsigned); err != nil {
		return fmt.Errorf("%s: %w", filepath.Base(path), err)
	}
	if sig.Status == "NotSigned" {
		fmt.Fprintf(log, "    note: %s carries no digital signature (downloaded over HTTPS from the vendor)\n", filepath.Base(path))
	}
	return nil
}

// runElevated runs an installer with administrator rights (Windows shows its permission prompt) and
// returns its exit code.
func runElevated(exe string, args string) (int, error) {
	script := "try { $p = Start-Process -FilePath " + psQuote(exe) + " -ArgumentList " + psQuote(args) +
		" -Verb RunAs -Wait -PassThru -ErrorAction Stop; exit $p.ExitCode } " +
		"catch { Write-Output ('" + elevateFailed + "' + $_.Exception.Message); exit 1 }"
	out, err := powershell(script)
	return elevatedResult(out, err)
}

const elevateFailed = "NQ-ELEVATE-FAILED: "

func elevatedResult(out string, err error) (int, error) {
	if i := strings.Index(out, elevateFailed); i >= 0 {
		return -1, fmt.Errorf("could not get administrator permission: %s", firstLine(out[i+len(elevateFailed):]))
	}
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			return ee.ExitCode(), nil
		}
		return -1, err
	}
	return 0, nil
}

// run executes a program, sending its output to the log.
func run(log io.Writer, dir string, name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.Dir = dir
	cmd.Stdout, cmd.Stderr = log, log
	return cmd.Run()
}

func output(name string, args ...string) (string, error) {
	var b bytes.Buffer
	cmd := exec.Command(name, args...)
	cmd.Stdout, cmd.Stderr = &b, &b
	err := cmd.Run()
	return strings.TrimSpace(b.String()), err
}

// vcRuntimeInstalled reads the registry flag the Visual C++ 2015-2022 x64 runtime sets.
func vcRuntimeInstalled() bool {
	out, err := output("reg", "query", `HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64`, "/v", "Installed")
	return err == nil && strings.Contains(out, "0x1")
}

var pyVersionRe = regexp.MustCompile(`^(\d+)\.(\d+)\|(.+)$`)

// parsePython reads "3.13|C:\path\python.exe" and returns the interpreter path if it is 3.11 or newer.
func parsePython(out string) (string, bool) {
	m := pyVersionRe.FindStringSubmatch(strings.TrimSpace(out))
	if m == nil {
		return "", false
	}
	major, _ := strconv.Atoi(m[1])
	minor, _ := strconv.Atoi(m[2])
	if major != 3 || minor < 11 {
		return "", false
	}
	return strings.TrimSpace(m[3]), true
}

// findPython looks for a usable Python 3.11+, preferring the per-user python.org install.
func findPython() (string, bool) {
	probe := []string{"-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}|{sys.executable}')"}
	var candidates [][]string
	if local := os.Getenv("LOCALAPPDATA"); local != "" {
		for _, v := range []string{"313", "312", "311"} {
			candidates = append(candidates, []string{filepath.Join(local, "Programs", "Python", "Python"+v, "python.exe")})
		}
	}
	candidates = append(candidates, []string{"py", "-3.13"}, []string{"py", "-3"}, []string{"python"})
	for _, c := range candidates {
		if strings.Contains(c[0], string(os.PathSeparator)) {
			if _, err := os.Stat(c[0]); err != nil {
				continue
			}
		}
		// The Microsoft Store "python" alias only opens the Store; it fails this probe and is skipped.
		if out, err := output(c[0], append(c[1:], probe...)...); err == nil {
			if path, ok := parsePython(lastLine(out)); ok {
				return path, true
			}
		}
	}
	return "", false
}

// restrictToOwner gives only the current user access to a file (it holds secrets).
func restrictToOwner(path string) {
	if user := os.Getenv("USERNAME"); user != "" {
		_ = exec.Command("icacls", path, "/inheritance:r", "/grant:r", user+":F").Run()
	}
}

func firstLine(s string) string {
	line, _, _ := strings.Cut(strings.TrimSpace(s), "\n")
	return strings.TrimSpace(line)
}

func lastLine(s string) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	return strings.TrimSpace(lines[len(lines)-1])
}
