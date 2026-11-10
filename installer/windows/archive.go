package main

import (
	"archive/zip"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// unzip extracts the entries of a zip archive under dest, streaming one file at a time.
// stripPrefix is removed from every entry name (entries outside it are skipped), entries starting with any of
// skip are left out, and names that would escape dest ("../", absolute paths) are refused.
func unzip(archive, dest, stripPrefix string, skip []string) (int, error) {
	r, err := zip.OpenReader(archive)
	if err != nil {
		return 0, fmt.Errorf("%s: %w (%v)", filepath.Base(archive), errNotZip, err)
	}
	defer r.Close()
	root, err := filepath.Abs(dest)
	if err != nil {
		return 0, err
	}
	n := 0
	for _, f := range r.File {
		name := strings.ReplaceAll(f.Name, "\\", "/")
		if skipped(name, skip) {
			continue
		}
		rel, ok := strings.CutPrefix(name, stripPrefix)
		if !ok || rel == "" {
			continue
		}
		target := filepath.Join(root, filepath.FromSlash(rel))
		if target != root && !strings.HasPrefix(target, root+string(os.PathSeparator)) {
			return n, fmt.Errorf("unsafe path in archive: %q", f.Name)
		}
		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0o755); err != nil {
				return n, err
			}
			continue
		}
		if err := extractOne(f, target); err != nil {
			return n, err
		}
		n++
	}
	return n, nil
}

func skipped(name string, prefixes []string) bool {
	for _, p := range prefixes {
		if strings.HasPrefix(name, p) {
			return true
		}
	}
	return false
}

func extractOne(f *zip.File, target string) error {
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	src, err := f.Open()
	if err != nil {
		return err
	}
	defer src.Close()
	dst, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(dst, src); err != nil {
		dst.Close()
		return err
	}
	return dst.Close()
}

// extractEntry writes one named entry of a zip archive to target.
func extractEntry(archive, name, target string) error {
	r, err := zip.OpenReader(archive)
	if err != nil {
		return fmt.Errorf("%s: %w", filepath.Base(archive), errNotZip)
	}
	defer r.Close()
	for _, f := range r.File {
		if strings.ReplaceAll(f.Name, "\\", "/") == name {
			tmp := target + ".new"
			if err := extractOne(f, tmp); err != nil {
				return err
			}
			os.Remove(target)
			return os.Rename(tmp, target)
		}
	}
	return fmt.Errorf("%s has no %s", filepath.Base(archive), name)
}

// findInZip returns the first entry whose name ends with suffix (for archives with a versioned top folder).
func findInZip(archive, suffix string) (string, error) {
	r, err := zip.OpenReader(archive)
	if err != nil {
		return "", fmt.Errorf("%s: %w", filepath.Base(archive), errNotZip)
	}
	defer r.Close()
	for _, f := range r.File {
		if name := strings.ReplaceAll(f.Name, "\\", "/"); strings.HasSuffix(name, suffix) {
			return name, nil
		}
	}
	return "", fmt.Errorf("%s has no %s", filepath.Base(archive), suffix)
}
