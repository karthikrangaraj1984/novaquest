package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

var httpClient = &http.Client{Timeout: 30 * time.Minute}

// githubAPI is a variable so tests can point it at a local server.
var githubAPI = "https://api.github.com"

type ghRelease struct {
	Assets []struct {
		Name   string `json:"name"`
		URL    string `json:"browser_download_url"`
		Digest string `json:"digest"`
	} `json:"assets"`
}

// resolveAsset finds the download URL and expected sha256 of a release asset.
func resolveAsset(a ghAsset) (url, sha string, err error) {
	req, _ := http.NewRequest("GET", fmt.Sprintf("%s/repos/%s/releases/tags/%s", githubAPI, a.Repo, a.Tag), nil)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "novaquest-installer/"+installerVersion)
	resp, err := httpClient.Do(req)
	if err != nil {
		return "", "", fmt.Errorf("looking up %s on GitHub: %w", a.Label, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return "", "", fmt.Errorf("looking up %s on GitHub: HTTP %d (if this says 403, GitHub's hourly limit was hit: try again in an hour)", a.Label, resp.StatusCode)
	}
	var rel ghRelease
	if err := json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(&rel); err != nil {
		return "", "", fmt.Errorf("reading the %s release: %w", a.Label, err)
	}
	return pickAsset(rel, a)
}

func pickAsset(rel ghRelease, a ghAsset) (string, string, error) {
	for _, x := range rel.Assets {
		if x.Name != a.Name {
			continue
		}
		sha, ok := strings.CutPrefix(x.Digest, "sha256:")
		if !ok || len(sha) != 64 {
			return "", "", fmt.Errorf("GitHub publishes no sha256 digest for %s, so it can't be verified", a.Name)
		}
		return x.URL, strings.ToLower(sha), nil
	}
	return "", "", fmt.Errorf("%s not found in release %s of %s", a.Name, a.Tag, a.Repo)
}

// download fetches url into dest (via a .part file), retrying, and checks the sha256 when one is given.
// A file already in the cache that matches the expected hash is reused.
func download(url, dest, wantSHA256 string, out io.Writer) error {
	if wantSHA256 != "" {
		if got, err := fileHash(dest, sha256.New()); err == nil && got == wantSHA256 {
			fmt.Fprintf(out, "    using the copy downloaded earlier\n")
			return nil
		}
	}
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return err
	}
	var last error
	for attempt := 1; attempt <= 3; attempt++ {
		if attempt > 1 {
			fmt.Fprintf(out, "    retrying (%d of 3)…\n", attempt)
			time.Sleep(time.Duration(attempt*2) * time.Second)
		}
		if last = fetchOnce(url, dest+".part", out); last != nil {
			continue
		}
		if wantSHA256 != "" {
			got, err := fileHash(dest+".part", sha256.New())
			if err != nil {
				return err
			}
			if got != wantSHA256 {
				os.Remove(dest + ".part")
				return fmt.Errorf("download of %s is corrupt or was tampered with (sha256 %s, expected %s)", filepath.Base(dest), got, wantSHA256)
			}
		}
		return os.Rename(dest+".part", dest)
	}
	return fmt.Errorf("downloading %s: %w", url, last)
}

func fetchOnce(url, dest string, out io.Writer) error {
	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("User-Agent", "novaquest-installer/"+installerVersion)
	resp, err := httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	f, err := os.Create(dest)
	if err != nil {
		return err
	}
	pw := &progress{total: resp.ContentLength, out: out}
	_, err = io.Copy(f, io.TeeReader(resp.Body, pw))
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	pw.done()
	return err
}

type progress struct {
	total, n int64
	shown    int
	out      io.Writer
}

func (p *progress) Write(b []byte) (int, error) {
	p.n += int64(len(b))
	if p.total > 0 {
		if pct := int(p.n * 100 / p.total); pct/10 > p.shown/10 {
			p.shown = pct
			fmt.Fprintf(p.out, "\r    %3d%% of %d MB", pct, p.total>>20)
		}
	}
	return len(b), nil
}

func (p *progress) done() {
	if p.total > 0 {
		fmt.Fprintln(p.out)
	}
}

func fileHash(path string, h hash.Hash) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

var errNotZip = errors.New("not a zip archive")
