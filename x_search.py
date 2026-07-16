"""X.com (Twitter) video search via gallery-dl.

Requires a browser logged into X.com (Chrome recommended). Uses gallery-dl's
Twitter extractor to search with keyword + date filters (since/until).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

X_SEARCH_PRODUCT = "Latest"  # chronological — best for news footage


def _comet_profile_path() -> str | None:
	p = os.path.expanduser("~/Library/Application Support/Comet/Default")
	return p if os.path.isdir(p) else None


def gallery_dl_cookie_specs(cookies_browser: str) -> list[str]:
	"""Return gallery-dl --cookies-from-browser values to try, in order."""
	b = (cookies_browser or "chrome").strip().lower()
	specs: list[str] = []
	if b == "comet":
		prof = _comet_profile_path()
		if prof:
			specs.append(f"chromium:{prof}")
		specs.append("chrome")
	elif b in {"chrome", "chromium", "edge", "firefox", "opera", "safari", "brave", "vivaldi"}:
		specs.append(b)
	else:
		specs.append("chrome")
	# Always keep Chrome as a fallback for X auth.
	if "chrome" not in specs:
		specs.append("chrome")
	# Deduplicate while preserving order.
	seen = set()
	out: list[str] = []
	for s in specs:
		if s not in seen:
			seen.add(s)
			out.append(s)
	return out


def yt_dlp_cookie_arg(cookies_browser: str) -> list[str]:
	"""Return yt-dlp cookie CLI args for the selected browser."""
	b = (cookies_browser or "chrome").strip().lower()
	if b == "comet":
		prof = _comet_profile_path()
		if prof:
			return ["--cookies-from-browser", f"chromium:{prof}"]
		return ["--cookies-from-browser", "chrome"]
	if b in {"chrome", "chromium", "edge", "firefox", "opera", "safari", "brave", "vivaldi"}:
		return ["--cookies-from-browser", b]
	return ["--cookies-from-browser", "chrome"]


def _gallery_dl_bin() -> str:
	# Homebrew paths first — critical when running as a frozen .app bundle.
	for p in ("/opt/homebrew/bin/gallery-dl", "/usr/local/bin/gallery-dl"):
		if os.path.isfile(p) and os.access(p, os.X_OK):
			return p
	found = shutil.which("gallery-dl")
	if found:
		return found
	venv = Path(__file__).resolve().parent / ".venv" / "bin" / "gallery-dl"
	if venv.is_file():
		return str(venv)
	return "gallery-dl"


def _ytdlp_bin() -> str:
	for p in ("/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp"):
		if os.path.isfile(p) and os.access(p, os.X_OK):
			return p
	return shutil.which("yt-dlp") or "yt-dlp"


def extract_event_date_range(
	paragraph: str,
	context_year: int | None = None,
) -> tuple[str | None, str | None]:
	"""Extract (since, until) as YYYY-MM-DD for X search operators.

	Priority:
	1. Exact date DD/MM/YYYY in paragraph -> ±2 days window
	2. DD/MM without year + context_year -> that month ±3 days
	3. Month name + year in English -> full month
	4. context_year only -> full year
	"""
	text = paragraph or ""

	m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
	if m:
		day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
		try:
			center = datetime.date(year, month, day)
			since = center - datetime.timedelta(days=2)
			# X `until` is exclusive.
			until = center + datetime.timedelta(days=3)
			return since.isoformat(), until.isoformat()
		except ValueError:
			pass

	if context_year:
		m2 = re.search(r"\b(\d{1,2})/(\d{1,2})\b", text)
		if m2:
			day, month = int(m2.group(1)), int(m2.group(2))
			try:
				center = datetime.date(context_year, month, day)
				since = center - datetime.timedelta(days=3)
				until = center + datetime.timedelta(days=4)
				return since.isoformat(), until.isoformat()
			except ValueError:
				pass

	months = {
		"january": 1, "february": 2, "march": 3, "april": 4,
		"may": 5, "june": 6, "july": 7, "august": 8,
		"september": 9, "october": 10, "november": 11, "december": 12,
	}
	tl = text.lower()
	for name, num in months.items():
		if re.search(rf"\b{name}\b", tl):
			year = context_year
			ym = re.search(rf"\b{name}\s+(\d{{4}})\b", tl)
			if ym:
				year = int(ym.group(1))
			if year:
				since = f"{year:04d}-{num:02d}-01"
				until = f"{year + 1:04d}-01-01" if num == 12 else f"{year:04d}-{num + 1:02d}-01"
				return since, until
			break

	# Do NOT apply a full-year filter from context alone — it hides older but
	# relevant footage (e.g. 2024 World Cup clips). Year filtering is handled
	# downstream via in_year_window() when merging candidates.
	return None, None


def build_x_search_query(
	base_query: str,
	since: str | None = None,
	until: str | None = None,
	*,
	media_filter: str = "videos",
) -> str:
	"""Build an X.com search query with video + date filters."""
	q = (base_query or "").strip()
	if not q:
		return ""
	parts = [q]
	fl = (media_filter or "videos").strip().lower()
	if fl and f"filter:{fl}" not in q.lower() and "filter:videos" not in q.lower() and "filter:media" not in q.lower():
		parts.append(f"filter:{fl}")
	if since and f"since:{since}" not in q.lower():
		parts.append(f"since:{since}")
	if until and f"until:{until}" not in q.lower():
		parts.append(f"until:{until}")
	return " ".join(parts)


def x_search_url(query: str) -> str:
	import urllib.parse
	q = build_x_search_query(query)
	return "https://x.com/search?q=" + urllib.parse.quote(q) + "&src=typed_query&f=live"


def _status_url(tweet_id: int, screen_name: str | None = None) -> str:
	"""Build a canonical tweet URL. Prefer /i/status/ to avoid bad screen names."""
	return f"https://x.com/i/status/{int(tweet_id)}"


def tweet_date_to_yyyymmdd(date_str: str) -> str:
	"""Convert gallery-dl date '2026-07-06 21:40:13' -> '20260706'."""
	s = (date_str or "").strip()
	if not s:
		return ""
	m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
	if m:
		return f"{m.group(1)}{m.group(2)}{m.group(3)}"
	return ""


def simplify_x_query(query: str, max_words: int = 5) -> str:
	"""Strip filter operators and keep the most important keywords."""
	q = (query or "").strip()
	for token in ("filter:videos", "filter:media", "filter:links"):
		q = re.sub(re.escape(token), "", q, flags=re.I)
	q = re.sub(r"\bsince:\S+", "", q, flags=re.I)
	q = re.sub(r"\buntil:\S+", "", q, flags=re.I)
	words = [w for w in re.split(r"\s+", q.strip()) if w]
	return " ".join(words[:max_words])


def _parse_gallery_dl_records(records: list) -> list[dict]:
	"""Parse gallery-dl -j output into tweet video candidates."""
	tweets: dict[int, dict] = {}
	out: list[dict] = []

	for rec in records:
		if not isinstance(rec, list) or len(rec) < 2:
			continue
		level = rec[0]
		if level == -1 and isinstance(rec[1], dict) and rec[1].get("error"):
			raise RuntimeError(str(rec[1].get("message") or rec[1].get("error")))

		if level == 2 and isinstance(rec[1], dict):
			meta = rec[1]
			tid = meta.get("tweet_id") or meta.get("conversation_id")
			if not tid:
				continue
			author = meta.get("author") or meta.get("user") or {}
			screen = str(author.get("nick") or author.get("name") or "").strip()
			tweets[int(tid)] = {
				"tweet_id": int(tid),
				"screen_name": screen,
				"content": str(meta.get("content") or "").strip(),
				"date": str(meta.get("date") or ""),
				"upload_date": tweet_date_to_yyyymmdd(str(meta.get("date") or "")),
			}
			continue

		if level == 3 and len(rec) >= 3 and isinstance(rec[2], dict):
			url = str(rec[1] or "")
			meta = rec[2]
			if "video.twimg.com" not in url and "/vid/" not in url:
				continue
			tid = meta.get("tweet_id") or meta.get("conversation_id")
			if not tid:
				continue
			tid = int(tid)
			tweet = tweets.get(tid, {})
			author = (meta.get("author") or meta.get("user") or {})
			screen = str(author.get("nick") or author.get("name") or tweet.get("screen_name") or "").strip()
			content = str(meta.get("content") or tweet.get("content") or "").strip()
			title = content[:200] if content else f"X video {tid}"
			upload_date = tweet_date_to_yyyymmdd(str(meta.get("date") or tweet.get("date") or ""))
			status_url = _status_url(tid, screen)
			out.append({
				"webpage_url": status_url,
				"title": title,
				"channel": screen,
				"upload_date": upload_date,
				"duration": -1,
				"tweet_id": tid,
				"video_url": url,
			})

	# Tweets with metadata but no separate level-3 video entry.
	for tid, tweet in tweets.items():
		screen = tweet.get("screen_name") or ""
		status_url = _status_url(tid, screen)
		if any(x.get("tweet_id") == tid for x in out):
			continue
		content = tweet.get("content") or ""
		out.append({
			"webpage_url": status_url,
			"title": (content[:200] if content else f"X video {tid}"),
			"channel": screen,
			"upload_date": tweet.get("upload_date") or "",
			"duration": -1,
			"tweet_id": tid,
		})

	return out


def _tweet_has_video(status_url: str, cookies_browser: str, timeout: int = 12) -> dict | None:
	"""Quick yt-dlp probe: return metadata dict if tweet has a video, else None."""
	bin_path = _ytdlp_bin()
	cmd = [
		bin_path,
		"--ignore-no-formats-error",
		"--no-warnings",
		"--quiet",
		"--dump-single-json",
		"--no-download",
		status_url,
	]
	cmd += yt_dlp_cookie_arg(cookies_browser)
	try:
		proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
	except Exception:
		return None
	if proc.returncode != 0:
		return None
	try:
		info = json.loads(proc.stdout or "{}")
	except json.JSONDecodeError:
		return None
	if not info:
		return None
	# Twitter videos have video_ext != none or duration > 0
	if info.get("duration") or any(
		(f.get("vcodec") or "none") != "none" for f in (info.get("formats") or []) if isinstance(f, dict)
	):
		return info
	return None


def _enrich_entries_with_ytdlp(entries: list[dict], cookies_browser: str, logf: Callable[[str], None] | None = None) -> list[dict]:
	"""Verify/enrich tweet entries; drop tweets that have no video."""
	out: list[dict] = []
	for e in entries:
		url = str(e.get("webpage_url") or "")
		if not url:
			continue
		if e.get("video_url"):
			out.append(e)
			continue
		info = _tweet_has_video(url, cookies_browser)
		if not info:
			continue
		e2 = dict(e)
		e2["title"] = str(info.get("title") or e2.get("title") or "")
		e2["channel"] = str(info.get("uploader") or e2.get("channel") or "")
		ud = str(info.get("upload_date") or "")
		if ud:
			e2["upload_date"] = ud
		dur = info.get("duration")
		if isinstance(dur, (int, float)):
			e2["duration"] = int(dur)
		out.append(e2)
	if logf and entries and not out:
		logf(f"  [X] {len(entries)} tweet(s) nhưng không có video (đã lọc bằng yt-dlp)")
	return out


def _run_gallery_dl_search(
	search_url: str,
	limit: int,
	cookies_browser: str,
	logf: Callable[[str], None] | None = None,
) -> list[dict]:
	bin_path = _gallery_dl_bin()
	last_err = None
	for spec in gallery_dl_cookie_specs(cookies_browser):
		cmd = [
			bin_path,
			"--cookies-from-browser", spec,
			"--range", f"1-{max(1, int(limit))}",
			"-j",
			search_url,
		]
		if logf:
			logf(f"  [X] gallery-dl cookies={spec}")
		try:
			proc = subprocess.run(
				cmd,
				capture_output=True,
				text=True,
				timeout=50,
			)
		except subprocess.TimeoutExpired:
			last_err = "timeout"
			if logf:
				logf(f"  [X] gallery-dl timeout (50s) — query có thể quá dài")
			continue
		except Exception as e:
			last_err = str(e)
			continue

		stdout = proc.stdout or ""
		stderr = proc.stderr or ""
		if proc.returncode != 0 and not stdout.strip():
			last_err = (stderr or stdout or "gallery-dl failed").strip()[:300]
			if logf and "AuthRequired" in last_err:
				logf(f"  [X] AuthRequired với {spec}, thử browser khác…")
			continue

		# gallery-dl logs to stderr; JSON array is in stdout.
		json_start = stdout.find("[")
		if json_start < 0:
			last_err = (stderr or stdout or "no JSON output")[:300]
			continue
		try:
			records = json.loads(stdout[json_start:])
		except json.JSONDecodeError as e:
			last_err = f"JSON parse error: {e}"
			continue

		try:
			entries = _parse_gallery_dl_records(records)
		except RuntimeError as e:
			last_err = str(e)
			if logf:
				logf(f"  [X] {last_err} (cookies={spec})")
			continue

		if entries:
			enriched = _enrich_entries_with_ytdlp(entries, cookies_browser, logf=logf)
			return enriched or entries
		if stdout.strip() in ("[]",) or records == []:
			last_err = "no video tweets in results"
		else:
			last_err = "no video tweets in results"

	if logf:
		logf(f"  [X] Search thất bại: {last_err or 'unknown'}")
		if last_err == "timeout":
			logf("  [X] X.com phản hồi chậm — thử lại hoặc giảm số query song song.")
		elif last_err and "Auth" in last_err:
			logf("  [X] Hãy đăng nhập X.com trên Chrome rồi chọn cookies Chrome trong app.")
		else:
			logf("  [X] Không có video phù hợp trên X với từ khóa này (thử query ngắn hơn).")
	return []


def run_x_search(
	query: str,
	limit: int = 8,
	cookies_browser: str = "chrome",
	since: str | None = None,
	until: str | None = None,
	logf: Callable[[str], None] | None = None,
) -> dict:
	"""Search X.com for videos matching query + optional date range.

	Returns {"entries": [...]} compatible with other search helpers in app.py.
	"""
	base = simplify_x_query(query, max_words=5)
	if not base:
		return {"entries": []}

	attempts: list[str] = []
	# 1) Short query + optional date
	attempts.append(build_x_search_query(base, since=since, until=until))
	# 2) Same without date
	if since or until:
		attempts.append(build_x_search_query(base))
	# 3) filter:media (broader — catches GIF/native video tweets)
	attempts.append(build_x_search_query(base, media_filter="media"))
	# 4) Even shorter (3 words)
	short3 = simplify_x_query(base, max_words=3)
	if short3 and short3.lower() != base.lower():
		attempts.append(build_x_search_query(short3))

	seen_q: set[str] = set()
	entries: list[dict] = []
	last_q = base
	last_url = x_search_url(build_x_search_query(base))

	for full_q in attempts:
		k = full_q.strip().lower()
		if not k or k in seen_q:
			continue
		seen_q.add(k)
		last_q = full_q
		url = x_search_url(full_q)
		last_url = url
		if logf:
			logf(f"  [X] Query: {full_q}")
		found = _run_gallery_dl_search(url, limit=limit, cookies_browser=cookies_browser, logf=logf)
		if found:
			entries = _enrich_entries_with_ytdlp(found, cookies_browser, logf=logf)
			if entries:
				return {"entries": entries, "query": full_q, "search_url": url}

	return {"entries": entries, "query": last_q, "search_url": last_url}