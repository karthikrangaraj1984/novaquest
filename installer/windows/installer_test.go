package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

func makeZip(t *testing.T, files map[string]string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "a.zip")
	f, _ := os.Create(path)
	w := zip.NewWriter(f)
	for name, body := range files {
		fw, err := w.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		fw.Write([]byte(body))
	}
	w.Close()
	f.Close()
	return path
}

func TestUnzipStripsPrefixSkipsAndRefusesEscapes(t *testing.T) {
	z := makeZip(t, map[string]string{
		"pgsql/bin/postgres.exe":      "pg",
		"pgsql/share/extension/x.sql": "sql",
		"pgsql/pgAdmin 4/big.dll":     "skip me",
		"pgsql/symbols/postgres.pdb":  "skip me",
		"other/readme.txt":            "outside prefix",
	})
	dest := t.TempDir()
	n, err := unzip(z, dest, "pgsql/", pgSkipPrefixes)
	if err != nil || n != 2 {
		t.Fatalf("n=%d err=%v", n, err)
	}
	if b, _ := os.ReadFile(filepath.Join(dest, "bin", "postgres.exe")); string(b) != "pg" {
		t.Fatal("postgres.exe not extracted")
	}
	for _, p := range []string{"pgAdmin 4", "symbols", "readme.txt", "other"} {
		if fileExists(filepath.Join(dest, p)) {
			t.Fatalf("%s should not be extracted", p)
		}
	}
	evil := makeZip(t, map[string]string{"../../evil.txt": "x"})
	if _, err := unzip(evil, t.TempDir(), "", nil); err == nil || !strings.Contains(err.Error(), "unsafe path") {
		t.Fatalf("zip-slip not refused: %v", err)
	}
	notZip := filepath.Join(t.TempDir(), "x.zip")
	os.WriteFile(notZip, []byte("<html>error page</html>"), 0o644)
	if _, err := unzip(notZip, t.TempDir(), "", nil); !errors.Is(err, errNotZip) {
		t.Fatalf("want errNotZip, got %v", err)
	}
}

func TestExtractEntryAndFind(t *testing.T) {
	z := makeZip(t, map[string]string{"caddy.exe": "binary", "LICENSE": "l",
		"poppler-26.09.0/Library/bin/pdftotext.exe": "p"})
	target := filepath.Join(t.TempDir(), "caddy.exe")
	os.WriteFile(target, []byte("old"), 0o755)
	if err := extractEntry(z, "caddy.exe", target); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(target); string(b) != "binary" {
		t.Fatal("not replaced")
	}
	if err := extractEntry(z, "missing.exe", target); err == nil {
		t.Fatal("missing entry accepted")
	}
	name, err := findInZip(z, "Library/bin/pdftotext.exe")
	if err != nil || name != "poppler-26.09.0/Library/bin/pdftotext.exe" {
		t.Fatalf("%q %v", name, err)
	}
}

func TestUpdateEnvKeepsKeyAndFillsSecret(t *testing.T) {
	example := "# comment\nNQ_SECRET_KEY=change-me-to-a-long-random-string\nANTHROPIC_API_KEY=sk-ant-keep-me\n" +
		"# NQ_PG_BIN=/usr/lib/postgresql/16/bin\n# NQ_PG_DATA=/var/lib/novaquest/pgdata\n"
	out := updateEnv(example, map[string]string{"NQ_PG_BIN": `C:\nq\pgsql\bin`, "NQ_PG_DATA": `C:\nq\pgdata`, "NQ_BIN_DIR": `C:\nq\bin`})
	for _, want := range []string{"ANTHROPIC_API_KEY=sk-ant-keep-me\n", "NQ_PG_BIN=C:\\nq\\pgsql\\bin\n",
		"NQ_PG_DATA=C:\\nq\\pgdata\n", "NQ_BIN_DIR=C:\\nq\\bin\n", "# comment\n"} {
		if !strings.Contains(out, want) {
			t.Fatalf("missing %q in:\n%s", want, out)
		}
	}
	if strings.Contains(out, secretPlaceholder) || strings.Contains(out, "/usr/lib/postgresql") {
		t.Fatalf("placeholder or example left:\n%s", out)
	}
	if strings.Count(out, "NQ_PG_BIN=") != 1 {
		t.Fatal("duplicate key")
	}
	// Running again changes nothing (the secret is kept once it is real).
	if again := updateEnv(out, map[string]string{"NQ_PG_BIN": `C:\nq\pgsql\bin`, "NQ_PG_DATA": `C:\nq\pgdata`, "NQ_BIN_DIR": `C:\nq\bin`}); again != out {
		t.Fatalf("not idempotent:\n%s\n---\n%s", out, again)
	}
	// Windows line endings and an empty file are handled.
	if got := updateEnv("A=1\r\nB=2\r\n", map[string]string{"B": "3"}); !strings.HasPrefix(got, "A=1\nB=3\nNQ_SECRET_KEY=") {
		t.Fatalf("got %q", got)
	}
	if got := updateEnv("", map[string]string{"X": "1"}); !strings.HasPrefix(got, "X=1\nNQ_SECRET_KEY=") || len(got) != len("X=1\nNQ_SECRET_KEY=")+64+1 {
		t.Fatalf("got %q", got)
	}
}

func TestWriteEnvFileUsesExample(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, ".env.example"), []byte("NQ_SECRET_KEY=change-me-to-a-long-random-string\nANTHROPIC_API_KEY=\n"), 0o644)
	if err := writeEnvFile(filepath.Join(dir, ".env"), filepath.Join(dir, ".env.example"), map[string]string{"NQ_BIN_DIR": "b"}); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(filepath.Join(dir, ".env"))
	if !strings.Contains(string(b), "NQ_BIN_DIR=b\n") || !strings.Contains(string(b), "ANTHROPIC_API_KEY=\n") {
		t.Fatalf("%s", b)
	}
	if st, _ := os.Stat(filepath.Join(dir, ".env")); st.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v", st.Mode().Perm())
	}
}

func TestPickAssetNeedsDigest(t *testing.T) {
	rel := ghRelease{}
	rel.Assets = append(rel.Assets, struct {
		Name   string `json:"name"`
		URL    string `json:"browser_download_url"`
		Digest string `json:"digest"`
	}{"caddy_2.10.2_windows_amd64.zip", "https://x/c.zip", "sha256:" + strings.Repeat("AB", 32)},
		struct {
			Name   string `json:"name"`
			URL    string `json:"browser_download_url"`
			Digest string `json:"digest"`
		}{"old.zip", "https://x/o.zip", ""})
	url, sha, err := pickAsset(rel, caddyAsset)
	if err != nil || url != "https://x/c.zip" || sha != strings.Repeat("ab", 32) {
		t.Fatal(url, sha, err)
	}
	if _, _, err := pickAsset(rel, ghAsset{"Old", "o/o", "v1", "old.zip"}); err == nil || !strings.Contains(err.Error(), "no sha256") {
		t.Fatalf("asset without digest accepted: %v", err)
	}
	if _, _, err := pickAsset(rel, ghAsset{"M", "o/o", "v1", "missing.zip"}); err == nil {
		t.Fatal("missing asset accepted")
	}
}

func TestDownloadVerifiesRetriesAndCaches(t *testing.T) {
	body := []byte("tool archive bytes")
	sum := sha256.Sum256(body)
	want := hex.EncodeToString(sum[:])
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/flaky":
			if calls.Add(1) == 1 {
				w.WriteHeader(502)
				return
			}
			w.Write(body)
		case "/tampered":
			w.Write([]byte("something else"))
		case "/repos/caddyserver/caddy/releases/tags/v2.10.2":
			fmt.Fprintf(w, `{"assets":[{"name":"caddy_2.10.2_windows_amd64.zip","browser_download_url":"%s/flaky","digest":"sha256:%s"}]}`, "http://"+r.Host, want)
		default:
			w.WriteHeader(404)
		}
	}))
	defer srv.Close()
	githubAPI = srv.URL
	url, sha, err := resolveAsset(caddyAsset)
	if err != nil || sha != want {
		t.Fatal(err)
	}
	dest := filepath.Join(t.TempDir(), "dl", "c.zip")
	var log bytes.Buffer
	if err := download(url, dest, sha, &log); err != nil {
		t.Fatal(err, log.String())
	}
	if calls.Load() != 2 || !strings.Contains(log.String(), "retrying") {
		t.Fatalf("expected one retry, calls=%d log=%s", calls.Load(), log.String())
	}
	if err := download(url, dest, sha, &log); err != nil || calls.Load() != 2 || !strings.Contains(log.String(), "downloaded earlier") {
		t.Fatalf("cache not used: %v calls=%d", err, calls.Load())
	}
	bad := filepath.Join(t.TempDir(), "t.zip")
	if err := download(srv.URL+"/tampered", bad, want, io.Discard); err == nil || !strings.Contains(err.Error(), "corrupt or was tampered") {
		t.Fatalf("tampered file accepted: %v", err)
	}
	if fileExists(bad) || fileExists(bad+".part") {
		t.Fatal("tampered file left behind")
	}
	if _, _, err := resolveAsset(mailpitAsset); err == nil || !strings.Contains(err.Error(), "HTTP 404") {
		t.Fatalf("missing release not reported: %v", err)
	}
}

func TestSignaturePolicy(t *testing.T) {
	ms := parseSignature("Valid|CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, S=Washington, C=US")
	if err := checkSigned(ms, microsoftSigner, false); err != nil {
		t.Fatal(err)
	}
	if err := checkSigned(ms, pythonSigner, false); err == nil {
		t.Fatal("wrong publisher accepted")
	}
	unsigned := parseSignature("some warning\nNotSigned|")
	if unsigned.Status != "NotSigned" {
		t.Fatalf("%+v", unsigned)
	}
	if checkSigned(unsigned, "EnterpriseDB Corporation", true) != nil || checkSigned(unsigned, microsoftSigner, false) == nil {
		t.Fatal("unsigned policy wrong")
	}
	if checkSigned(parseSignature("HashMismatch|CN=x, O=EnterpriseDB Corporation"), "EnterpriseDB Corporation", true) == nil {
		t.Fatal("tampered signature accepted")
	}
}

func TestElevationResult(t *testing.T) {
	if _, err := elevatedResult("NQ-ELEVATE-FAILED: The operation was canceled by the user.", errors.New("exit 1")); err == nil ||
		!strings.Contains(err.Error(), "canceled by the user") {
		t.Fatalf("cancel not reported: %v", err)
	}
	if code, err := elevatedResult("", nil); code != 0 || err != nil {
		t.Fatal(code, err)
	}
	ee := exec.Command("sh", "-c", "exit 7").Run()
	if code, err := elevatedResult("", ee); code != 7 || err != nil {
		t.Fatal(code, err)
	}
	if got := psQuote(`C:\Users\O'Neil\x.exe`); got != `'C:\Users\O''Neil\x.exe'` {
		t.Fatal(got)
	}
}

func TestParsePython(t *testing.T) {
	cases := map[string]bool{"3.13|C:\\Py\\python.exe": true, "3.11|/usr/bin/python3": true, "3.10|C:\\old.exe": false,
		"2.7|C:\\py27.exe": false, "Python was not found; run without arguments to install from the Microsoft Store": false}
	for in, ok := range cases {
		if _, got := parsePython(in); got != ok {
			t.Errorf("%q: got %v", in, got)
		}
	}
}

func TestBundledPgvectorSource(t *testing.T) {
	path := filepath.Join(t.TempDir(), "src.zip")
	os.WriteFile(path, pgvectorSource, 0o644)
	for _, f := range []string{"Makefile.win", "vector.control", "src/vector.c", "sql/vector.sql"} {
		if _, err := findInZip(path, f); err != nil {
			t.Error(err)
		}
	}
}

func TestRequirementsCheckScript(t *testing.T) {
	py, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 not available")
	}
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "r.txt"), []byte("# header\nnot-a-real-package-xyz==1.0\n"), 0o644)
	if exec.Command(py, "-c", requirementsCheck, filepath.Join(dir, "r.txt")).Run() == nil {
		t.Fatal("missing package passed the check")
	}
	os.WriteFile(filepath.Join(dir, "r.txt"), []byte("# header\npip=="+pipVersion(t, py)+"\n"), 0o644)
	if out, err := exec.Command(py, "-c", requirementsCheck, filepath.Join(dir, "r.txt")).CombinedOutput(); err != nil {
		t.Fatalf("installed package failed the check: %s", out)
	}
}

func pipVersion(t *testing.T, py string) string {
	out, err := exec.Command(py, "-c", "import importlib.metadata as m; print(m.version('pip'))").Output()
	if err != nil {
		t.Skip("pip metadata not available")
	}
	return strings.TrimSpace(string(out))
}

func TestRunStepsReportsAndContinues(t *testing.T) {
	var out bytes.Buffer
	in := &installer{out: &out, log: io.Discard, logPath: "install.log", appDir: ""}
	installed := false
	steps := []step{
		{"Done already", func() (bool, string) { return true, "v1" }, nil},
		{"Breaks", func() (bool, string) { return false, "" }, func() error { return errors.New("network down") }},
		{"Installs", func() (bool, string) { return installed, "" }, func() error { installed = true; return nil }},
	}
	if code := in.runSteps(steps, false); code != 1 {
		t.Fatalf("code %d", code)
	}
	for _, want := range []string{"[1/3] Done already: already installed (v1)", "[2/3] Breaks: installing…", "FAILED: network down",
		"[3/3] Installs: installing…", "    done", "1 step(s) failed"} {
		if !strings.Contains(out.String(), want) {
			t.Fatalf("missing %q in:\n%s", want, out.String())
		}
	}
	out.Reset()
	if code := in.runSteps(steps[2:], true); code != 0 || !strings.Contains(out.String(), "Everything is installed") {
		t.Fatalf("check mode: %d %s", code, out.String())
	}
}

func TestTesseractLocations(t *testing.T) {
	t.Setenv("ProgramFiles", `D:\Apps`)
	t.Setenv("LOCALAPPDATA", `C:\Users\k\AppData\Local`)
	got := strings.Join(tesseractExes(), "|")
	for _, want := range []string{filepath.Join(`D:\Apps`, "Tesseract-OCR", "tesseract.exe"),
		filepath.Join(`C:\Users\k\AppData\Local`, "Programs", "Tesseract-OCR", "tesseract.exe")} {
		if !strings.Contains(got, want) {
			t.Fatalf("%s missing from %s", want, got)
		}
	}
	if tesseractAsset.Repo != "tesseract-ocr/tesseract" || !strings.HasSuffix(tesseractAsset.Name, ".exe") {
		t.Fatalf("unexpected Tesseract asset %+v", tesseractAsset)
	}
}

func TestTessEnglishStep(t *testing.T) {
	in := &installer{home: t.TempDir(), out: io.Discard}
	if ok, _ := in.checkTessEnglish(); ok {
		t.Fatal("no English model yet")
	}
	if in.settings()["NQ_TESSDATA"] != in.tessdata() {
		t.Fatal(".env must point NovaQuest at its own tessdata folder")
	}
	os.MkdirAll(in.tessdata(), 0o755)
	os.WriteFile(filepath.Join(in.tessdata(), "eng.traineddata"), []byte("wrong file"), 0o644)
	if ok, _ := in.checkTessEnglish(); ok {
		t.Fatal("a file with the wrong sha256 is not accepted")
	}
	if !strings.HasPrefix(tessEngURL, "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/") || len(tessEngSHA256) != 64 {
		t.Fatal("English model must be pinned to a tessdata_fast commit and sha256")
	}
}
