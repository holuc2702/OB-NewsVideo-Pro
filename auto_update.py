"""
Auto-update OB-NewsVideo from GitHub Releases.

How to publish updates (repo: holuc2702/OB-NewsVideo-Pro):
  1. Bump APP_VERSION in app.py (e.g. 1.2.0)
  2. Build: pyinstaller OB-NewsVideo.spec  (or your build script)
  3. Zip the app:
       cd dist && ditto -c -k --sequesterRsrc --keepParent OB-NewsVideo.app OB-NewsVideo-macOS.zip
  4. Create a GitHub Release with tag v1.2.0 (or 1.2.0)
  5. Upload OB-NewsVideo-macOS.zip as a release asset
     (name should contain .zip and preferably 'mac' or 'OB-NewsVideo' / '.app')

Private repo: set github_token in ~/.newsfootage_hunter/settings.json
  or env GITHUB_TOKEN / GH_TOKEN.

Update flow:
  check_for_update() -> prompt user -> download zip -> write helper script ->
  quit app -> script replaces .app -> open new app.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

# --- Configure your GitHub repo here ---
# Account that owns the Releases (must match the token / gh login used to publish)
GITHUB_OWNER = "holuc2702"
GITHUB_REPO = "OB-NewsVideo-Pro"
# Must match the version you put on the Release tag (with or without leading v)
APP_VERSION = "1.1.0"

# Frozen bundle names (must match OB-NewsVideo.spec BUNDLE)
APP_BUNDLE_NAME = "OB-NewsVideo Pro.app"
APP_EXE_NAME = "OBNewsVideoPro"

DEFAULT_ASSET_HINTS = (
	"OB-NewsVideo Pro",
	"OB-NewsVideo-Pro",
	"OBNewsVideoPro",
	"OB-NewsVideo",
	"macOS",
	".app.zip",
	"macos",
	"darwin",
	"mac",
)


def normalize_version(v: str) -> str:
	v = (v or "").strip()
	if v.lower().startswith("v"):
		v = v[1:]
	# strip pre-release noise for simple compare: 1.2.0-beta -> 1.2.0
	v = v.split("+")[0].split("-")[0]
	return v


def version_tuple(v: str) -> tuple[int, ...]:
	parts = []
	for p in normalize_version(v).split("."):
		try:
			parts.append(int(re.sub(r"\D", "", p) or "0"))
		except Exception:
			parts.append(0)
	while len(parts) < 3:
		parts.append(0)
	return tuple(parts[:4])


def is_newer(remote: str, local: str) -> bool:
	return version_tuple(remote) > version_tuple(local)


def app_bundle_path() -> Path | None:
	"""Return path to .app when running as frozen PyInstaller bundle."""
	if not getattr(sys, "frozen", False):
		return None
	# .../OB-NewsVideo.app/Contents/MacOS/OB-NewsVideo
	exe = Path(sys.executable).resolve()
	for p in [exe] + list(exe.parents):
		if p.suffix == ".app" and p.is_dir():
			return p
	# parents: MacOS -> Contents -> App.app
	try:
		cand = exe.parents[2]
		if cand.suffix == ".app":
			return cand
	except Exception:
		pass
	return None


def running_from_app() -> bool:
	return app_bundle_path() is not None


def _ssl_context(unverified: bool = False):
	if unverified:
		return ssl._create_unverified_context()
	try:
		import certifi  # type: ignore

		return ssl.create_default_context(cafile=certifi.where())
	except Exception:
		return ssl.create_default_context()


def _http_json(url: str, token: str | None = None, timeout: int = 30) -> dict | list:
	headers = {
		"Accept": "application/vnd.github+json",
		"User-Agent": f"OB-NewsVideo/{APP_VERSION}",
		"X-GitHub-Api-Version": "2022-11-28",
	}
	if token:
		headers["Authorization"] = f"Bearer {token}"
	req = urllib.request.Request(url, headers=headers)
	last_err = None
	for unverified in (False, True):
		try:
			with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(unverified)) as resp:
				return json.loads(resp.read().decode("utf-8"))
		except Exception as e:
			last_err = e
	raise RuntimeError(str(last_err) if last_err else "HTTP failed")


def _http_download(url: str, dest: Path, token: str | None, log: LogFn, cancel: Callable[[], bool] | None = None) -> None:
	headers = {
		"Accept": "application/octet-stream",
		"User-Agent": f"OB-NewsVideo/{APP_VERSION}",
	}
	if token:
		headers["Authorization"] = f"Bearer {token}"
	req = urllib.request.Request(url, headers=headers)
	dest.parent.mkdir(parents=True, exist_ok=True)
	ctx = None
	for unverified in (False, True):
		try:
			ctx = _ssl_context(unverified)
			with urllib.request.urlopen(req, timeout=120, context=ctx) as resp, open(dest, "wb") as out:
				total = int(resp.headers.get("Content-Length") or 0)
				read = 0
				chunk = 1024 * 256
				last_pct = -1
				while True:
					if cancel and cancel():
						raise RuntimeError("Đã hủy tải update")
					buf = resp.read(chunk)
					if not buf:
						break
					out.write(buf)
					read += len(buf)
					if total > 0:
						pct = int(read * 100 / total)
						if pct != last_pct and pct % 5 == 0:
							log(f"[UPDATE] Đã tải {pct}% ({read // (1024*1024)} MB)")
							last_pct = pct
			return
		except RuntimeError:
			raise
		except Exception as e:
			if unverified:
				raise RuntimeError(f"Tải update thất bại: {e}") from e
			continue


def resolve_github_token(explicit: str | None = None) -> str | None:
	for t in (
		explicit,
		os.environ.get("GITHUB_TOKEN"),
		os.environ.get("GH_TOKEN"),
		os.environ.get("OB_GITHUB_TOKEN"),
	):
		if t and str(t).strip():
			return str(t).strip()
	return None


def pick_asset(assets: list[dict]) -> dict | None:
	if not assets:
		return None
	# Prefer zip containing app name
	scored: list[tuple[int, dict]] = []
	for a in assets:
		name = (a.get("name") or "").lower()
		url = a.get("browser_download_url") or a.get("url")
		if not url:
			continue
		score = 0
		if name.endswith(".zip"):
			score += 50
		if name.endswith(".dmg"):
			score += 30
		for hint in DEFAULT_ASSET_HINTS:
			if hint.lower() in name:
				score += 10
		if "win" in name or "windows" in name or "linux" in name:
			score -= 40
		scored.append((score, a))
	if not scored:
		return None
	scored.sort(key=lambda x: x[0], reverse=True)
	if scored[0][0] <= 0:
		# still allow first zip
		for s, a in scored:
			if (a.get("name") or "").lower().endswith(".zip"):
				return a
		return scored[0][1]
	return scored[0][1]


def check_for_update(token: str | None = None, timeout: int = 25) -> dict:
	"""
	Returns dict:
	  ok, update_available, local_version, remote_version, name, body, asset, error, html_url
	"""
	tok = resolve_github_token(token)
	api = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
	try:
		data = _http_json(api, token=tok, timeout=timeout)
	except Exception as e:
		msg = str(e)
		if "404" in msg or "Not Found" in msg:
			msg = (
				f"Không tìm thấy release (repo private/404 hoặc chưa publish release). "
				f"Repo: {GITHUB_OWNER}/{GITHUB_REPO}. "
				f"Nếu private, thêm github_token vào settings hoặc env GITHUB_TOKEN."
			)
		return {
			"ok": False,
			"update_available": False,
			"local_version": APP_VERSION,
			"remote_version": None,
			"error": msg,
		}

	if not isinstance(data, dict) or data.get("message") == "Not Found":
		return {
			"ok": False,
			"update_available": False,
			"local_version": APP_VERSION,
			"remote_version": None,
			"error": data.get("message") if isinstance(data, dict) else "Invalid release payload",
		}

	tag = str(data.get("tag_name") or data.get("name") or "").strip()
	remote = normalize_version(tag)
	local = normalize_version(APP_VERSION)
	asset = pick_asset(list(data.get("assets") or []))
	available = bool(remote and is_newer(remote, local) and asset)
	return {
		"ok": True,
		"update_available": available,
		"local_version": local,
		"remote_version": remote,
		"name": data.get("name") or tag,
		"body": data.get("body") or "",
		"html_url": data.get("html_url"),
		"asset": asset,
		"error": None if asset or not is_newer(remote, local) else "Release không có file .zip/.dmg phù hợp",
		"prerelease": bool(data.get("prerelease")),
	}


def _find_app_in_dir(root: Path) -> Path | None:
	# Prefer exact / known bundle names
	for name in (APP_BUNDLE_NAME, "OB-NewsVideo Pro.app", "OB-NewsVideo.app", "NewsFootage Hunter.app"):
		p = root / name
		if p.is_dir():
			return p
	apps = list(root.rglob("*.app"))
	# ignore nested frameworks
	apps = [a for a in apps if "Contents/Frameworks" not in str(a)]
	if not apps:
		return None
	# Prefer names containing NewsVideo / Pro
	def score(p: Path) -> tuple:
		n = p.name.lower()
		s = 0
		if "newsvideo" in n or "news-video" in n:
			s += 10
		if "pro" in n:
			s += 5
		return (-s, len(p.parts))
	apps.sort(key=score)
	return apps[0]


def _extract_zip(zip_path: Path, dest: Path, log: LogFn) -> Path:
	log(f"[UPDATE] Giải nén {zip_path.name}...")
	dest.mkdir(parents=True, exist_ok=True)
	with zipfile.ZipFile(zip_path, "r") as zf:
		zf.extractall(dest)
	app = _find_app_in_dir(dest)
	if not app:
		raise RuntimeError("Trong file zip không tìm thấy .app")
	return app


def _write_replace_script(
	old_app: Path,
	new_app: Path,
	pid: int,
) -> Path:
	"""Shell script waits for PID to exit, replaces app, reopens."""
	script_path = Path(tempfile.gettempdir()) / f"ob_newsvideo_update_{int(time.time())}.sh"
	# Use ditto for better macOS resource handling
	content = f"""#!/bin/bash
set -e
OLD_APP={shlex_quote(str(old_app))}
NEW_APP={shlex_quote(str(new_app))}
PID={int(pid)}
LOG={shlex_quote(str(Path.home() / ".newsfootage_hunter" / "update.log"))}
mkdir -p "$(dirname "$LOG")"
echo "$(date) start update PID=$PID" >> "$LOG"

# Wait for old process to exit (max ~2 min)
for i in $(seq 1 120); do
  if ! kill -0 "$PID" 2>/dev/null; then
    break
  fi
  sleep 0.5
done
sleep 1

if [ ! -d "$NEW_APP" ]; then
  echo "$(date) NEW_APP missing: $NEW_APP" >> "$LOG"
  exit 1
fi

# Backup old app
if [ -d "$OLD_APP" ]; then
  BAK="${{OLD_APP}}.bak.$(date +%s)"
  echo "$(date) backup -> $BAK" >> "$LOG"
  rm -rf "$BAK" 2>/dev/null || true
  mv "$OLD_APP" "$BAK" || true
fi

# Install new
PARENT="$(dirname "$OLD_APP")"
mkdir -p "$PARENT"
# Move new app into place (name must match OLD)
TARGET="$OLD_APP"
echo "$(date) install $NEW_APP -> $TARGET" >> "$LOG"
rm -rf "$TARGET" 2>/dev/null || true
# Prefer ditto
if command -v ditto >/dev/null 2>&1; then
  ditto "$NEW_APP" "$TARGET"
else
  mv "$NEW_APP" "$TARGET"
fi

# Clear quarantine so Gatekeeper is less noisy
xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null || true

echo "$(date) open $TARGET" >> "$LOG"
open "$TARGET" || "$TARGET/Contents/MacOS/OBNewsVideoPro" || "$TARGET/Contents/MacOS/OB-NewsVideo" &

# Cleanup backup after successful open (keep one)
# rm -rf "$BAK" 2>/dev/null || true

# Self-delete
rm -f -- "$0" 2>/dev/null || true
"""
	script_path.write_text(content, encoding="utf-8")
	script_path.chmod(0o755)
	return script_path


def shlex_quote(s: str) -> str:
	return "'" + s.replace("'", "'\"'\"'") + "'"


def download_and_stage_update(
	release_info: dict,
	token: str | None = None,
	log: LogFn | None = None,
	cancel: Callable[[], bool] | None = None,
) -> Path:
	"""Download asset and return path to staged .app directory."""
	def _log(m: str):
		if log:
			log(m)

	asset = release_info.get("asset") or {}
	# API asset url needs Accept: application/octet-stream + auth for private
	url = asset.get("url") or asset.get("browser_download_url")
	if not url:
		raise RuntimeError("Release không có URL asset")
	name = asset.get("name") or "update.zip"
	tok = resolve_github_token(token)
	# For public browser_download_url is fine; for private use API asset url with token
	if tok and asset.get("url"):
		url = asset["url"]

	tmpdir = Path(tempfile.mkdtemp(prefix="ob_nv_update_"))
	zip_path = tmpdir / name
	_log(f"[UPDATE] Tải {name}...")
	_http_download(url, zip_path, tok, _log, cancel)

	if name.lower().endswith(".zip") or zipfile.is_zipfile(zip_path):
		app = _extract_zip(zip_path, tmpdir / "extracted", _log)
		return app

	if name.lower().endswith(".dmg"):
		raise RuntimeError("Asset .dmg chưa được hỗ trợ auto-replace. Hãy upload file .zip chứa .app")

	raise RuntimeError(f"Không hỗ trợ asset: {name}")


def apply_update_and_relaunch(
	staged_app: Path,
	log: LogFn | None = None,
) -> None:
	"""
	Schedule replacement of current .app and exit.
	Must be running from a frozen .app bundle.
	"""
	def _log(m: str):
		if log:
			log(m)

	old = app_bundle_path()
	if not old:
		raise RuntimeError(
			"Đang chạy từ source (python app.py), không thể tự thay .app. "
			"Hãy tải bản mới thủ công hoặc build lại."
		)
	if not staged_app.exists():
		raise RuntimeError("Staged app không tồn tại")

	script = _write_replace_script(old, staged_app, os.getpid())
	_log(f"[UPDATE] Sẽ thoát app và cài bản mới...\nScript: {script}")
	# Detach helper
	subprocess.Popen(
		["/bin/bash", str(script)],
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		start_new_session=True,
	)
	# Give script a moment to start waiting
	time.sleep(0.3)


def perform_update(
	token: str | None = None,
	log: LogFn | None = None,
	cancel: Callable[[], bool] | None = None,
	release_info: dict | None = None,
) -> dict:
	"""Full download + stage. Does NOT quit — caller should confirm then apply_update_and_relaunch."""
	info = release_info or check_for_update(token=token)
	if not info.get("ok"):
		return info
	if not info.get("update_available"):
		info["staged_app"] = None
		return info
	staged = download_and_stage_update(info, token=token, log=log, cancel=cancel)
	info["staged_app"] = str(staged)
	return info


def check_for_update_async(
	on_done: Callable[[dict], None],
	token: str | None = None,
) -> threading.Thread:
	def worker():
		try:
			info = check_for_update(token=token)
		except Exception as e:
			info = {
				"ok": False,
				"update_available": False,
				"local_version": APP_VERSION,
				"error": str(e),
			}
		try:
			on_done(info)
		except Exception:
			pass

	t = threading.Thread(target=worker, daemon=True)
	t.start()
	return t
