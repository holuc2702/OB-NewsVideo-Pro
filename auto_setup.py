"""
Auto-install CLI tools needed by OB-NewsVideo on macOS.

Flow:
  1. Fix PATH so Finder-launched .app can see Homebrew
  2. Detect missing: yt-dlp, ffmpeg, ffprobe, gallery-dl
  3. Install Homebrew (non-interactive) if missing
  4. brew install missing packages
  5. pip --user fallback for yt-dlp / gallery-dl if brew fails
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]

REQUIRED_BINS = ("yt-dlp", "ffmpeg", "ffprobe", "gallery-dl")
BREW_PACKAGES = {
	"yt-dlp": "yt-dlp",
	"ffmpeg": "ffmpeg",  # also provides ffprobe
	"ffprobe": "ffmpeg",
	"gallery-dl": "gallery-dl",
}
PIP_FALLBACK = {
	"yt-dlp": "yt-dlp",
	"gallery-dl": "gallery-dl",
}

BREW_PATHS = (
	"/opt/homebrew/bin/brew",
	"/usr/local/bin/brew",
	"/opt/homebrew/bin",
	"/usr/local/bin",
	"/opt/homebrew/sbin",
	"/usr/local/sbin",
)


def ensure_homebrew_path() -> None:
	"""Prepend common Homebrew locations so GUI apps can find brew tools."""
	current = os.environ.get("PATH", "") or ""
	parts = [p for p in current.split(":") if p]
	# Also include ~/.local/bin (pip --user scripts on some setups)
	extra = list(BREW_PATHS[2:]) + [
		str(Path.home() / ".local" / "bin"),
		str(Path.home() / "Library" / "Python" / f"{sys.version_info.major}.{sys.version_info.minor}" / "bin"),
	]
	for p in reversed(extra):
		if p and p not in parts:
			parts.insert(0, p)
	# If brew exists, use its shellenv PATH pieces
	brew = _brew_binary()
	if brew:
		try:
			out = subprocess.check_output(
				[brew, "--prefix"],
				text=True,
				timeout=10,
				stderr=subprocess.DEVNULL,
			).strip()
			if out:
				for sub in ("bin", "sbin"):
					bp = f"{out}/{sub}"
					if bp not in parts:
						parts.insert(0, bp)
		except Exception:
			pass
	os.environ["PATH"] = ":".join(parts)


def _brew_binary() -> str | None:
	for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
		if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
			return candidate
	return shutil.which("brew")


def missing_bins(bins: Iterable[str] = REQUIRED_BINS) -> list[str]:
	ensure_homebrew_path()
	return [b for b in bins if shutil.which(b) is None]


def which_map(bins: Iterable[str] = REQUIRED_BINS) -> dict[str, str | None]:
	ensure_homebrew_path()
	return {b: shutil.which(b) for b in bins}


def _run(
	cmd: list[str],
	log: LogFn,
	cancel: CancelFn | None = None,
	timeout: int | None = 3600,
	env: dict | None = None,
) -> int:
	log(f"$ {' '.join(cmd)}")
	merged = os.environ.copy()
	if env:
		merged.update(env)
	try:
		proc = subprocess.Popen(
			cmd,
			stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT,
			text=True,
			env=merged,
		)
	except FileNotFoundError as e:
		log(f"[ERR] Không tìm thấy lệnh: {e}")
		return 127
	except Exception as e:
		log(f"[ERR] Không chạy được: {e}")
		return 1

	assert proc.stdout is not None
	start = time.time()
	while True:
		if cancel and cancel():
			try:
				proc.kill()
			except Exception:
				pass
			log("[INFO] Đã hủy cài đặt.")
			return 130
		line = proc.stdout.readline()
		if line:
			log(line.rstrip())
		elif proc.poll() is not None:
			break
		if timeout and (time.time() - start) > timeout:
			try:
				proc.kill()
			except Exception:
				pass
			log(f"[ERR] Timeout sau {timeout}s")
			return 124
		if not line:
			time.sleep(0.05)
	# drain remaining
	rest = proc.stdout.read() or ""
	for ln in rest.splitlines():
		if ln.strip():
			log(ln.rstrip())
	return int(proc.returncode or 0)


def _install_homebrew(log: LogFn, cancel: CancelFn | None = None) -> bool:
	if platform.system() != "Darwin":
		log("[ERR] Auto-setup chỉ hỗ trợ macOS.")
		return False
	if _brew_binary():
		return True
	log("[SETUP] Chưa có Homebrew — đang cài (lần đầu có thể hỏi mật khẩu macOS)...")
	# Official non-interactive installer
	install_url = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
	script = f'/bin/bash -c "$(curl -fsSL {install_url})"'
	# Write a small wrapper so we can stream logs more reliably
	with tempfile.NamedTemporaryFile("w", suffix="_brew_install.sh", delete=False) as f:
		f.write("#!/bin/bash\nset -e\n")
		f.write(script + "\n")
		path = f.name
	os.chmod(path, 0o755)
	env = {
		"NONINTERACTIVE": "1",
		"CI": "1",
	}
	code = _run(["/bin/bash", path], log, cancel=cancel, timeout=1800, env=env)
	try:
		os.unlink(path)
	except Exception:
		pass
	ensure_homebrew_path()
	ok = _brew_binary() is not None and code == 0
	if not ok:
		# Sometimes installer returns non-zero but brew is usable
		ok = _brew_binary() is not None
	if ok:
		log("[OK] Homebrew đã sẵn sàng.")
	else:
		log("[ERR] Cài Homebrew thất bại. Cài tay: https://brew.sh")
	return ok


def _brew_install(packages: list[str], log: LogFn, cancel: CancelFn | None = None) -> bool:
	brew = _brew_binary()
	if not brew:
		return False
	# unique preserve order
	seen = set()
	pkgs = []
	for p in packages:
		if p not in seen:
			seen.add(p)
			pkgs.append(p)
	if not pkgs:
		return True
	log(f"[SETUP] brew install {' '.join(pkgs)}")
	# update is slow; skip full update, just install
	code = _run([brew, "install", *pkgs], log, cancel=cancel, timeout=2400)
	ensure_homebrew_path()
	return code == 0


def _pip_install(pkg: str, log: LogFn, cancel: CancelFn | None = None) -> bool:
	log(f"[SETUP] Fallback: pip install --user {pkg}")
	cmd = [sys.executable, "-m", "pip", "install", "--user", "-U", pkg]
	code = _run(cmd, log, cancel=cancel, timeout=600)
	ensure_homebrew_path()
	return code == 0


def ensure_dependencies(
	log: LogFn | None = None,
	cancel: CancelFn | None = None,
	bins: Iterable[str] = REQUIRED_BINS,
) -> tuple[bool, list[str]]:
	"""
	Ensure required CLI tools exist. Returns (ok, still_missing).
	Safe to call multiple times.
	"""
	def _log(msg: str):
		if log:
			log(msg)

	if platform.system() != "Darwin":
		_log("[WARN] Auto-setup tối ưu cho macOS; trên hệ khác chỉ kiểm tra PATH.")
		miss = missing_bins(bins)
		return (len(miss) == 0, miss)

	ensure_homebrew_path()
	miss = missing_bins(bins)
	if not miss:
		_log("[OK] Đủ công cụ: " + ", ".join(REQUIRED_BINS))
		return True, []

	_log("[SETUP] Thiếu: " + ", ".join(miss))

	if cancel and cancel():
		return False, miss

	# Need brew for ffmpeg (and preferred for everything)
	need_brew_pkgs: list[str] = []
	for b in miss:
		pkg = BREW_PACKAGES.get(b)
		if pkg:
			need_brew_pkgs.append(pkg)

	if need_brew_pkgs or any(b in ("ffmpeg", "ffprobe") for b in miss):
		if not _brew_binary():
			if not _install_homebrew(_log, cancel):
				# still try pip for non-ffmpeg tools
				pass
		if _brew_binary() and need_brew_pkgs:
			_brew_install(need_brew_pkgs, _log, cancel)

	ensure_homebrew_path()
	miss = missing_bins(bins)

	# pip fallback for pure-python CLIs
	for b in list(miss):
		if cancel and cancel():
			break
		pip_name = PIP_FALLBACK.get(b)
		if not pip_name:
			continue
		if _pip_install(pip_name, _log, cancel):
			ensure_homebrew_path()
			# pip may install as gallery-dl or gallery_dl entry point
			miss = missing_bins(bins)

	miss = missing_bins(bins)
	if not miss:
		_log("[OK] Cài xong tất cả công cụ.")
		return True, []
	_log("[ERR] Vẫn thiếu: " + ", ".join(miss))
	_log("Cài tay gợi ý: brew install yt-dlp ffmpeg gallery-dl")
	return False, miss


def ensure_dependencies_async(
	on_log: LogFn,
	on_done: Callable[[bool, list[str]], None],
	cancel_event: threading.Event | None = None,
) -> threading.Thread:
	def worker():
		def cancel():
			return bool(cancel_event and cancel_event.is_set())

		ok, miss = ensure_dependencies(log=on_log, cancel=cancel)
		try:
			on_done(ok, miss)
		except Exception:
			pass

	t = threading.Thread(target=worker, daemon=True)
	t.start()
	return t
