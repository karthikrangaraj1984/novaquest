package main

// Pinned versions of everything the installer sets up. Helper tools match the versions NovaQuest was
// tested with (see TEST_RESULTS.md); change them here only together with a full test run.

const installerVersion = "1.0.4"

// Python: per-user install from python.org. Any Python 3.11+ already on the machine is used instead.
const (
	pythonVersion = "3.13.16"
	pythonURL     = "https://www.python.org/ftp/python/" + pythonVersion + "/python-" + pythonVersion + "-amd64.exe"
	pythonSigner  = "Python Software Foundation"
)

// PostgreSQL 16: EDB's portable Windows binaries (no Windows service, no admin rights). NovaQuest runs its
// own cluster from them on 127.0.0.1:54329, managed by `nq up` / `nq down`.
const pgVersion = "16.15"

var pgURLs = []string{
	"https://get.enterprisedb.com/postgresql/postgresql-" + pgVersion + "-1-windows-x64-binaries.zip",
	"https://sbp.enterprisedb.com/getfile.jsp?fileid=1260623",
}

// Parts of the EDB archive NovaQuest never uses (skipped to save ~600 MB of disk).
var pgSkipPrefixes = []string{"pgsql/pgAdmin 4/", "pgsql/symbols/", "pgsql/StackBuilder/", "pgsql/doc/"}

// Microsoft Visual C++ runtime, needed by the PostgreSQL binaries.
const (
	vcRedistURL     = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
	vsBuildToolsURL = "https://aka.ms/vs/17/release/vs_BuildTools.exe"
	microsoftSigner = "Microsoft Corporation"
)

// pgvector (optional, --with-pgvector): built from the 0.8.0 source bundled in this installer
// (git tag v0.8.0, commit 2627c5ff775ae6d7aef0c430121ccf857842d2f2), the same version as the tests.
const pgvectorVersion = "0.8.0"

// GitHub release assets, verified against the sha256 digest GitHub publishes for each asset.
type ghAsset struct {
	Label, Repo, Tag, Name string
}

var (
	mailpitAsset = ghAsset{"Mailpit " + "1.27.10", "axllent/mailpit", "v1.27.10", "mailpit-windows-amd64.zip"}
	caddyAsset   = ghAsset{"Caddy " + "2.10.2", "caddyserver/caddy", "v2.10.2", "caddy_2.10.2_windows_amd64.zip"}
	popplerAsset = ghAsset{"Poppler " + "26.09.0", "oschwartz10612/poppler-windows", "v26.09.0-0", "Release-26.09.0-0.zip"}
	// Tesseract OCR reads the words on scanned textbook pages (the official Windows build, English included).
	tesseractAsset = ghAsset{"Tesseract OCR " + "5.5.3", "tesseract-ocr/tesseract", "5.5.3", "tesseract-ocr-w64-setup-5.5.3.20260724.exe"}
)

// English for Tesseract: the official Windows installer, run silently, installs no language at all, so Tesseract
// can't read a page. NovaQuest keeps its own copy of the English model, the same file it was tested with
// (tessdata_fast 4.1.0, commit 65727574dfcd264acbb0c3e07860e4e9e9b22185), checked against its sha256.
const (
	tessEngURL    = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/65727574dfcd264acbb0c3e07860e4e9e9b22185/eng.traineddata"
	tessEngSHA256 = "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"
)
