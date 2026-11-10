// NovaQuest prerequisites installer for Windows 10/11 (64-bit).
//
// Sets up everything NovaQuest needs to run locally, without a Windows service and mostly without
// administrator rights (Windows asks for permission only to install Microsoft's runtime/build tools):
//
//	Microsoft Visual C++ runtime · Python 3.13 (per user) · PostgreSQL 16 (portable) · Mailpit · Caddy ·
//	Poppler (pdftotext) · Tesseract OCR (scanned books) · optionally pgvector · then the app's Python packages, .env and an `nq` command.
//
// Every download is verified: GitHub assets against the sha256 digest GitHub publishes, Microsoft and Python
// installers by their Authenticode signature. Running it again repairs or upgrades; finished parts are skipped.
package main

import (
	"crypto/sha256"
	_ "embed"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

//go:embed assets/pgvector-0.8.0-src.zip
var pgvectorSource []byte

type step struct {
	name    string
	check   func() (bool, string) // already done? and a short detail
	install func() error
}

type installer struct {
	home, appDir string
	withPgvector bool
	out          io.Writer // console + log file
	log          io.Writer // log file only (programs' own output)
	logPath      string
	python       string
}

func main() {
	var (
		dir       = flag.String("dir", "", "NovaQuest folder (default: the folder this installer is in, or the current folder)")
		pgvector  = flag.Bool("with-pgvector", false, "also build pgvector for Postgres (needs ~5 GB for Microsoft C++ Build Tools; only for optional embedding search)")
		checkOnly = flag.Bool("check", false, "only report what is installed")
		yes       = flag.Bool("yes", false, "don't wait for Enter at the end")
	)
	flag.Parse()
	code := realMain(*dir, *pgvector, *checkOnly)
	if !*yes {
		fmt.Print("\nPress Enter to close this window.")
		fmt.Scanln()
	}
	os.Exit(code)
}

func realMain(dir string, withPgvector, checkOnly bool) int {
	fmt.Printf("NovaQuest prerequisites installer %s\n\n", installerVersion)
	if runtime.GOOS != "windows" {
		fmt.Println("This installer is for Windows. On Linux or macOS, follow the README's quick start.")
		return 2
	}
	local := os.Getenv("LOCALAPPDATA")
	if local == "" {
		fmt.Println("LOCALAPPDATA is not set; cannot choose an install folder.")
		return 2
	}
	in := &installer{home: filepath.Join(local, "NovaQuest"), appDir: findAppDir(dir), withPgvector: withPgvector}
	if err := os.MkdirAll(in.home, 0o755); err != nil {
		fmt.Println("Cannot create", in.home, err)
		return 1
	}
	in.logPath = filepath.Join(in.home, "install.log")
	logFile, err := os.OpenFile(in.logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		fmt.Println("Cannot write", in.logPath, err)
		return 1
	}
	defer logFile.Close()
	fmt.Fprintf(logFile, "\n===== %s, installer %s, check=%v pgvector=%v app=%q\n", time.Now().Format(time.RFC3339), installerVersion, checkOnly, withPgvector, in.appDir)
	in.out, in.log = io.MultiWriter(os.Stdout, logFile), logFile
	if runtime.GOARCH != "amd64" {
		fmt.Fprintln(in.out, "Note: this is not a 64-bit Intel/AMD PC; the x64 programs will run under Windows' emulation.")
	}
	fmt.Fprintf(in.out, "Tools go into %s\n", in.home)
	if in.appDir != "" {
		fmt.Fprintf(in.out, "NovaQuest folder: %s\n\n", in.appDir)
	} else {
		fmt.Fprintf(in.out, "NovaQuest folder: not found (put this installer in the unzipped NovaQuest folder, or use -dir).\n"+
			"Only the tools will be installed.\n\n")
	}
	return in.runSteps(in.steps(), checkOnly)
}

func (in *installer) runSteps(steps []step, checkOnly bool) int {
	failed := 0
	for i, s := range steps {
		done, detail := s.check()
		label := fmt.Sprintf("[%d/%d] %s", i+1, len(steps), s.name)
		switch {
		case done:
			fmt.Fprintf(in.out, "%s: already installed%s\n", label, suffix(detail))
			continue
		case checkOnly:
			fmt.Fprintf(in.out, "%s: not installed%s\n", label, suffix(detail))
			failed++
			continue
		}
		fmt.Fprintf(in.out, "%s: installing…\n", label)
		if err := s.install(); err != nil {
			fmt.Fprintf(in.out, "    FAILED: %v\n", err)
			failed++
			continue
		}
		if ok, detail := s.check(); ok {
			fmt.Fprintf(in.out, "    done%s\n", suffix(detail))
		} else {
			fmt.Fprintf(in.out, "    FAILED: installed, but the check afterwards did not pass%s\n", suffix(detail))
			failed++
		}
	}
	fmt.Fprintln(in.out)
	if checkOnly {
		if failed == 0 {
			fmt.Fprintln(in.out, "Everything is installed.")
			return 0
		}
		fmt.Fprintf(in.out, "%d item(s) missing. Run the installer without -check to install them.\n", failed)
		return 1
	}
	if failed > 0 {
		fmt.Fprintf(in.out, "%d step(s) failed. Details are in %s\nFix the cause (often the internet connection) and run the installer again: finished steps are skipped.\n", failed, in.logPath)
		return 1
	}
	in.printNextSteps()
	return 0
}

func suffix(detail string) string {
	if detail == "" {
		return ""
	}
	return " (" + detail + ")"
}

func (in *installer) steps() []step {
	s := []step{
		{"Microsoft Visual C++ runtime", func() (bool, string) { return vcRuntimeInstalled(), "" }, in.installVCRuntime},
		{"Python " + pythonVersion[:4] + " (3.11 or later)", in.checkPython, in.installPython},
		{"PostgreSQL " + pgVersion, in.checkPostgres, in.installPostgres},
	}
	if in.withPgvector || fileExists(in.vectorControl()) {
		s = append(s, step{"pgvector " + pgvectorVersion + " (optional)", in.checkPgvector, in.installPgvector})
	}
	s = append(s,
		step{mailpitAsset.Label + " (local email)", in.checkTool("mailpit", "version", "v1.27.10"), in.installTool(mailpitAsset, "mailpit.exe")},
		step{caddyAsset.Label + " (HTTPS)", in.checkTool("caddy", "version", "v2.10.2"), in.installTool(caddyAsset, "caddy.exe")},
		step{popplerAsset.Label + " (pdftotext)", in.checkPoppler, in.installPoppler},
		step{tesseractAsset.Label + " (reads scanned books)", checkTesseract, in.installTesseract},
		step{"English for Tesseract OCR", in.checkTessEnglish, in.installTessEnglish},
	)
	if in.appDir != "" {
		s = append(s,
			step{"NovaQuest Python packages", in.checkPackages, in.installPackages},
			step{"NovaQuest settings (.env) and the nq command", in.checkSettings, in.writeSettings},
		)
	}
	return s
}

func (in *installer) downloads() string  { return filepath.Join(in.home, "downloads") }
func (in *installer) pgBin() string      { return filepath.Join(in.home, "pgsql", "bin") }
func (in *installer) toolBin() string    { return filepath.Join(in.home, "bin") }
func (in *installer) popplerBin() string { return filepath.Join(in.home, "poppler", "Library", "bin") }
func (in *installer) tessdata() string   { return filepath.Join(in.home, "tessdata") }
func (in *installer) vectorControl() string {
	return filepath.Join(in.home, "pgsql", "share", "extension", "vector.control")
}
func (in *installer) venvPython() string {
	return filepath.Join(in.appDir, ".venv", "Scripts", "python.exe")
}

// ---------- Microsoft Visual C++ runtime ----------

func (in *installer) installVCRuntime() error {
	exe := filepath.Join(in.downloads(), "vc_redist.x64.exe")
	if err := download(vcRedistURL, exe, "", in.out); err != nil {
		return err
	}
	if err := verifySigned(exe, microsoftSigner, false, in.out); err != nil {
		return err
	}
	fmt.Fprintln(in.out, "    Windows will ask for permission to install it.")
	code, err := runElevated(exe, "/install /quiet /norestart")
	if err != nil {
		return err
	}
	if code != 0 && code != 3010 && code != 1638 { // 3010: restart later, 1638: a newer one is installed
		return fmt.Errorf("the Visual C++ installer stopped with code %d", code)
	}
	return nil
}

// ---------- Python ----------

func (in *installer) checkPython() (bool, string) {
	path, ok := findPython()
	if ok {
		in.python = path
	}
	return ok, path
}

func (in *installer) installPython() error {
	exe := filepath.Join(in.downloads(), filepath.Base(pythonURL))
	if err := download(pythonURL, exe, "", in.out); err != nil {
		return err
	}
	if err := verifySigned(exe, pythonSigner, false, in.out); err != nil {
		return err
	}
	// Per-user install: no administrator rights needed; adds Python to this user's PATH.
	err := run(in.log, "", exe, "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1",
		"InstallLauncherAllUsers=0", "Include_test=0", "Include_doc=0", "Shortcuts=0")
	if err != nil {
		return fmt.Errorf("the Python installer failed: %v", err)
	}
	return nil
}

// ---------- PostgreSQL ----------

func (in *installer) checkPostgres() (bool, string) {
	exe := filepath.Join(in.pgBin(), "postgres.exe")
	if !fileExists(exe) {
		return false, ""
	}
	out, err := output(exe, "--version")
	return err == nil && strings.Contains(out, " "+pgVersion), out
}

func (in *installer) installPostgres() error {
	zipPath := filepath.Join(in.downloads(), "postgresql-"+pgVersion+"-windows-x64-binaries.zip")
	if _, err := findInZip(zipPath, "pgsql/bin/postgres.exe"); err != nil {
		var last error
		for _, url := range pgURLs {
			if last = download(url, zipPath, "", in.out); last == nil {
				if _, last = findInZip(zipPath, "pgsql/bin/postgres.exe"); last == nil {
					break
				}
				os.Remove(zipPath)
			}
		}
		if last != nil {
			return fmt.Errorf("could not download PostgreSQL %s: %w", pgVersion, last)
		}
	}
	tmp := filepath.Join(in.home, "pgsql.new")
	os.RemoveAll(tmp)
	fmt.Fprintln(in.out, "    unpacking…")
	if _, err := unzip(zipPath, tmp, "pgsql/", pgSkipPrefixes); err != nil {
		return err
	}
	if err := verifySigned(filepath.Join(tmp, "bin", "postgres.exe"), "EnterpriseDB Corporation", true, in.out); err != nil {
		os.RemoveAll(tmp)
		return err
	}
	final := filepath.Join(in.home, "pgsql")
	if err := os.RemoveAll(final); err != nil {
		return fmt.Errorf("cannot replace %s (is NovaQuest still running? run `nq down` first): %w", final, err)
	}
	return os.Rename(tmp, final)
}

// ---------- pgvector (optional) ----------

func (in *installer) checkPgvector() (bool, string) {
	return fileExists(in.vectorControl()) && fileExists(filepath.Join(in.home, "pgsql", "lib", "vector.dll")), ""
}

func vcvarsPath() string {
	vswhere := filepath.Join(os.Getenv("ProgramFiles(x86)"), "Microsoft Visual Studio", "Installer", "vswhere.exe")
	if !fileExists(vswhere) {
		return ""
	}
	out, err := output(vswhere, "-latest", "-products", "*", "-requires",
		"Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath")
	if err != nil || out == "" {
		return ""
	}
	bat := filepath.Join(lastLine(out), "VC", "Auxiliary", "Build", "vcvars64.bat")
	if !fileExists(bat) {
		return ""
	}
	return bat
}

func (in *installer) installPgvector() error {
	if !fileExists(filepath.Join(in.pgBin(), "postgres.exe")) {
		return fmt.Errorf("PostgreSQL must be installed first")
	}
	vcvars := vcvarsPath()
	if vcvars == "" {
		exe := filepath.Join(in.downloads(), "vs_BuildTools.exe")
		if err := download(vsBuildToolsURL, exe, "", in.out); err != nil {
			return err
		}
		if err := verifySigned(exe, microsoftSigner, false, in.out); err != nil {
			return err
		}
		fmt.Fprintln(in.out, "    installing Microsoft C++ Build Tools (large download, can take 15+ minutes). Windows will ask for permission.")
		code, err := runElevated(exe, "--quiet --wait --norestart --nocache --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended")
		if err != nil {
			return err
		}
		if code != 0 && code != 3010 {
			return fmt.Errorf("the Build Tools installer stopped with code %d", code)
		}
		if vcvars = vcvarsPath(); vcvars == "" {
			return fmt.Errorf("C++ Build Tools were installed but the compiler was not found; restart Windows and run the installer again")
		}
	}
	build := filepath.Join(in.home, "build")
	src := filepath.Join(build, "pgvector-"+pgvectorVersion)
	os.RemoveAll(src)
	srcZip := filepath.Join(build, "pgvector-"+pgvectorVersion+"-src.zip")
	if err := os.MkdirAll(build, 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(srcZip, pgvectorSource, 0o644); err != nil {
		return err
	}
	if _, err := unzip(srcZip, src, "", nil); err != nil {
		return err
	}
	script := filepath.Join(build, "build-pgvector.cmd")
	body := strings.Join([]string{
		"@echo off",
		`call "` + vcvars + `" || exit /b 1`,
		`set "PGROOT=` + filepath.Join(in.home, "pgsql") + `"`,
		`cd /d "` + src + `" || exit /b 1`,
		"nmake /nologo /F Makefile.win || exit /b 1",
		"nmake /nologo /F Makefile.win install || exit /b 1",
	}, "\r\n") + "\r\n"
	if err := os.WriteFile(script, []byte(body), 0o644); err != nil {
		return err
	}
	fmt.Fprintln(in.out, "    compiling pgvector…")
	if err := run(in.log, build, "cmd.exe", "/c", script); err != nil {
		return fmt.Errorf("building pgvector failed (%v); the compiler output is in %s", err, in.logPath)
	}
	return nil
}

// ---------- Mailpit, Caddy ----------

func (in *installer) checkTool(name, arg, want string) func() (bool, string) {
	return func() (bool, string) {
		exe := filepath.Join(in.toolBin(), name+".exe")
		if !fileExists(exe) {
			return false, ""
		}
		out, _ := output(exe, arg)
		return strings.Contains(out, want), firstLine(out)
	}
}

func (in *installer) installTool(a ghAsset, exeName string) func() error {
	return func() error {
		url, sha, err := resolveAsset(a)
		if err != nil {
			return err
		}
		zipPath := filepath.Join(in.downloads(), a.Name)
		if err := download(url, zipPath, sha, in.out); err != nil {
			return err
		}
		if err := os.MkdirAll(in.toolBin(), 0o755); err != nil {
			return err
		}
		if err := extractEntry(zipPath, exeName, filepath.Join(in.toolBin(), exeName)); err != nil {
			return fmt.Errorf("%w (is it still running? run `nq down` first)", err)
		}
		return nil
	}
}

// ---------- Poppler ----------

func (in *installer) checkPoppler() (bool, string) {
	exe := filepath.Join(in.popplerBin(), "pdftotext.exe")
	if !fileExists(exe) {
		return false, ""
	}
	out, _ := output(exe, "-v")
	return strings.Contains(out, "pdftotext version"), firstLine(out)
}

func (in *installer) installPoppler() error {
	url, sha, err := resolveAsset(popplerAsset)
	if err != nil {
		return err
	}
	zipPath := filepath.Join(in.downloads(), popplerAsset.Name)
	if err := download(url, zipPath, sha, in.out); err != nil {
		return err
	}
	entry, err := findInZip(zipPath, "Library/bin/pdftotext.exe")
	if err != nil {
		return err
	}
	prefix := strings.TrimSuffix(entry, "Library/bin/pdftotext.exe")
	dest := filepath.Join(in.home, "poppler")
	os.RemoveAll(dest)
	_, err = unzip(zipPath, dest, prefix, nil)
	return err
}

// ---------- Tesseract OCR ----------

// tesseractExes: where the Tesseract installer puts tesseract.exe (all users, or the current user only).
// NovaQuest looks in the same places.
func tesseractExes() []string {
	dirs := []string{os.Getenv("ProgramFiles"), `C:\Program Files`, `C:\Program Files (x86)`}
	if local := os.Getenv("LOCALAPPDATA"); local != "" {
		dirs = append(dirs, filepath.Join(local, "Programs"))
	}
	var out []string
	for _, d := range dirs {
		if d != "" {
			out = append(out, filepath.Join(d, "Tesseract-OCR", "tesseract.exe"))
		}
	}
	return out
}

func checkTesseract() (bool, string) {
	for _, exe := range tesseractExes() {
		if fileExists(exe) {
			out, _ := output(exe, "--version")
			if strings.Contains(strings.ToLower(out), "tesseract") {
				return true, firstLine(out)
			}
		}
	}
	return false, ""
}

func (in *installer) installTesseract() error {
	url, sha, err := resolveAsset(tesseractAsset)
	if err != nil {
		return err
	}
	exe := filepath.Join(in.downloads(), tesseractAsset.Name)
	if err := download(url, exe, sha, in.out); err != nil {
		return err
	}
	fmt.Fprintln(in.out, "    Windows will ask for permission to install it.")
	code, err := runElevated(exe, "/S") // silent install to Program Files\Tesseract-OCR, English included
	if err != nil {
		return err
	}
	if code != 0 {
		return fmt.Errorf("the Tesseract installer stopped with code %d", code)
	}
	return nil
}

// checkTessEnglish: NovaQuest's own copy of Tesseract's English model is in place and is the tested file.
func (in *installer) checkTessEnglish() (bool, string) {
	got, err := fileHash(filepath.Join(in.tessdata(), "eng.traineddata"), sha256.New())
	return err == nil && got == tessEngSHA256, ""
}

func (in *installer) installTessEnglish() error {
	if err := os.MkdirAll(in.tessdata(), 0o755); err != nil {
		return err
	}
	return download(tessEngURL, filepath.Join(in.tessdata(), "eng.traineddata"), tessEngSHA256, in.out)
}

// ---------- NovaQuest app ----------

func findAppDir(flagDir string) string {
	var candidates []string
	if flagDir != "" {
		candidates = []string{flagDir}
	} else {
		if exe, err := os.Executable(); err == nil {
			candidates = append(candidates, filepath.Dir(exe))
		}
		if wd, err := os.Getwd(); err == nil {
			candidates = append(candidates, wd)
		}
	}
	for _, d := range candidates {
		if abs, err := filepath.Abs(d); err == nil && fileExists(filepath.Join(abs, "tools", "nq.py")) {
			return abs
		}
	}
	return ""
}

func (in *installer) checkPackages() (bool, string) {
	py := in.venvPython()
	if !fileExists(py) {
		return false, ""
	}
	// Every pinned requirement must be installed at its exact version.
	err := run(io.Discard, in.appDir, py, "-c", requirementsCheck, "requirements.txt")
	return err == nil, ""
}

const requirementsCheck = `import sys, importlib.metadata as m
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.split("#")[0].strip()
    if "==" in line:
        name, version = line.split("==")
        if m.version(name.split("[")[0]) != version.strip():
            sys.exit(1)`

func (in *installer) installPackages() error {
	if in.python == "" {
		if path, ok := findPython(); ok {
			in.python = path
		} else {
			return fmt.Errorf("Python is not installed (see the Python step above)")
		}
	}
	if !fileExists(in.venvPython()) {
		fmt.Fprintln(in.out, "    creating .venv…")
		if err := run(in.log, in.appDir, in.python, "-m", "venv", ".venv"); err != nil {
			return fmt.Errorf("creating the virtual environment failed: %v", err)
		}
	}
	fmt.Fprintln(in.out, "    installing packages from requirements.txt…")
	if err := run(in.log, in.appDir, in.venvPython(), "-m", "pip", "install", "--disable-pip-version-check",
		"--no-input", "-r", "requirements.txt"); err != nil {
		return fmt.Errorf("pip install failed: %v", err)
	}
	return nil
}

func (in *installer) settings() map[string]string {
	return map[string]string{
		"NQ_PG_BIN":     in.pgBin(),
		"NQ_PG_DATA":    filepath.Join(in.home, "pgdata"),
		"NQ_BIN_DIR":    in.toolBin(),
		"NQ_EXTRA_PATH": in.popplerBin(),
		"NQ_TESSDATA":   in.tessdata(),
	}
}

func (in *installer) checkSettings() (bool, string) {
	b, err := os.ReadFile(filepath.Join(in.appDir, ".env"))
	if err != nil || !fileExists(filepath.Join(in.appDir, "nq.cmd")) {
		return false, ""
	}
	text := string(b)
	for k, v := range in.settings() {
		if !strings.Contains(text, "\n"+k+"="+v+"\n") && !strings.HasPrefix(text, k+"="+v+"\n") {
			return false, ""
		}
	}
	return !strings.Contains(text, secretPlaceholder), ""
}

func (in *installer) writeSettings() error {
	if err := writeEnvFile(filepath.Join(in.appDir, ".env"), filepath.Join(in.appDir, ".env.example"), in.settings()); err != nil {
		return err
	}
	cmd := "@echo off\r\nrem NovaQuest launcher: nq init | seed | up | status | down | set-key | embed\r\n" +
		"\"%~dp0.venv\\Scripts\\python.exe\" -m tools.nq %*\r\n"
	return os.WriteFile(filepath.Join(in.appDir, "nq.cmd"), []byte(cmd), 0o755)
}

func (in *installer) printNextSteps() {
	fmt.Fprintln(in.out, "All prerequisites are installed.")
	if in.appDir == "" {
		fmt.Fprintln(in.out, "\nNext: unzip NovaQuest, put this installer in that folder and run it again to set up the app.")
		return
	}
	fmt.Fprintf(in.out, "\nNext, in a new Command Prompt:\n  cd /d \"%s\"\n", in.appDir)
	n := 1
	if pdfs, _ := filepath.Glob(filepath.Join(in.appDir, "data", "source", "*.pdf")); len(pdfs) == 0 {
		fmt.Fprintf(in.out, "  %d. Copy the NCERT Class 8 Science PDF (whole book, or one PDF per chapter) into data\\source\n", n)
		n++
	}
	fmt.Fprintf(in.out, "  %d. nq set-key     (optional: paste your Claude API key; without it NovaQuest runs offline)\n", n)
	fmt.Fprintf(in.out, "  %d. nq init        (creates the database and loads the syllabus)\n", n+1)
	fmt.Fprintf(in.out, "  %d. nq seed        (optional demo school)\n", n+2)
	fmt.Fprintf(in.out, "  %d. nq up          then open https://localhost:8443\n", n+3)
	if !in.withPgvector && !fileExists(in.vectorControl()) {
		fmt.Fprintln(in.out, "\npgvector was not installed: NovaQuest uses full-text search. Embedding search is optional;\n"+
			"to add it later, run this installer with -with-pgvector, then `nq init` again.")
	}
}

func fileExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}
