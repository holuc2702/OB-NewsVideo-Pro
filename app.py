#!/usr/bin/env python3
# NewsFootage Hunter — v21 (pipeline per paragraph)
# Goals:
# - Each line = one paragraph (supports "ĐOẠN X: ...")
# - For each paragraph: analyze -> generate queries (Rules or Ollama) -> search -> pick -> download (2 videos) -> next paragraph
# - Limit: only videos <= 30 minutes
# - Rename videos: "ĐOẠN X: <keyword/hint> - dd/mm/yy.mp4"
# - Search & download X.com (Twitter) videos via gallery-dl (keyword + date filter)
# - Export numbered HTML with X.com search links per paragraph: 03_metadata/script_numbered.html
# - STOP and START/RESUME
# - Save settings + log to ~/.newsfootage_hunter
# - Homebrew PATH fix so .app can find yt-dlp/ffmpeg/ffprobe/gallery-dl
# - Auto-setup: install Homebrew + tools on first run / when missing
# - Auto-update: GitHub Releases (holuc2702/OB-NewsVideo-Pro)

import os
import sys
import re
import json
import csv
import time
import random
import datetime
import threading
import queue
import platform
import shutil
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import ssl
import math
import concurrent.futures
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, filedialog

import douyin_ws
import x_search
import auto_setup
import auto_update

douyin_ws.start_ws_thread()

APP_NAME = "OB-NewsVideo Pro"
APP_VERSION = auto_update.APP_VERSION
DEFAULT_RESULTS_PER_QUERY = 8
DEFAULT_DOWNLOAD_THREADS = 8
MAX_DURATION_SECONDS = 30 * 60
VIDEOS_PER_PARAGRAPH = 4
MAX_VIDEO_FILENAME_LEN = 90
DATE_SUFFIX_LEN = 8  # DD-MM-YY e.g. 01-07-26

# Prefer videos around inferred timeline (helps avoid 2019-2023 when script is 2026)
DEFAULT_YEAR_WINDOW = 2  # ± years

# Heavy mode: re-rank candidates with Ollama (slower but smarter)
DEFAULT_RERANK_CANDIDATES = 18
DEFAULT_SUBTITLE_CHAR_LIMIT = 4500
# Multilingual OSINT search (footage often lives on UK/RU channels)
DEFAULT_SEARCH_LANGS = ["en", "uk", "ru"]
DEFAULT_MAX_QUERIES = 8
DEFAULT_TAVILY_MAX = 5
DEFAULT_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "google/gemini-2.0-flash-exp:free"
DEFAULT_LLM_SEARCH_MODEL = "google/gemini-2.0-flash-exp:free:online"
# YouTube localizes results by account+region. For foreign-language footage we must
# force interface language (hl) + region (gl), and NOT use the logged-in (VN) cookies
# during search, otherwise everything comes back in Vietnamese.
DEFAULT_SEARCH_USE_COOKIES = False
# Never search in Vietnamese. Search in the language(s) of the country involved
# in the event (Ukraine -> uk, Russia -> ru, China -> zh, ...), always plus English.
LANG_REGION_MAP = {
	"en": ("en", "US"),
	"uk": ("uk", "UA"),
	"ru": ("ru", "RU"),
	"zh": ("zh-Hans", "TW"),
}

# Vietnamese-specific letters (horns, breve, đ, and the Latin Extended Additional
# block). Used to DETECT and DROP Vietnamese queries — we must never search in VN.
VIETNAMESE_CHARS = r"[\u0102\u0103\u01A0\u01A1\u01AF\u01B0\u0110\u0111\u1EA0-\u1EFF]"


def is_vietnamese(text: str) -> bool:
	return bool(re.search(VIETNAMESE_CHARS, text or ""))


def detect_query_lang(query: str) -> str:
	"""Guess the language of a search query so we can force YouTube's hl/gl.

	Returns one of: 'vi', 'zh', 'uk', 'ru', 'en'.
	"""
	q = query or ""
	if is_vietnamese(q):
		return "vi"
	# Chinese / CJK ideographs.
	if re.search(r"[\u4E00-\u9FFF]", q):
		return "zh"
	if re.search(r"[\u0400-\u04FF]", q):
		# Ukrainian-specific letters distinguish UK from RU.
		if re.search(r"[\u0456\u0457\u0454\u0491\u0406\u0407\u0404\u0490]", q):
			return "uk"
		return "ru"
	return "en"


def hl_gl_for_lang(lang: str) -> tuple[str, str]:
	# 'vi' (or anything unknown) falls back to English/US so we never pull VN results.
	return LANG_REGION_MAP.get(lang, ("en", "US"))


def drop_vietnamese_queries(queries, logf=None) -> list:
	"""Remove any query that is (or contains) Vietnamese text. We must never search
	YouTube in Vietnamese — it returns Vietnamese re-uploads, not original footage."""
	out = []
	for q in (queries or []):
		s = str(q).strip()
		if not s:
			continue
		if is_vietnamese(s):
			if logf:
				logf(f"  [LANG] bỏ query tiếng Việt: {s[:60]}")
			continue
		out.append(s)
	return out
DEFAULT_COOL_MODE = True
DEFAULT_OLLAMA_THREADS = 4
DEFAULT_OLLAMA_SLEEP_SEC = 0.25
DEFAULT_COOL_NICE = 10            # lower CPU priority for heavy subprocesses (posix)
DEFAULT_COOL_DOWNLOAD_CONC = 5    # max parallel video fragments under Cool mode

# Mutable runtime cooling config, updated from the UI when a run starts.
COOL = {
	"on": DEFAULT_COOL_MODE,
	"nice": DEFAULT_COOL_NICE,
	"sleep": DEFAULT_OLLAMA_SLEEP_SEC,
	"download_conc": DEFAULT_COOL_DOWNLOAD_CONC,
}

# Optional FREE cloud LLM. When enabled, local Ollama generation is transparently
# routed to Groq or Google Gemini so the heavy inference runs on the server and
# the laptop stays cool. Keys rotate on quota/rate-limit failures (like Tavily).
CLOUD = {"on": False, "provider": "groq", "model": "", "base_url": "", "search_model": "", "keys": [], "idx": 0}
cloud_lock = threading.Lock()

# Kết quả quét Ollama gần nhất — dùng để xoay vòng ưu tiên key còn quota.
OLLAMA_AUDIT: dict[str, Any] = {"keys": [], "results": [], "summary": None}
ollama_audit_lock = threading.Lock()

# Global semaphore to strictly limit concurrent yt-dlp queries and avoid YouTube 429 Rate Limiting
GLOBAL_YTDLP_SEM = threading.Semaphore(4)


class _LLMResp:
	"""Minimal subprocess.CompletedProcess stand-in for cloud LLM responses,
	so existing callers can keep reading .returncode and .stdout unchanged."""

	def __init__(self, returncode: int, stdout: str):
		self.returncode = returncode
		self.stdout = stdout or ""
		self.stderr = ""


def _http_post_json(url: str, payload: dict, headers: dict, timeout: int):
	"""POST JSON; return (parsed_json, error_str). Tries a normal (verified) TLS
	connection first, then retries with an unverified context, because many
	macOS Python builds ship without root certificates -> raw urllib HTTPS calls
	fail with CERTIFICATE_VERIFY_FAILED even though yt-dlp (own certs) works."""
	data = json.dumps(payload).encode("utf-8")
	last_err = None
	for attempt in ("verified", "unverified"):
		try:
			req = urllib.request.Request(url, data=data, headers=headers, method="POST")
			if attempt == "unverified":
				ctx = ssl._create_unverified_context()
				resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
			else:
				resp = urllib.request.urlopen(req, timeout=timeout)
			with resp as r:
				return json.loads(r.read().decode("utf-8")), None
		except urllib.error.HTTPError as e:
			# Server responded with an error -> not a TLS issue; capture the body.
			try:
				body = e.read().decode("utf-8", "ignore")
			except Exception:
				body = ""
			return None, f"HTTP {e.code} {body[:300]}"
		except ssl.SSLError as e:
			last_err = f"SSL {e}"
			continue  # retry with the unverified context
		except Exception as e:
			# DNS/timeout/connection errors won't be fixed by another TLS context.
			return None, f"{type(e).__name__}: {e}"
	return None, (last_err or "unknown error")


def _http_get_text(url: str, headers: dict, timeout: int) -> tuple[str | None, str | None]:
	last_err = None
	for attempt in ("verified", "unverified"):
		try:
			req = urllib.request.Request(url, headers=headers, method="GET")
			if attempt == "unverified":
				ctx = ssl._create_unverified_context()
				resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
			else:
				resp = urllib.request.urlopen(req, timeout=timeout)
			with resp as r:
				return r.read().decode("utf-8", "ignore"), None
		except urllib.error.HTTPError as e:
			try:
				body = e.read().decode("utf-8", "ignore")
			except Exception:
				body = ""
			return None, f"HTTP {e.code} {body[:300]}"
		except ssl.SSLError as e:
			last_err = f"SSL {e}"
			continue
		except Exception as e:
			return None, f"{type(e).__name__}: {e}"
	return None, (last_err or "unknown error")


def fetch_ollama_me(api_key: str) -> tuple[dict | None, str | None]:
	"""POST https://ollama.com/api/me — xác thực API key, trả email/plan."""
	headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
	return _http_post_json("https://ollama.com/api/me", {}, headers, 15)


def fetch_ollama_settings_html(session_cookie: str) -> tuple[str | None, str | None]:
	"""Lấy HTML trang settings (cần cookie đăng nhập trình duyệt, không dùng API key)."""
	cookie = (session_cookie or "").strip()
	if cookie.lower().startswith("__secure-session="):
		cookie_hdr = cookie
	else:
		cookie_hdr = f"__Secure-session={cookie}"
	headers = {
		"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
		"Cookie": cookie_hdr,
		"Accept": "text/html,application/xhtml+xml",
	}
	html, err = _http_get_text("https://ollama.com/settings", headers, 20)
	if err:
		return None, err
	if html and ("signin" in html.lower()[:2000] or len(html) < 500):
		return None, "Cookie hết hạn hoặc chưa đăng nhập — hãy lấy lại từ trình duyệt."
	return html, None


def parse_ollama_settings_usage(html: str) -> dict | None:
	"""Parse % Session / Weekly từ ollama.com/settings (giới hạn theo giờ / tuần)."""
	if not html:
		return None
	plain = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
	plain = re.sub(r"<style[^>]*>.*?</style>", " ", plain, flags=re.I | re.S)
	plain = re.sub(r"<[^>]+>", "\n", plain)
	plain = re.sub(r"[ \t]+", " ", plain)

	def _pct(label: str) -> float | None:
		m = re.search(rf"{label}\s*usage\s*(\d+(?:\.\d+)?)\s*%\s*used", plain, re.I)
		return float(m.group(1)) if m else None

	def _reset(label: str) -> str | None:
		m = re.search(rf"{label}\s*usage.*?Resets in\s*([^\n]+)", plain, re.I | re.S)
		return m.group(1).strip() if m else None

	plan_m = re.search(r"Cloud Usage\s*(free|pro|max)\b", plain, re.I)
	session_pct = _pct("Session")
	weekly_pct = _pct("Weekly")
	if session_pct is None and weekly_pct is None:
		return None
	return {
		"plan": (plan_m.group(1).lower() if plan_m else None),
		"session_percent": session_pct,
		"session_reset": _reset("Session"),
		"weekly_percent": weekly_pct,
		"weekly_reset": _reset("Weekly"),
	}


def format_ollama_usage_log(usage: dict) -> list[str]:
	lines = []
	plan = usage.get("plan") or "?"
	lines.append(f"  [USAGE] Gói cloud (trang settings): {plan}")
	if usage.get("session_percent") is not None:
		lines.append(
			f"  [USAGE] Session: {usage['session_percent']}% đã dùng"
			+ (f" — reset sau {usage['session_reset']}" if usage.get("session_reset") else "")
		)
	if usage.get("weekly_percent") is not None:
		lines.append(
			f"  [USAGE] Weekly: {usage['weekly_percent']}% đã dùng"
			+ (f" — reset sau {usage['weekly_reset']}" if usage.get("weekly_reset") else "")
		)
	return lines


def classify_ollama_probe_error(err: str) -> str:
	low = (err or "").lower()
	if any(x in low for x in ("http 401", "http 403", "unauthorized", "forbidden", "invalid api", "invalid key", "not authorized")):
		return "invalid"
	if any(x in low for x in ("http 429", "rate limit", "rate_limit", "too many", "usage limit", "usage limits", "quota", "exceeded", "limit reached", "out of")):
		return "quota"
	return "error"


def probe_ollama_key(api_key: str, probe_model: str = "gemma3:4b") -> dict[str, Any]:
	"""Kiểm tra 1 API key: /api/me + gọi chat tối thiểu (1 token) để biết còn quota không."""
	me, me_err = fetch_ollama_me(api_key)
	if me_err:
		return {
			"status": classify_ollama_probe_error(me_err),
			"detail": me_err,
			"email": None,
			"plan": None,
			"account_id": None,
		}
	email = me.get("Email") or me.get("email") or "?"
	plan = me.get("Plan") or me.get("plan") or "free"
	account_id = me.get("ID") or me.get("id")
	suspended = me.get("SuspendedAt") or {}
	if isinstance(suspended, dict) and suspended.get("Valid"):
		return {
			"status": "suspended",
			"detail": "Tài khoản bị suspend",
			"email": email,
			"plan": plan,
			"account_id": account_id,
		}

	headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
	payload = {
		"model": probe_model,
		"messages": [{"role": "user", "content": "."}],
		"stream": False,
		"options": {"num_predict": 1},
	}
	data, err = _http_post_json("https://ollama.com/api/chat", payload, headers, 45)
	if err:
		return {
			"status": classify_ollama_probe_error(err),
			"detail": err,
			"email": email,
			"plan": plan,
			"account_id": account_id,
		}
	if isinstance(data, dict) and data.get("error"):
		err_msg = str(data.get("error") or "")
		return {
			"status": classify_ollama_probe_error(err_msg),
			"detail": err_msg,
			"email": email,
			"plan": plan,
			"account_id": account_id,
		}
	return {
		"status": "ready",
		"detail": "Sẵn sàng",
		"email": email,
		"plan": plan,
		"account_id": account_id,
	}


def store_ollama_audit(keys: list[str], results: list[dict[str, Any] | None], summary: dict[str, Any]):
	"""Lưu kết quả quét để cloud_generate ưu tiên key sẵn sàng."""
	with ollama_audit_lock:
		OLLAMA_AUDIT["keys"] = list(keys)
		OLLAMA_AUDIT["results"] = list(results)
		OLLAMA_AUDIT["summary"] = summary


def ollama_audit_matches_keys(keys: list[str]) -> bool:
	with ollama_audit_lock:
		return OLLAMA_AUDIT.get("keys") == keys and bool(OLLAMA_AUDIT.get("results"))


def ollama_key_indices_priority(keys: list[str], include_quota: bool = False) -> list[int]:
	"""Thứ tự key: ready → chưa biết → (tuỳ chọn) hết quota → lỗi/khóa."""
	n = len(keys)
	if not ollama_audit_matches_keys(keys):
		return list(range(n))
	with ollama_audit_lock:
		results = OLLAMA_AUDIT.get("results") or []
	ready, unknown, quota, bad = [], [], [], []
	for i in range(n):
		r = results[i] if i < len(results) else None
		st = (r or {}).get("status")
		if st == "ready":
			ready.append(i)
		elif st == "quota":
			quota.append(i)
		elif st in ("invalid", "suspended", "error"):
			bad.append(i)
		else:
			unknown.append(i)
	order = ready + unknown
	if include_quota:
		order += quota
	return order + bad


def apply_ollama_audit_rotation(keys: list[str], logf=None) -> int | None:
	"""Đặt CLOUD['idx'] vào key ready đầu tiên (nếu có). Trả về số key ready."""
	if not ollama_audit_matches_keys(keys):
		return None
	order = ollama_key_indices_priority(keys, include_quota=False)
	ready_n = len(OLLAMA_AUDIT.get("summary", {}).get("ready") or [])
	with cloud_lock:
		if order:
			CLOUD["idx"] = order[0]
	if logf and ready_n:
		logf(f"  → Đã áp xoay vòng: bắt đầu từ key #{order[0] + 1} ({ready_n} key sẵn sàng)")
	return ready_n


def audit_ollama_keys_batch(keys: list[str], logf=None, max_workers: int = 8) -> dict[str, Any]:
	"""Quét hàng loạt API key Ollama — phù hợp xoay vòng nhiều key."""
	results: list[dict[str, Any] | None] = [None] * len(keys)

	def _one(idx: int, key: str):
		try:
			results[idx] = probe_ollama_key(key)
		except Exception as e:
			results[idx] = {
				"status": "error",
				"detail": str(e),
				"email": None,
				"plan": None,
				"account_id": None,
			}

	workers = max(1, min(max_workers, len(keys)))
	done = 0
	with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
		futs = {ex.submit(_one, i, k): i for i, k in enumerate(keys)}
		for fut in concurrent.futures.as_completed(futs):
			fut.result()
			done += 1
			if logf and (done == len(keys) or done % max(1, len(keys) // 10) == 0):
				logf(f"  … tiến độ: {done}/{len(keys)} keys")

	status_label = {
		"ready": "✅ Sẵn sàng",
		"quota": "⚠ Hết quota",
		"invalid": "❌ Key lỗi",
		"suspended": "⛔ Bị khóa",
		"error": "❌ Lỗi",
	}
	buckets: dict[str, list[int]] = {k: [] for k in status_label}
	accounts: dict[str, dict[str, Any]] = {}

	for i, r in enumerate(results):
		if not r:
			continue
		st = r.get("status") or "error"
		buckets.setdefault(st, []).append(i + 1)
		if logf:
			label = status_label.get(st, st)
			email = r.get("email") or "?"
			plan = r.get("plan") or "?"
			detail = _safe_snip(str(r.get("detail") or ""), 90)
			logf(f"  Key #{i + 1}: {label} — {email} ({plan}) — {detail}")

		acc_key = str(r.get("account_id") or r.get("email") or f"key-{i+1}")
		if acc_key not in accounts:
			accounts[acc_key] = {"email": r.get("email"), "plan": r.get("plan"), "ready": [], "quota": [], "other": []}
		if st == "ready":
			accounts[acc_key]["ready"].append(i + 1)
		elif st == "quota":
			accounts[acc_key]["quota"].append(i + 1)
		else:
			accounts[acc_key]["other"].append(i + 1)

	summary = {
		"total": len(keys),
		"ready": buckets.get("ready", []),
		"quota": buckets.get("quota", []),
		"invalid": buckets.get("invalid", []),
		"suspended": buckets.get("suspended", []),
		"error": buckets.get("error", []),
		"accounts": accounts,
	}
	if logf:
		logf(f"\n[TÓM TẮT] {len(keys)} keys — "
			f"Sẵn sàng: {len(summary['ready'])} | "
			f"Hết quota: {len(summary['quota'])} | "
			f"Lỗi/invalid: {len(summary['invalid']) + len(summary['error']) + len(summary['suspended'])}")
		if summary["ready"]:
			logf(f"  → Xoay vòng ưu tiên keys: {', '.join('#' + str(x) for x in summary['ready'][:20])}"
				+ (" …" if len(summary["ready"]) > 20 else ""))
		if len(accounts) > 1:
			logf("  → Theo tài khoản:")
			for acc in accounts.values():
				em = acc.get("email") or "?"
				pl = acc.get("plan") or "?"
				logf(f"     {em} ({pl}): ready={acc.get('ready') or '-'} quota={acc.get('quota') or '-'}")
	store_ollama_audit(keys, results, summary)
	return summary


def normalize_api_base_url(base_url: str) -> str:
	base = (base_url or "").strip().rstrip("/")
	return base or DEFAULT_OPENROUTER_BASE


def openai_chat_completions_url(base_url: str) -> str:
	base = normalize_api_base_url(base_url)
	if base.endswith("/chat/completions"):
		return base
	if base.endswith("/v1"):
		return base + "/chat/completions"
	return base + "/v1/chat/completions"


def is_openai_compatible_cloud_provider(prov: str) -> bool:
	return (prov or "").strip().lower() in {"groq", "openrouter", "custom", "ollama"}


def openai_compat_extra_headers(prov: str) -> dict[str, str]:
	p = (prov or "").strip().lower()
	if p == "openrouter":
		return {"HTTP-Referer": "https://ob-newsvideo.local", "X-Title": APP_NAME}
	return {}


def effective_llm_search_model(use_web_tool: bool = False) -> str:
	"""Model tìm web — user gõ riêng ở tab Web Search thì dùng đúng tên đó."""
	explicit = (CLOUD.get("search_model") or "").strip()
	if explicit:
		model = explicit
	else:
		model = (CLOUD.get("model") or "").strip() or DEFAULT_LLM_SEARCH_MODEL
	prov = (CLOUD.get("provider") or "").strip().lower()
	if prov == "openrouter" and use_web_tool:
		# openrouter:web_search server tool — không cần suffix :online
		if model.endswith(":online"):
			model = model[: -len(":online")]
		return model
	if prov == "openrouter":
		low = model.lower()
		if ":online" not in low and "sonar" not in low and "perplexity" not in low:
			model = model + ":online"
	return model


def extract_openrouter_url_citations(data: dict, max_results: int) -> list[dict[str, str]]:
	"""Trích URL từ annotations OpenRouter (openrouter:web_search tool)."""
	out: list[dict[str, str]] = []
	seen: set[str] = set()
	if not isinstance(data, dict):
		return out
	ch = (data.get("choices") or [{}])[0]
	msg = (ch.get("message") or {}) if isinstance(ch, dict) else {}
	for ann in msg.get("annotations") or []:
		if not isinstance(ann, dict):
			continue
		cite = ann.get("url_citation") or ann.get("urlCitation") or {}
		if not isinstance(cite, dict):
			continue
		raw_url = str(cite.get("url") or "").strip()
		url = canonical_video_url(raw_url) or raw_url
		title = str(cite.get("title") or "").strip()
		if url and url not in seen:
			seen.add(url)
			out.append({"url": url, "title": title or url})
		if len(out) >= max_results:
			break
	return out


def sync_cloud_from_ui(
	provider_var,
	base_url_var,
	model_var,
	search_model_var,
	keys_fn,
) -> tuple[bool, str]:
	"""Nạp CLOUD từ UI (dùng khi test ngoài pipeline). Trả (ok, lỗi)."""
	prov = (provider_var.get().strip() or "groq").lower()
	keys = keys_fn()
	if prov not in ("openrouter", "custom"):
		return False, "Chọn nhà cung cấp OpenRouter hoặc Custom ở tab Cloud AI."
	if not keys:
		return False, "Chưa có API key ở tab Cloud AI."
	base_in = base_url_var.get().strip()
	if prov == "openrouter":
		base = normalize_api_base_url(base_in or DEFAULT_OPENROUTER_BASE)
	elif prov == "custom":
		base = base_in.rstrip("/")
		if not base:
			return False, "Chưa có Base URL (tab Cloud AI)."
	else:
		base = ""
	CLOUD["provider"] = prov
	CLOUD["base_url"] = base
	CLOUD["model"] = model_var.get().strip()
	CLOUD["search_model"] = search_model_var.get().strip()
	CLOUD["keys"] = keys
	return True, ""


def parse_llm_video_search_results(text: str, max_results: int) -> list[dict[str, str]]:
	"""Trích URL video từ JSON hoặc văn bản LLM."""
	out: list[dict[str, str]] = []
	seen: set[str] = set()
	if not text:
		return out

	arr = _extract_json_array(text)
	if arr:
		for item in arr:
			if not isinstance(item, dict):
				continue
			url = canonical_video_url(str(item.get("url") or "")) or str(item.get("url") or "").strip()
			title = str(item.get("title") or "").strip()
			if url and url not in seen:
				seen.add(url)
				out.append({"url": url, "title": title or url})
			if len(out) >= max_results:
				return out

	obj = _extract_json_obj(text)
	if obj:
		for key in ("results", "videos", "items", "links"):
			val = obj.get(key)
			if isinstance(val, list):
				for item in val:
					if not isinstance(item, dict):
						continue
					url = canonical_video_url(str(item.get("url") or "")) or str(item.get("url") or "").strip()
					title = str(item.get("title") or "").strip()
					if url and url not in seen:
						seen.add(url)
						out.append({"url": url, "title": title or url})
					if len(out) >= max_results:
						return out

	url_re = re.compile(
		r"https?://(?:www\.)?(?:"
		r"youtube\.com/watch\?v=[A-Za-z0-9_-]{11}|youtu\.be/[A-Za-z0-9_-]{11}|"
		r"youtube\.com/shorts/[A-Za-z0-9_-]{11}|"
		r"(?:twitter\.com|x\.com)/(?:i|[^/\s]+)/status/\d+|"
		r"t\.me/[^/\s]+/\d+|facebook\.com/[^\s]+|tiktok\.com/[^\s]+|vk\.com/video[^\s]+"
		r")[^\s\"'<>)\]]*",
		re.IGNORECASE,
	)
	for m in url_re.finditer(text):
		raw = m.group(0).rstrip(".,;)")
		url = canonical_video_url(raw) or raw
		if url and url not in seen:
			seen.add(url)
			out.append({"url": url, "title": url})
		if len(out) >= max_results:
			break
	return out


def llm_web_search_rotate(query: str, max_results: int, keys: list, state: dict, logf=None) -> list | None:
	"""Tìm link video qua LLM — OpenRouter dùng server tool web_search (giống Deer Flow)."""
	if not keys:
		return None
	prov = (CLOUD.get("provider") or "").strip().lower()
	if prov not in ("openrouter", "custom"):
		return None
	base = CLOUD.get("base_url") or DEFAULT_OPENROUTER_BASE
	api_url = openai_chat_completions_url(base)
	use_web_tool = prov == "openrouter"
	model = effective_llm_search_model(use_web_tool=use_web_tool)
	n_results = max(1, min(25, int(max_results)))
	prompt = (
		f"Tìm các video YouTube / X.com tin tức và footage gốc liên quan đến:\n{query}\n\n"
		f"Liệt kê tối đa {n_results} video. Mỗi mục gồm tiêu đề + URL đầy đủ (youtube.com hoặc x.com).\n"
		"Ưu tiên tin tức, footage thật, không phải bài viết text."
	)
	video_domains = [
		"youtube.com", "youtu.be", "x.com", "twitter.com",
		"t.me", "facebook.com", "tiktok.com", "vk.com",
	]
	n = len(keys)
	start = int(state.get("idx", 0)) % n
	for tried in range(n):
		ki = (start + tried) % n
		key = keys[ki]
		payload: dict[str, Any] = {
			"model": model,
			"messages": [{"role": "user", "content": prompt}],
			"temperature": 0.2,
		}
		if use_web_tool:
			payload["tools"] = [{
				"type": "openrouter:web_search",
				"parameters": {
					"engine": "auto",
					"max_results": n_results,
					"allowed_domains": video_domains,
				},
			}]
		headers = {
			"Authorization": "Bearer " + key,
			"Content-Type": "application/json",
			**openai_compat_extra_headers(prov),
		}
		data, err = _http_post_json(api_url, payload, headers, 90)
		if err:
			low = str(err).lower()
			rotate = any(x in low for x in ("401", "402", "403", "429", "432", "quota", "rate", "limit", "credit"))
			if logf and tried == 0:
				logf(f"  [LLM-WEB] key #{ki + 1}/{n} lỗi ({err}) — {'xoay key' if rotate else 'bỏ qua'}")
			if rotate:
				state["idx"] = (ki + 1) % n
				continue
			return None
		text = None
		if isinstance(data, dict):
			ch = data.get("choices") or []
			if ch:
				text = (ch[0].get("message") or {}).get("content")
			usage = data.get("usage") or {}
			tool_use = usage.get("server_tool_use") or {}
			ws_n = tool_use.get("web_search_requests")
			if logf and ws_n:
				logf(f"  [LLM-WEB] OpenRouter web_search_requests={ws_n}")
		results = extract_openrouter_url_citations(data or {}, n_results)
		seen = {r["url"] for r in results}
		for item in parse_llm_video_search_results(text or "", n_results):
			u = item.get("url") or ""
			if u and u not in seen:
				seen.add(u)
				results.append(item)
			if len(results) >= n_results:
				break
		state["idx"] = ki
		if logf and results:
			logf(f"  [LLM-WEB] model={model} · {len(results)} link (tool={'web_search' if use_web_tool else 'chat'})")
		return results
	if logf:
		logf("  [LLM-WEB] tất cả key đều lỗi.")
	return None


def cloud_generate(prompt: str, model: str, timeout: int):
	"""Generate text via a free cloud LLM (Groq or Gemini). Returns text or None.
	Rotates through the configured API keys on failure, and (for Groq) auto-tries
	a few known model names if the configured/default one is unavailable. The
	exact failure reason is stored in CLOUD['last_error'] for the UI log."""
	keys = CLOUD.get("keys") or []
	if not keys:
		with cloud_lock:
			CLOUD["last_error"] = "chưa có API key"
		return None
	prov = (CLOUD.get("provider") or "groq").strip().lower()
	user_model = (model or "").strip()
	
	with cloud_lock:
		model_ok = CLOUD.get("model_ok")
	
	if prov == "gemini":
		candidates = [user_model] if user_model else ["gemini-1.5-flash", "gemini-1.5-flash-8b", "gemini-2.0-flash"]
	elif prov == "ollama":
		candidates = []
		for m in [model_ok, user_model, "gpt-oss:20b", "gpt-oss:120b", "llama3.1"]:
			if m:
				normalized = m.replace("openai/gpt-oss-", "gpt-oss:")
				if normalized not in candidates:
					candidates.append(normalized)
	elif prov in ("openrouter", "custom"):
		candidates = []
		for m in [model_ok, user_model, DEFAULT_OPENROUTER_MODEL,
				"meta-llama/llama-3.3-70b-instruct:free", "qwen/qwen-2.5-7b-instruct:free"]:
			if m and m not in candidates:
				candidates.append(m)
	else:
		# Prefer a model already confirmed to work, then the user's, then known-good ones.
		candidates = []
		for m in [model_ok, user_model,
				"llama-3.1-8b-instant", "llama-3.3-70b-versatile",
				"gemma2-9b-it"]:
			if m and m not in candidates:
				candidates.append(m)
	n = len(keys)
	with cloud_lock:
		CLOUD["last_error"] = None
	if prov == "ollama":
		key_order = ollama_key_indices_priority(keys, include_quota=False)
		if not key_order:
			key_order = list(range(n))
	else:
		key_order = None
	for mdl in candidates:
		model_unavailable = False
		max_tries = len(key_order) if key_order is not None else n
		tried = 0
		while tried < max_tries:
			if key_order is not None:
				with cloud_lock:
					cur_idx = int(CLOUD.get("idx", 0)) % n
				try:
					start_pos = key_order.index(cur_idx)
				except ValueError:
					start_pos = 0
				pos = (start_pos + tried) % len(key_order)
				idx = key_order[pos]
			else:
				with cloud_lock:
					idx = int(CLOUD.get("idx", 0)) % n
			tried += 1
			key = keys[idx]
			if prov == "gemini":
				url = ("https://generativelanguage.googleapis.com/v1beta/models/"
					+ mdl + ":generateContent?key=" + urllib.parse.quote(key))
				payload = {"contents": [{"parts": [{"text": prompt}]}]}
				headers = {"Content-Type": "application/json"}
			elif prov == "ollama":
				url = "https://ollama.com/v1/chat/completions"
				payload = {"model": mdl, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
				headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
			elif prov == "openrouter":
				url = openai_chat_completions_url(CLOUD.get("base_url") or DEFAULT_OPENROUTER_BASE)
				payload = {"model": mdl, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
				headers = {
					"Authorization": "Bearer " + key,
					"Content-Type": "application/json",
					**openai_compat_extra_headers("openrouter"),
				}
			elif prov == "custom":
				base = (CLOUD.get("base_url") or "").strip()
				if not base:
					with cloud_lock:
						CLOUD["last_error"] = "chưa có Base URL (tab Cloud AI)"
					return None
				url = openai_chat_completions_url(base)
				payload = {"model": mdl, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
				headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
			else:
				url = "https://api.groq.com/openai/v1/chat/completions"
				payload = {"model": mdl, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
				headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
			data, err = _http_post_json(url, payload, headers, timeout)
			if err is not None:
				with cloud_lock:
					CLOUD["last_error"] = f"[{mdl}] {err}"
				low = err.lower()
				# Bad / decommissioned / unknown model -> try the NEXT model (same key).
				if err.startswith("HTTP 404") or "decommission" in low or "does not exist" in low or "not found" in low or "invalid model" in low:
					model_unavailable = True
					break
				# Otherwise (auth/quota/rate-limit) -> rotate to the next key.
				with cloud_lock:
					if key_order is not None:
						try:
							pos_in_order = key_order.index(idx)
							CLOUD["idx"] = key_order[(pos_in_order + 1) % len(key_order)]
						except ValueError:
							CLOUD["idx"] = (idx + 1) % n
					else:
						CLOUD["idx"] = (idx + 1) % n
				continue
			text = None
			if prov == "gemini":
				cands = data.get("candidates") or []
				if cands:
					parts = ((cands[0].get("content") or {}).get("parts")) or []
					if parts and parts[0].get("text"):
						text = parts[0].get("text")
			else:
				ch = data.get("choices") or []
				if ch:
					text = (ch[0].get("message") or {}).get("content")
			if text:
				with cloud_lock:
					CLOUD["model_ok"] = mdl  # remember the model that worked
				return text
			with cloud_lock:
				CLOUD["last_error"] = f"[{mdl}] phản hồi rỗng/không đúng định dạng"
				if key_order is not None:
					try:
						pos_in_order = key_order.index(idx)
						CLOUD["idx"] = key_order[(pos_in_order + 1) % len(key_order)]
					except ValueError:
						CLOUD["idx"] = (idx + 1) % n
				else:
					CLOUD["idx"] = (idx + 1) % n
		if model_unavailable:
			continue
	return None


def cool_run(cmd, timeout):
	"""subprocess.run wrapper. In Cool mode it runs the child at lower CPU
	priority (nice) and adds a tiny cooldown afterwards, to avoid sustained
	thermal spikes on laptops. Otherwise it behaves exactly like subprocess.run
	(same return object, same exceptions propagated to the caller).

	Also: local Ollama generation calls of the form ["ollama","run",model,prompt]
	are transparently routed to a FREE cloud LLM when Cloud AI is enabled, so the
	heavy inference happens on the server and the laptop stays cool."""
	# This app no longer runs a local LLM. Any ["ollama","run",...] request is
	# served by the FREE cloud LLM; if the cloud is unavailable we fail soft
	# (caller falls back to Rules) instead of spawning a hot local model.
	if cmd and len(cmd) >= 2 and cmd[0] == "ollama" and cmd[1] == "run":
		try:
			if CLOUD.get("on") and (CLOUD.get("keys") or []) and len(cmd) >= 4:
				text = cloud_generate(cmd[3], CLOUD.get("model") or "", timeout)
				if text:
					return _LLMResp(0, text)
		except Exception:
			pass
		return _LLMResp(1, "")
	run_cmd = cmd
	try:
		if COOL.get("on") and os.name == "posix" and cmd and cmd[0] not in ("nice", "ionice") and shutil.which("nice"):
			run_cmd = ["nice", "-n", str(COOL.get("nice", 10))] + list(cmd)
	except Exception:
		run_cmd = cmd
	p = subprocess.run(run_cmd, capture_output=True, text=True, timeout=timeout)
	try:
		if COOL.get("on") and float(COOL.get("sleep", 0) or 0) > 0:
			time.sleep(float(COOL["sleep"]))
	except Exception:
		pass
	return p

SETTINGS_DIR = Path.home() / ".newsfootage_hunter"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"
LOG_PATH = SETTINGS_DIR / "app.log"


# ----------------------------
# Environment
# ----------------------------

def ensure_homebrew_path():
	"""Delegate to auto_setup so Finder-launched .app sees brew tools."""
	auto_setup.ensure_homebrew_path()


def apply_cool_mode_env(enabled: bool, ollama_threads: int):
	"""Reduce CPU spikes / heat on laptops by capping thread pools.
	Updates COOL['on'] and (best-effort) sets env vars honored by Ollama and the
	math libraries used by yt-dlp/ffmpeg/python deps. Safe if any are ignored."""
	CAP_VARS = (
		"OMP_NUM_THREADS",
		"OPENBLAS_NUM_THREADS",
		"MKL_NUM_THREADS",
		"VECLIB_MAXIMUM_THREADS",
		"NUMEXPR_NUM_THREADS",
	)
	try:
		COOL["on"] = bool(enabled)
		n = str(max(1, int(ollama_threads)))
		if enabled:
			os.environ["OLLAMA_NUM_PARALLEL"] = "1"
			os.environ["OLLAMA_NUM_THREAD"] = n
			for var in CAP_VARS:
				os.environ[var] = n
		else:
			# Don't force; let user/system decide.
			for var in ("OLLAMA_NUM_PARALLEL", "OLLAMA_NUM_THREAD") + CAP_VARS:
				os.environ.pop(var, None)
	except Exception:
		pass


def write_log_line(line: str):
	try:
		SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
		with open(LOG_PATH, "a", encoding="utf-8") as f:
			f.write(line + "\n")
	except Exception:
		pass


def preflight_missing_bins(check_gallery_dl: bool = False):
	"""Return missing CLI tools. Always check yt-dlp/ffmpeg/ffprobe;
	gallery-dl when X.com is enabled (or force via check_gallery_dl=True)."""
	ensure_homebrew_path()
	bins = ["yt-dlp", "ffmpeg", "ffprobe"]
	if check_gallery_dl:
		bins.append("gallery-dl")
	missing = auto_setup.missing_bins(bins)
	# Also accept gallery-dl inside project .venv when running from source
	if "gallery-dl" in missing:
		venv_gdl = Path(__file__).resolve().parent / ".venv" / "bin" / "gallery-dl"
		if venv_gdl.is_file():
			# prepend venv bin so later downloads find it
			vbin = str(venv_gdl.parent)
			path = os.environ.get("PATH", "")
			if vbin not in path.split(":"):
				os.environ["PATH"] = vbin + ":" + path
			missing = [b for b in missing if b != "gallery-dl"]
	return missing


def run_auto_setup_dialog(parent, log_fn=None, force: bool = False, need_gallery_dl: bool = True) -> bool:
	"""Modal progress dialog: install missing tools automatically."""
	ensure_homebrew_path()
	missing = preflight_missing_bins(check_gallery_dl=need_gallery_dl)
	if not missing and not force:
		if log_fn:
			log_fn("[OK] Đã đủ công cụ hệ thống.")
		return True

	win = tk.Toplevel(parent)
	win.title(f"{APP_NAME} — Cài đặt công cụ")
	win.geometry("640x420")
	win.transient(parent)
	win.grab_set()

	status_var = tk.StringVar(value="Đang kiểm tra / cài đặt công cụ cần thiết…")
	ttk.Label(win, textvariable=status_var).pack(anchor="w", padx=12, pady=(12, 6))
	ttk.Label(
		win,
		text="Thiếu: " + (", ".join(missing) if missing else "(cài lại theo yêu cầu)"),
	).pack(anchor="w", padx=12)

	txt = tk.Text(win, height=16, wrap="word")
	txt.pack(fill="both", expand=True, padx=12, pady=8)
	pbar = ttk.Progressbar(win, mode="indeterminate")
	pbar.pack(fill="x", padx=12, pady=(0, 8))
	pbar.start(12)

	btn_row = ttk.Frame(win)
	btn_row.pack(fill="x", padx=12, pady=(0, 12))
	cancel_event = threading.Event()
	result = {"ok": False, "missing": list(missing)}

	def append_log(line: str):
		def _do():
			try:
				txt.insert("end", line + "\n")
				txt.see("end")
			except Exception:
				pass
			if log_fn:
				try:
					log_fn(line)
				except Exception:
					pass
			try:
				write_log_line(line)
			except Exception:
				pass

		try:
			win.after(0, _do)
		except Exception:
			_do()

	def on_cancel():
		cancel_event.set()
		status_var.set("Đang hủy…")

	def on_close_btn():
		if result.get("done"):
			win.destroy()
		else:
			on_cancel()

	cancel_btn = ttk.Button(btn_row, text="Hủy", command=on_cancel)
	cancel_btn.pack(side="right")
	close_btn = ttk.Button(btn_row, text="Đóng", command=on_close_btn, state="disabled")
	close_btn.pack(side="right", padx=(0, 8))

	want_bins = ["yt-dlp", "ffmpeg", "ffprobe"]
	if need_gallery_dl or force:
		want_bins.append("gallery-dl")

	def worker():
		ok, still = auto_setup.ensure_dependencies(
			log=append_log,
			cancel=cancel_event.is_set,
			bins=want_bins,
		)
		result["ok"] = ok
		result["missing"] = still
		result["done"] = True

		def finish():
			try:
				pbar.stop()
			except Exception:
				pass
			try:
				cancel_btn.state(["disabled"])
				close_btn.state(["!disabled"])
			except Exception:
				pass
			if ok:
				status_var.set("✅ Đã cài xong — sẵn sàng dùng.")
				win.after(700, win.destroy)
			elif cancel_event.is_set():
				status_var.set("Đã hủy. Vẫn thiếu: " + ", ".join(still or missing))
			else:
				status_var.set("❌ Còn thiếu: " + ", ".join(still or missing))

		try:
			win.after(0, finish)
		except Exception:
			finish()

	threading.Thread(target=worker, daemon=True).start()
	parent.wait_window(win)
	ensure_homebrew_path()
	return len(preflight_missing_bins(check_gallery_dl=need_gallery_dl)) == 0


# ----------------------------
# VN phonetic normalization
# ----------------------------

PHONETIC_MAP = {
	# common VN phonetic -> standard spelling
	"mốts'kâu": "Moscow",
	"mốtscâu": "Moscow",
	"mát-xcơ-va": "Moscow",
	"mát xcơ va": "Moscow",
	"matxcova": "Moscow",
	"moskva": "Moscow",
	"washingtơn": "Washington",
	"oa-sinh-tơn": "Washington",
	"krưm": "Crimea",
	"kiev": "Kyiv",
	"ki-ép": "Kyiv",
	"kiep": "Kyiv",
	"khác-cốp": "Kharkiv",
	"kharkov": "Kharkiv",
	"kherson": "Kherson",
	"khéc-son": "Kherson",
	"kher-son": "Kherson",
	"zaporizhzhia": "Zaporizhzhia",
	"da-pô-ri-di-a": "Zaporizhzhia",
	"dapơriza": "Zaporizhzhia",
	"donetsk": "Donetsk",
	"đô-nét-xk": "Donetsk",
	"đônétxk": "Donetsk",
	"luhansk": "Luhansk",
	"lu-gan-sk": "Luhansk",
	"lugansk": "Luhansk",
	"bách-mút": "Bakhmut",
	"bakhmut": "Bakhmut",
	"odesa": "Odesa",
	"o-đét-xa": "Odesa",
	"mariupol": "Mariupol",
	"ma-ri-u-pol": "Mariupol",
	"wagner": "Wagner",
	"vác-nơ": "Wagner",
	"chasiv yar": "Chasiv Yar",
	"chát-síp-ya": "Chasiv Yar",
	"pokrovsk": "Pokrovsk",
	"pô-krốp-xk": "Pokrovsk",
	"avdiivka": "Avdiivka",
	"áp-đi-íp-ka": "Avdiivka",
	"vovchansk": "Vovchansk",
	"vô-vơ-chan-xk": "Vovchansk",
	"vuhledar": "Vuhledar",
	"vơ-lê-đan": "Vuhledar",
	"kupiansk": "Kupiansk",
	"ku-pi-an-xk": "Kupiansk",
	"lyman": "Lyman",
	"li-man": "Lyman",
	"toretsk": "Toretsk",
	"tô-rét-xk": "Toretsk",
	"myrnohrad": "Myrnohrad",
	"mi-rô-grát": "Myrnohrad",
	"kostiantynivka": "Kostiantynivka",
	"kốt-xtan-ti-nip-ka": "Kostiantynivka",
	"kramatorsk": "Kramatorsk",
	"kra-ma-to-xk": "Kramatorsk",
	"sloviansk": "Sloviansk",
	"xlô-vi-an-xk": "Sloviansk",
	"sumy": "Sumy",
	"xu-my": "Sumy",
	"mykolaiv": "Mykolaiv",
	"my-kô-lai-ép": "Mykolaiv",
	"melitopol": "Melitopol",
	"mê-li-tô-pôn": "Melitopol",
	"berdiansk": "Berdiansk",
	"béc-đi-an-xk": "Berdiansk",
	"sevastopol": "Sevastopol",
	"xê-vát-tô-pôn": "Sevastopol",
	"simferopol": "Simferopol",
	"xim-phê-rô-pôn": "Simferopol",
	"dnipro": "Dnipro",
	"đơ-níp-rô": "Dnipro",
	"kerch": "Kerch",
	"kéc-sơ": "Kerch",
	"tokmak": "Tokmak",
	"tốc-mác": "Tokmak",
	"enerhodar": "Enerhodar",
	"ê-nê-hô-đa": "Enerhodar",
	"nova kakhovka": "Nova Kakhovka",
	"nô-va ca-khốp-ka": "Nova Kakhovka",
	"ukraina": "Ukraine",
	"u-krai-na": "Ukraine",
	"u-crai-na": "Ukraine",
	
	# Leaders / People
	"zelensky": "Zelensky",
	"dê-len-sky": "Zelensky",
	"putin": "Putin",
	"pu-tin": "Putin",
	"budanov": "Budanov",
	"bu-đa-nốp": "Budanov",
	
	# Weapons / Hardware
	"himars": "HIMARS",
	"hai-mác": "HIMARS",
	"atacms": "ATACMS",
	"a-tát-căm": "ATACMS",
	"bayraktar": "Bayraktar",
	"ba-rắc-ta": "Bayraktar",
	"lancet": "Lancet",
	"lan-xét": "Lancet",
	"iskander": "Iskander",
	"ít-khan-đơ": "Iskander",
	"kalibr": "Kalibr",
	"ca-líp": "Kalibr",
	"kinzhal": "Kinzhal",
	"kin-dan": "Kinzhal",
	"patriot": "Patriot",
	"pa-tri-ốt": "Patriot",
}

NUMBER_PHONETIC_MAP = {
	"không": "0",
	"một": "1",
	"mốt": "1",
	"hai": "2",
	"ba": "3",
	"bốn": "4",
	"tư": "4",
	"năm": "5",
	"lăm": "5",
	"nhăm": "5",
	"sáu": "6",
	"bảy": "7",
	"tám": "8",
	"chín": "9",
}

# TTS việt hoá → chuẩn tìm kiếm (đảo ngược quy tắc đọc AI)
TTS_PHRASE_REVERSE = [
	(r"(?i)\bLực lượng Phòng vệ Israel\b", "IDF"),
	(r"(?i)\bĐảng Cộng sản Trung Quốc\b", "ĐCSTQ"),
	(r"(?i)\bchế độ chuyên chế\b", "Cộng sản"),
	(r"(?i)\btoàn trị\b", "độc tài"),
	(r"(?i)\bkilomet\s+trên\s+giờ\b", "km/h"),
	(r"(?i)\bđô\s+la\s+mỹ\b", "USD"),
	(r"(?i)\bthế\s+chiến\s+1\b", "Thế chiến I"),
	(r"(?i)\bthế\s+chiến\s+2\b", "Thế chiến II"),
	(r"(?i)\bym\s+lặng\b", "im lặng"),
	(r"(?i)\b(\d+)\s+mét\b", r"\1 m"),
]

_VN_NUM_TOKEN = (
	r"không|một|mốt|hai|ba|bốn|tư|năm|lăm|nhăm|sáu|bảy|tám|chín|"
	r"mười|mươi|trăm|nghìn|ngàn"
)


def parse_vn_number_tokens(tokens: list[str]) -> int | None:
	"""Parse a token list like ['hai','không','hai','lăm'] -> 2025."""
	d_map = NUMBER_PHONETIC_MAP
	if not tokens:
		return None
	if not any(t in ("mười", "mươi", "trăm", "nghìn", "ngàn") for t in tokens):
		if len(tokens) >= 2:
			if all(t in d_map for t in tokens):
				return int("".join(d_map[t] for t in tokens))
		elif len(tokens) == 1 and tokens[0] in d_map:
			return int(d_map[tokens[0]])
		return None

	total = 0
	current = 0
	i = 0
	n = len(tokens)
	while i < n:
		t = tokens[i]
		if t in d_map:
			val = int(d_map[t])
			if i + 1 < n and tokens[i + 1] == "trăm":
				total += val * 100
				i += 2
			elif i + 1 < n and tokens[i + 1] in ("nghìn", "ngàn"):
				total += (current + val) * 1000
				current = 0
				i += 2
			elif i + 1 < n and tokens[i + 1] in ("mươi", "mười"):
				next_val = 0
				if i + 2 < n and tokens[i + 2] in d_map:
					next_val = int(d_map[tokens[i + 2]])
					i += 3
				else:
					i += 2
				total += val * 10 + next_val
			else:
				current += val
				i += 1
		elif t == "mười":
			val = 10
			if i + 1 < n and tokens[i + 1] in d_map:
				val += int(d_map[tokens[i + 1]])
				i += 2
			else:
				i += 1
			current += val
		elif t in ("nghìn", "ngàn"):
			total += current * 1000
			current = 0
			i += 1
		elif t == "trăm":
			i += 1
		elif t in ("mươi", "mười"):
			total += current * 10
			current = 0
			i += 1
		else:
			return None
	return total + current


def _vn_number_chunk_pattern() -> str:
	return rf"(?:{_VN_NUM_TOKEN})(?:\s+(?:{_VN_NUM_TOKEN}))*"


def parse_vn_year_tokens(tokens: list[str]) -> int | None:
	"""Đọc năm TTS: hai không hai sáu→2026; hai không hai mươi→2020."""
	if not tokens:
		return None
	toks = [t.strip().lower() for t in tokens if t.strip()]
	if all(t in NUMBER_PHONETIC_MAP for t in toks):
		s = "".join(NUMBER_PHONETIC_MAP[t] for t in toks)
		if len(s) == 4 and s.startswith(("19", "20")):
			return int(s)
	if len(toks) >= 2 and toks[-1] == "mươi":
		pref = toks[:-1]
		if all(t in NUMBER_PHONETIC_MAP for t in pref):
			s = "".join(NUMBER_PHONETIC_MAP[t] for t in pref)
			if len(s) == 3:
				return int(s + "0")
	return None


def _parse_number_chunk(chunk: str) -> str | None:
	chunk = (chunk or "").strip().lower()
	if not chunk:
		return None
	if chunk.isdigit():
		return chunk
	tokens = [t.strip() for t in re.split(r"['\s]+", chunk) if t.strip()]
	if not tokens:
		return None
	year = parse_vn_year_tokens(tokens)
	if year is not None:
		return str(year)
	val = parse_vn_number_tokens(tokens)
	return str(val) if val is not None else None


def reverse_vn_dates(text: str) -> str:
	"""ngày mười sáu tháng sáu năm hai không hai sáu -> 16/6/2026"""
	chunk = _vn_number_chunk_pattern()
	out = text

	def _repl_full(m: re.Match) -> str:
		d = _parse_number_chunk(m.group(1)) or m.group(1)
		mo = _parse_number_chunk(m.group(2)) or m.group(2)
		y = _parse_number_chunk(m.group(3)) or m.group(3)
		return f"{d}/{mo}/{y}"

	out = re.sub(
		rf"(?i)\bngày\s+({chunk}|\d{{1,4}})\s+tháng\s+({chunk}|\d{{1,2}})\s+năm\s+({chunk}|\d{{2,4}})\b",
		_repl_full,
		out,
	)

	def _repl_dm(m: re.Match) -> str:
		d = _parse_number_chunk(m.group(1)) or m.group(1)
		mo = _parse_number_chunk(m.group(2)) or m.group(2)
		return f"{d}/{mo}"

	out = re.sub(
		rf"(?i)\bngày\s+({chunk}|\d{{1,2}})\s+tháng\s+({chunk}|\d{{1,2}})\b",
		_repl_dm,
		out,
	)
	return out


def reverse_vn_year_phrases(text: str) -> str:
	"""năm hai không hai lăm -> 2025 (chỉ khi ra năm 4 chữ số hợp lệ)."""
	chunk = _vn_number_chunk_pattern()

	def _repl(m: re.Match) -> str:
		raw = m.group(1)
		val = _parse_number_chunk(raw)
		if val and len(val) == 4 and val.startswith(("19", "20")):
			return val
		return m.group(0)

	return re.sub(rf"(?i)\bnăm\s+({chunk})\b", _repl, text)


def reverse_vn_percent_and_decimal(text: str) -> str:
	chunk = _vn_number_chunk_pattern()
	out = text

	def _pct_words(m: re.Match) -> str:
		n = _parse_number_chunk(m.group(1))
		return f"{n}%" if n else m.group(0)

	out = re.sub(rf"(?i)\b({chunk}|\d+(?:\.\d+)?)\s*phần\s*trăm\b", _pct_words, out)
	out = re.sub(r"(?i)(\d+(?:\.\d+)?)\s*phần\s*trăm\b", r"\1%", out)

	def _dec_words(m: re.Match) -> str:
		a = _parse_number_chunk(m.group(1))
		b = _parse_number_chunk(m.group(2))
		if a is not None and b is not None:
			return f"{a}.{b}"
		return m.group(0)

	out = re.sub(rf"(?i)\b({chunk}|\d+)\s*phẩy\s*({chunk}|\d+)\b", _dec_words, out)
	return out


def apply_tts_phrase_reverse(text: str) -> str:
	out = text or ""
	for pat, rep in TTS_PHRASE_REVERSE:
		out = re.sub(pat, rep, out)
	return out


def normalize_vn_number_words(text: str) -> str:
	# 1) Handle mixed numbers: e.g. "5 nghìn 48" or "5 nghìn" -> 5048, 5000
	def repl_mixed(m: re.Match):
		x = int(m.group(1))
		unit = m.group(2)
		y = m.group(3)
		val = x * 1000
		if y:
			val += int(y)
		return str(val)
	out = re.sub(r"(\d+)\s*(nghìn|ngàn)(?:\s+(\d+))?", repl_mixed, text, flags=re.IGNORECASE)

	# 2) Handle decimal numbers using "phẩy"
	# e.g., 12phẩy7 -> 12.7, 12 phẩy 6 -> 12.6, 1phẩy5 -> 1.5
	out = re.sub(r"(\d+)\s*phẩy\s*(\d+)", r"\1.\2", out, flags=re.IGNORECASE)

	# 3) Normal word-to-digit conversion
	num_words_no_nam = [w for w in NUMBER_PHONETIC_MAP.keys() if w != "năm"] + ["mười", "mươi", "trăm", "nghìn", "ngàn"]
	year_digits = list(NUMBER_PHONETIC_MAP.keys()) + ["mười"]
	lookahead = r"(?!\s+(?:\d+|" + "|".join(year_digits) + r"))"
	single_token_pat = r"(?:\b(?:năm" + lookahead + r"|" + "|".join(num_words_no_nam) + r")\b)"
	pattern_str = single_token_pat + r"(?:\s+" + single_token_pat + r")*"
	
	def repl(m: re.Match):
		raw = m.group(0)
		tokens = raw.lower().split()
		if len(tokens) < 2:
			return raw
		val = parse_vn_number_tokens(tokens)
		if val is not None:
			return str(val)
		return raw

	out = re.sub(pattern_str, repl, out, flags=re.IGNORECASE)
	all_num_words = list(NUMBER_PHONETIC_MAP.keys()) + ["mười", "mươi", "trăm", "nghìn", "ngàn"]
	single_word_pattern = r"(?i)\b(ngày|tháng|năm)\s+(" + "|".join(all_num_words) + r")\b"
	def repl_single(m: re.Match):
		prefix = m.group(1)
		word = m.group(2).lower()
		if word in NUMBER_PHONETIC_MAP:
			return f"{prefix} {NUMBER_PHONETIC_MAP[word]}"
		elif word == "mười":
			return f"{prefix} 10"
		return m.group(0)
		
	out = re.sub(single_word_pattern, repl_single, out)
	return out


def normalize_script(script: str) -> str:
	out = script or ""
	for k, v in PHONETIC_MAP.items():
		out = re.sub(re.escape(k), v, out, flags=re.IGNORECASE)

	def repl_number(m: re.Match):
		raw = m.group(0)
		tokens = [t.strip().lower() for t in re.split(r"'+", raw) if t.strip()]
		if not tokens:
			return raw
		year = parse_vn_year_tokens(tokens)
		if year is not None:
			return str(year)
		if all(t in NUMBER_PHONETIC_MAP for t in tokens):
			return "".join(NUMBER_PHONETIC_MAP[t] for t in tokens)
		return raw

	out = re.sub(
		r"(?i)\b(?:[a-zÀ-ỹ\d]+')(?:[a-zÀ-ỹ\d]+'?){1,8}\b",
		repl_number,
		out,
	)
	out = normalize_vn_number_words(out)
	return out


def standardize_script_for_search_and_export(text: str) -> str:
	"""Đảo ngược 'việt hoá' TTS (AI đọc) → dạng chuẩn để tìm kiếm footage.

	Examples:
	- "năm hai không hai lăm" / "hai'không'hai'lăm" -> "2025"
	- "ngày mười sáu tháng sáu năm hai không hai sáu" -> "16/6/2026"
	- "năm nghìn bốn trăm tám mươi" -> "5480"
	- "mười hai phẩy bảy phần trăm" -> "12.7%"
	- "kilomet trên giờ" -> "km/h", "đô la Mỹ" -> "USD"
	- "Mốtscâu" -> "Moscow", "Lực lượng Phòng vệ Israel" -> "IDF"

	Chỉ đổi số/ngày/cụm từ cố định — không dịch hay sửa nội dung câu.
	"""
	out = text or ""
	# 1) Cụm TTS cố định (IDF, ĐCSTQ, km/h, mét→m, …)
	out = apply_tts_phrase_reverse(out)
	# 2) Ngày tháng viết bằng chữ
	out = reverse_vn_dates(out)
	# 3) Năm viết bằng chữ: "năm hai không hai lăm"
	out = reverse_vn_year_phrases(out)
	# 3b) Năm đứng riêng không có chữ "năm": "hai không hai mươi" -> 2020
	chunk = _vn_number_chunk_pattern()

	def _repl_standalone_year(m: re.Match) -> str:
		val = _parse_number_chunk(m.group(0))
		if val and len(val) == 4 and val.startswith(("19", "20")):
			return val
		return m.group(0)

	out = re.sub(rf"(?i)\b({chunk})\b", _repl_standalone_year, out)
	# 4) % và số thập phân bằng chữ
	out = reverse_vn_percent_and_decimal(out)
	# 5) Phiên âm địa danh + năm kiểu hai'không'hai'sáu + số ghép
	out = normalize_script(out)
	# 6) Ngày đã có chữ số: ngày 8 tháng 6 năm 2026
	out = re.sub(
		r"(?i)\bngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{2,4})\b",
		r"\1/\2/\3",
		out,
	)
	out = re.sub(
		r"(?i)\bngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\b",
		r"\1/\2",
		out,
	)
	out = re.sub(r"\s+", " ", out).strip()
	return out


# ----------------------------
# Paragraph parsing
# ----------------------------

def split_paragraphs(script: str) -> list[str]:
	lines = [ln.strip() for ln in (script or "").splitlines()]
	return [ln for ln in lines if ln]


def parse_doan_prefix(line: str) -> tuple[int | None, str]:
	m = re.match(r"(?i)^\s*đoạn\s*(\d+)\s*:\s*(.*)$", (line or "").strip())
	if not m:
		return None, (line or "").strip()
	return int(m.group(1)), m.group(2).strip()


# ----------------------------
# Smart keyword extraction
# Read the paragraph and pull the REAL searchable entities (road numbers, places,
# units, people, weapons, actions) instead of the intro/filler words.
# ----------------------------

# Road / route codes: M14, H20, P57, E105, T0403, А-260 (Latin + Cyrillic prefixes).
ROAD_RE = re.compile(r"(?<![0-9A-Za-zА-Яа-я])([MHPTEKAМНРЕТКА]{1,2})[\-\s]?(\d{1,4})(?![0-9A-Za-z])")

GAZ_PLACES = [
	# Ukraine: oblasts / cities / fronts
	"Crimea", "Krym", "Kherson", "Zaporizhzhia", "Zaporozhye", "Donetsk", "Luhansk",
	"Lugansk", "Donbas", "Kyiv", "Kharkiv", "Kharkov", "Pokrovsk", "Avdiivka",
	"Bakhmut", "Chasiv Yar", "Kupiansk", "Lyman", "Vovchansk", "Huliaipole",
	"Hulyaipole", "Oleksandrivka", "Robotyne", "Toretsk", "Vuhledar", "Myrnohrad",
	"Kostiantynivka", "Kramatorsk", "Sloviansk", "Sumy", "Mykolaiv", "Odesa",
	"Odessa", "Mariupol", "Melitopol", "Berdiansk", "Sevastopol", "Simferopol",
	"Kinburn", "Dnipro", "Dnieper", "Kerch", "Tokmak", "Enerhodar", "Nova Kakhovka",
	# Russia
	"Moscow", "Saint Petersburg", "Belgorod", "Kursk", "Bryansk", "Rostov",
	"Novorossiysk", "Krasnodar", "Voronezh", "Tuapse", "Engels", "Taganrog",
]
GAZ_PEOPLE = [
	"Zelensky", "Zelenskyy", "Zelenskiy", "Syrskyi", "Syrsky", "Putin", "Budanov",
	"Voloshyn", "Brovdi", "Shoigu", "Gerasimov", "Umerov", "Medvedev",
]
GAZ_UNITS = [
	"ATESH", "GUR", "HUR", "SBU", "Azov", "Magyar", "Khortytsia", "Wagner",
	"Akhmat", "Rubizh", "3rd Assault Brigade", "93rd Brigade",
]
GAZ_WEAPONS = [
	"drone", "FPV", "Shahed", "Geran", "HIMARS", "ATACMS", "Storm Shadow", "Neptune",
	"Bayraktar", "Lancet", "Iskander", "Kalibr", "Kinzhal", "Patriot", "Tochka",
	"Grad", "Smerch", "Uragan", "Tor", "Pantsir", "glide bomb", "KAB", "UMPK",
]
# Vietnamese term -> English search word (so we never search in Vietnamese).
VN_TERM_MAP = {
	"tấn công": "attack", "tập kích": "strike", "không kích": "airstrike",
	"pháo kích": "shelling", "phòng không": "air defense", "hậu cần": "logistics",
	"tiếp tế": "resupply", "đoàn xe": "convoy", "vận tải": "transport",
	"sân bay": "airfield", "tên lửa": "missile", "xe tăng": "tank",
	"thiết giáp": "armor", "kho đạn": "ammo depot", "nhà máy lọc dầu": "oil refinery",
	"tàu chiến": "warship", "hạm đội": "fleet", "cháy nổ": "explosion",
	"tuyến đường": "highway", "cây cầu": "bridge", "pháo binh": "artillery",
	"bắn hạ": "shoot down", "đánh chặn": "intercept", "giao tranh": "clash",
	"tiến công": "advance",
}


def extract_key_terms(paragraph: str) -> dict:
	"""Read a paragraph and pull the REAL searchable entities (not filler words)."""
	text = normalize_script(paragraph or "")
	tl = text.lower()
	roads: list[str] = []
	for m in ROAD_RE.finditer(text):
		code = (m.group(1) + m.group(2)).upper()
		if code not in roads:
			roads.append(code)

	def scan(gaz: list[str]) -> list[str]:
		found: list[str] = []
		for name in gaz:
			if name.lower() in tl and name not in found:
				found.append(name)
		return found

	places = scan(GAZ_PLACES)
	people = scan(GAZ_PEOPLE)
	units = scan(GAZ_UNITS)
	weapons = scan(GAZ_WEAPONS)

	actions: list[str] = []
	for vn, en in VN_TERM_MAP.items():
		if vn in tl and en not in actions:
			actions.append(en)

	if re.search(r"\bnga\b", tl) and "Russia" not in places:
		places.append("Russia")
	if "ukrain" in tl and "Ukraine" not in places:
		places.append("Ukraine")
	if ("trung quốc" in tl or "bắc kinh" in tl) and "China" not in places:
		places.append("China")

	return {
		"roads": roads, "places": places, "people": people,
		"units": units, "weapons": weapons, "actions": actions,
	}


def paragraph_hint(paragraph: str) -> str:
	# Prefer the real extracted entities; fall back to trimmed first words (display only).
	terms = extract_key_terms(paragraph)
	bits: list[str] = []
	for x in terms["roads"] + [p for p in terms["places"] if p not in ("Russia", "Ukraine", "China")]:
		if x not in bits:
			bits.append(x)
	for x in terms["people"][:1] + terms["units"][:1] + terms["weapons"][:1]:
		if x not in bits:
			bits.append(x)
	if bits:
		return " ".join(bits[:6])
	p = normalize_script(paragraph)
	p = re.sub(r"\s+", " ", p).strip()
	p = re.sub(r"^thưa\s+quý\s+vị\s*,?\s*", "", p, flags=re.IGNORECASE)
	words = p.split(" ")
	hint = " ".join(words[:7]).strip("-–—:;,. ")
	return hint


# ----------------------------
# HTML export + X links
# ----------------------------

def x_search_url(query: str) -> str:
	q = query.strip()
	return "https://x.com/search?q=" + urllib.parse.quote(q) + "&src=typed_query&f=live"


def build_x_search_queries(content: str, youtube_queries: list[str] | None = None) -> list[str]:
	"""Build SHORT X.com queries (max ~5 words). Long AI queries return 0 results on X."""
	text = standardize_script_for_search_and_export(content or "")
	tl = text.lower()
	terms = extract_key_terms(content)
	qs: list[str] = []

	people = list(terms.get("people") or [])
	places = [p for p in (terms.get("places") or []) if p not in ("Russia", "Ukraine", "China")]
	names = re.findall(r"\b([A-Z][a-zA-Z''\u2019-]+(?:\s+[A-Z][a-zA-Z''\u2019-]+)+)\b", text)

	sport_tokens: list[str] = []
	for w in (
		"Croatia", "Portugal", "Toronto", "Canada", "football", "soccer", "goal", "VAR",
		"Ronaldo", "stadium", "World Cup", "sensor", "drone", "missile", "explosion",
		"Moscow", "Kyiv", "Ukraine", "Russia", "China", "Taiwan",
	):
		if w.lower() in tl and w not in sport_tokens:
			sport_tokens.append(w)

	if names:
		n = names[0]
		tail = sport_tokens[0] if sport_tokens else "video"
		qs.append(x_search.simplify_x_query(f"{n} {tail}", max_words=4))
	if len(places) >= 2:
		qs.append(x_search.simplify_x_query(f"{places[0]} {places[1]} goal", max_words=5))
	elif places and sport_tokens:
		qs.append(x_search.simplify_x_query(f"{places[0]} {sport_tokens[0]}", max_words=4))
	elif sport_tokens:
		qs.append(x_search.simplify_x_query(" ".join(sport_tokens[:3]), max_words=5))
	if people:
		qs.append(x_search.simplify_x_query(f"{people[0]} video", max_words=3))

	for yq in (youtube_queries or [])[:2]:
		sq = x_search.simplify_x_query(yq, max_words=5)
		if sq:
			qs.append(sq)

	ph = paragraph_hint(content)
	if ph:
		qs.append(x_search.simplify_x_query(ph, max_words=5))

	# Legacy targeted rules (Ukraine war scripts)
	qs.extend(x_queries_for_paragraph(content))

	seen: set[str] = set()
	out: list[str] = []
	for q in qs:
		s = (q or "").strip()
		k = s.lower()
		if s and k not in seen and len(s.split()) <= 6:
			seen.add(k)
			out.append(s)
	return out[:3]


def x_queries_for_paragraph(content: str) -> list[str]:
	"""Generate practical X.com search queries.

	X search works best with English/local spellings, not Vietnamese.
	So we normalize phonetic VN -> EN and build short EN queries.
	"""
	text = normalize_script(content or "")
	tl = text.lower()
	qs: list[str] = []

	# Avoid Vietnamese in X queries: use only entity/keyword style queries.
	# (The free-text hint often includes Vietnamese words and performs poorly on X.)

	# Add targeted queries
	if "moscow" in tl:
		if "drone" in tl or "uav" in tl or "strike" in tl or "attack" in tl:
			qs.append("Moscow drone attack")
		else:
			qs.append("Moscow")
	if "saint petersburg" in tl:
		qs.append("Saint Petersburg drone attack")
	if "zelensky" in tl or "zelenskyy" in tl:
		qs.append("Zelensky statement")
	if "syrskyi" in tl:
		qs.append("Syrskyi statement")
	if "putin" in tl:
		qs.append("Putin Moscow")
	if any(k in tl for k in ["donetsk", "zaporizhzhia", "donbas", "pokrovsk", "huliaipole", "oleksandrivka", "crimea", "kyiv"]):
		qs.append("Ukraine frontline footage")
	# dedup + cap 3
	seen = set()
	out = []
	for q in qs:
		k = q.strip().lower()
		if k and k not in seen:
			seen.add(k)
			out.append(q.strip())
	return out[:3]


def x_queries_with_cloud(content: str) -> list[str] | None:
	"""Generate 1-3 highly accurate X.com search queries using the configured Cloud AI.
	
	It goes straight to the point to find the most accurate video possible.
	Returns None if Cloud AI is disabled or fails.
	"""
	if not llm_enabled():
		return None
	text = normalize_script(content or "")
	prompt = (
		"You are a master news video editor searching for ORIGINAL footage on X.com.\n"
		"Analyze the following paragraph and extract the CORE keywords (straight to the point) "
		"for the most accurate video search query.\n"
		"Output ONLY a JSON array of 1 to 3 highly optimized X.com search queries.\n"
		"CRITICAL: Each query MUST be 2-5 words MAX. X search breaks with long queries.\n"
		"Good: 'Croatia VAR goal', 'Igor Matanovic sensor', 'Toronto stadium goal'.\n"
		"Bad: 'Igor Matanovic hair contact ball sensor footage' (too long).\n"
		"Queries MUST be in English. Do NOT output Vietnamese. No 'footage' unless 1 word left.\n\n"
		"Paragraph:\n" + text.strip()
	)
	
	try:
		res_str = cloud_generate(prompt, model=CLOUD.get("model", ""), timeout=25)
		if not res_str:
			return None
			
		arr = _extract_json_array(res_str)
		if arr:
			qs = [x_search.simplify_x_query(str(x).strip(), max_words=5) for x in arr if str(x).strip()]
			qs = [q for q in qs if q]
			return qs[:3]
	except Exception:
		pass
	return None


def export_numbered_script_html(project_root: Path, paragraphs: list[str]):
	meta_dir = project_root / "03_metadata"
	meta_dir.mkdir(parents=True, exist_ok=True)
	html_path = meta_dir / "script_numbered.html"

	items = []
	for i, p in enumerate(paragraphs, start=1):
		doan_i, content = parse_doan_prefix(p)
		content_std = standardize_script_for_search_and_export(content)
		idx = doan_i if doan_i is not None else i
		ph = paragraph_hint(content_std)
		# Prefer Cloud AI queries for X when available, else fallback rules
		xq = x_queries_with_cloud(content_std)
		if not xq:
			xq = x_queries_for_paragraph(content_std)
		x_links = " ".join([
			f"<a class='xlink' href='{x_search_url(q)}' target='_blank' rel='noreferrer'>X: {q}</a>"
			for q in xq
		])

		article_urls = extract_article_urls(content_std)
		art_link_html = ""
		if article_urls:
			art_link_html = " ".join([
				"<a class='artlink' href='"
				+ u.replace("&", "&amp;").replace("'", "%27").replace('"', "&quot;")
				+ "' target='_blank' rel='noreferrer'>Bai bao</a>"
				for u in article_urls
			])

		safe_p = (
			content_std.replace("&", "&amp;")
			.replace("<", "&lt;")
			.replace(">", "&gt;")
		)

		items.append(
			f"<div class='para'>"
			f"<div class='head'><span class='doan'>ĐOẠN {idx}</span>: <span class='hint'>{ph}</span></div>"
			f"<div class='links'>{x_links}</div>"
			f"{art_link_html}"
			f"<div class='body'>{safe_p}</div>"
			f"</div>"
		)

	# IMPORTANT: Build HTML without an f-string so CSS braces work reliably.
	html = """<!doctype html>
<html lang='vi'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Script (numbered) — NewsFootage Hunter</title>
  <style>
	body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;margin:24px;background:#0b0f14;color:#e8eef6;}
	.para{border:1px solid rgba(255,255,255,.10);border-radius:14px;padding:14px 16px;margin:14px 0;background:#111826;}
	.head{font-weight:800;margin-bottom:10px;line-height:1.25;display:flex;flex-wrap:wrap;align-items:center;gap:10px;}
	.doan{display:inline-block;font-weight:900;letter-spacing:.4px;padding:4px 10px;border-radius:999px;background:linear-gradient(90deg,#ff6a00,#ffb700);color:#111826;}
	.hint{font-weight:750;color:#cfe0ff;}
	.links{margin:0 0 12px 0;display:flex;flex-wrap:wrap;gap:10px;}
	a.xlink{color:#9ad0ff;text-decoration:none;border:1px solid rgba(154,208,255,.25);padding:6px 10px;border-radius:999px;background:rgba(13,21,34,.9);font-weight:700;}
	a.xlink:hover{background:rgba(17,32,51,.95);border-color:rgba(154,208,255,.45);}
	a.artlink{color:#ffd89a;text-decoration:none;border:1px solid rgba(255,216,154,.25);padding:6px 10px;border-radius:999px;background:rgba(34,24,13,.9);font-weight:700;}
	a.artlink:hover{background:rgba(51,34,17,.95);border-color:rgba(255,216,154,.45);}
	.body{line-height:1.60;color:#e8eef6;white-space:pre-wrap;opacity:.95;}
  </style>
</head>
<body>
  <h1 style='margin:0 0 10px 0'>Kịch bản đã đánh số đoạn</h1>
  <div style='opacity:.8;margin-bottom:18px'>Bấm link X hoặc link bài báo bên dưới để xem dữ liệu gốc theo từng ĐOẠN.</div>
  __ITEMS__
</body>
</html>"""

	html = html.replace("__ITEMS__", "".join(items))

	html_path.write_text(html, encoding="utf-8")
	return html_path


# ----------------------------
# AI (Ollama optional)
# ----------------------------

def is_ollama_running() -> bool:
	try:
		with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1.5) as r:
			return r.status == 200
	except Exception:
		return False


def llm_enabled() -> bool:
	"""True if an LLM backend is usable. Cloud is preferred; local Ollama is
	only considered if the user still happens to have it installed and running."""
	if CLOUD.get("on") and (CLOUD.get("keys") or []):
		return True
	try:
		return shutil.which("ollama") is not None and is_ollama_running()
	except Exception:
		return False


def _extract_json_obj(text: str) -> dict | None:
	"""Best-effort extract first JSON object from a model output."""
	if not text:
		return None
	m = re.search(r"\{[\s\S]*\}", text)
	if not m:
		return None
	try:
		obj = json.loads(m.group(0))
		return obj if isinstance(obj, dict) else None
	except Exception:
		return None


def _extract_json_array(text: str) -> list | None:
	"""Best-effort extract first JSON array from a model output."""
	if not text:
		return None
	m = re.search(r"\[[\s\S]*\]", text)
	if not m:
		return None
	try:
		arr = json.loads(m.group(0))
		return arr if isinstance(arr, list) else None
	except Exception:
		return None


def infer_script_context_with_ollama(script: str, model: str) -> dict | None:
	"""Infer global context from the FULL script.

	We use this to:
	- infer a likely year / time window (e.g. 2026)
	- extract key entities (locations/people/events)
	- keep paragraph-level queries consistent with the global timeline

	Returns a dict like:
	{
	  "year": 2026,
	  "time_range": "May–Jun 2026",
	  "topic": "Ukraine-Russia war",
	  "entities": ["Moscow", "Kyiv", "Zelensky"],
	  "style": "find original footage, avoid old compilations"
	}
	"""
	if not llm_enabled():
		return None

	text = (script or "").strip()
	if not text:
		return None

	# Keep prompt deterministic and JSON-only.
	prompt = (
		"You are a newsroom assistant helping find original, timely footage. "
		"Read the FULL Vietnamese script and infer the global timeline context. "
		"Output ONLY one JSON object with keys: year (integer or null), time_range (string or null), "
		"topic (string), entities (array of strings, max 12), notes (string).\n\n"
		"Rules:\n"
		"- If the script implies a time (recent events) but doesn't say the year explicitly, infer it.\n"
		"- Prefer the most likely year, not a list.\n"
		"- Entities should be English/local spelling (Moscow, Kyiv, Zaporizhzhia, Zelensky, Syrskyi…).\n"
		"- notes should mention any recency constraints (e.g. 'prefer videos from 2026').\n\n"
		"SCRIPT:\n" + text
	)

	cmd = ["ollama", "run", model, prompt]
	try:
		p = cool_run(cmd, 90)
	except Exception:
		return None
	if p.returncode != 0:
		return None

	out = (p.stdout or "").strip()
	obj = _extract_json_obj(out)
	if not obj:
		return None

	# Normalize
	y = obj.get("year")
	try:
		obj["year"] = int(y) if y is not None and str(y).strip() != "" else None
	except Exception:
		obj["year"] = None

	ents = obj.get("entities")
	if isinstance(ents, list):
		obj["entities"] = [str(x).strip() for x in ents if str(x).strip()][:12]
	else:
		obj["entities"] = []

	obj["topic"] = str(obj.get("topic") or "").strip() or "news"
	obj["time_range"] = str(obj.get("time_range") or "").strip() or None
	obj["notes"] = str(obj.get("notes") or "").strip() or ""
	return obj


def date_str_to_year(upload_date: str) -> int | None:
	# upload_date from yt-dlp is usually YYYYMMDD
	ud = (upload_date or "").strip()
	if len(ud) == 8 and ud.isdigit():
		try:
			return int(ud[:4])
		except Exception:
			return None
	return None


def in_year_window(upload_date: str, center_year: int | None, window_years: int = DEFAULT_YEAR_WINDOW) -> bool:
	if not center_year:
		return True
	y = date_str_to_year(upload_date)
	if not y:
		return True
	return (center_year - window_years) <= y <= (center_year + window_years)


def _safe_snip(s: str, n: int = 280) -> str:
	s = (s or "").strip()
	return s if len(s) <= n else (s[: n - 3] + "...")


def rerank_candidates_with_ollama(
	paragraph: str,
	candidates: list[dict],
	context: dict | None,
	model: str,
) -> list[dict] | None:
	"""Re-rank candidate videos with a local LLM.

	Input candidates: list of dict with keys: url,title,channel,upload_date,duration,resolution
	Return: same items with an added 'score' (0-100) sorted desc.

	This is intentionally 'heavy' (token/latency) but yields much better picks.
	"""
	if not llm_enabled():
		return None
	if not candidates:
		return None

	ctx_year = None
	ctx_topic = None
	ctx_entities: list[str] = []
	ctx_notes = ""
	if isinstance(context, dict):
		ctx_year = context.get("year")
		ctx_topic = context.get("topic")
		ents = context.get("entities")
		if isinstance(ents, list):
			ctx_entities = [str(x).strip() for x in ents if str(x).strip()][:10]
		ctx_notes = str(context.get("notes") or "").strip()

	# Keep payload small: only send essentials.
	packed = []
	for i, c in enumerate(candidates, start=1):
		packed.append({
			"id": i,
			"url": str(c.get("url") or ""),
			"title": _safe_snip(str(c.get("title") or ""), 160),
			"channel": _safe_snip(str(c.get("channel") or ""), 60),
			"upload_date": str(c.get("upload_date") or ""),
			"duration_sec": c.get("duration_sec"),
			"resolution": str(c.get("resolution") or ""),
		})

	ctx_block = {
		"topic": ctx_topic,
		"year": ctx_year,
		"entities": ctx_entities,
		"notes": ctx_notes,
	}

	prompt = (
		"You are a news footage selector. Your goal is to pick ORIGINAL, timely footage that matches the paragraph. "
		"You must avoid old compilations/recaps unless the paragraph is explicitly historical. "
		"You must strongly prefer uploads close to the inferred year.\n\n"
		"Return ONLY a JSON array. Each array item must be: {id: <number>, score: <0-100>, reason: <short string>}.\n\n"
		"Scoring guide:\n"
		"- +40: Directly matches described event/location/person/action\n"
		"- +30: Timely upload date (close to context year)\n"
		"- +20: Sounds like raw/original footage (CCTV, drone footage, on-the-ground)\n"
		"- -40: Looks like compilation/analysis/documentary/history\n"
		"- -30: Upload year far from context year\n\n"
		"GLOBAL CONTEXT (from full script):\n"
		+ json.dumps(ctx_block, ensure_ascii=False)
		+ "\n\nPARAGRAPH:\n"
		+ (paragraph or "").strip()
		+ "\n\nCANDIDATES:\n"
		+ json.dumps(packed, ensure_ascii=False)
	)

	cmd = ["ollama", "run", model, prompt]
	try:
		p = cool_run(cmd, 120)
	except Exception:
		return None
	if p.returncode != 0:
		return None

	out = (p.stdout or "").strip()
	arr = _extract_json_array(out)
	if not arr:
		return None

	# Map scores back
	score_map: dict[int, dict] = {}
	for it in arr:
		try:
			if not isinstance(it, dict):
				continue
			_id = int(it.get("id"))
			sc = float(it.get("score"))
			reason = str(it.get("reason") or "").strip()
			score_map[_id] = {"score": sc, "reason": reason}
		except Exception:
			continue

	rescored = []
	for i, c in enumerate(candidates, start=1):
		meta = score_map.get(i)
		if not meta:
			continue
		c2 = dict(c)
		c2["score"] = meta.get("score")
		c2["reason"] = meta.get("reason")
		rescored.append(c2)

	rescored.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
	return rescored


def editorial_judge_with_ollama(
	paragraph: str,
	candidate: dict,
	context: dict | None,
	model: str,
) -> dict | None:
	"""Final suitability check before downloading.

	Uses description + subtitles (when available) to decide if the video truly matches.
	Returns: {"ok": bool, "score": 0-100, "why": str, "cut_suggestions": [..], "avoid": [..]}
	"""
	if not llm_enabled():
		return None

	ctx_block = {
		"topic": (context.get("topic") if isinstance(context, dict) else None),
		"year": (context.get("year") if isinstance(context, dict) else None),
		"entities": (context.get("entities") if isinstance(context, dict) else []),
	}

	# Truncate big fields
	desc = _safe_snip(str(candidate.get("description") or ""), 1200)
	subs = str(candidate.get("subs_text") or "")
	if len(subs) > DEFAULT_SUBTITLE_CHAR_LIMIT:
		subs = subs[: DEFAULT_SUBTITLE_CHAR_LIMIT] + "..."

	prompt = (
		"You are a master Video Editor assistant. Decide if a candidate video is suitable as B-roll/footage to illustrate ONE paragraph of a documentary script. "
		"Be smart: accept the video if it provides EXCELLENT Contextual B-roll for abstract concepts (e.g., accepting 'Moscow supermarket prices' for a paragraph about inflation, or 'Central Bank building' for a paragraph about interest rates).\n"
		"Reject if it is completely unrelated to the core topic, or vastly out of the context timeline (unless historical).\n"
		"News commentary or compilations CAN be accepted if they contain relevant visual footage/B-roll that matches the script.\n\n"
		"Return ONLY one JSON object with keys: ok (boolean), score (0-100), why (string), cut_suggestions (array of 2-5 short strings), avoid (array of 1-4 short strings).\n\n"
		"GLOBAL CONTEXT:\n" + json.dumps(ctx_block, ensure_ascii=False) + "\n\n"
		"PARAGRAPH:\n" + (paragraph or "").strip() + "\n\n"
		"VIDEO METADATA:\n" + json.dumps({
			"title": candidate.get("title"),
			"channel": candidate.get("channel"),
			"upload_date": candidate.get("upload_date"),
			"duration_sec": candidate.get("duration_sec"),
			"description": desc,
		}, ensure_ascii=False) + "\n\n"
		"SUBTITLES (if any):\n" + (subs or "")
	)

	cmd = ["ollama", "run", model, prompt]
	try:
		p = cool_run(cmd, 120)
	except Exception:
		return None
	if p.returncode != 0:
		return None
	out = (p.stdout or "").strip()
	obj = _extract_json_obj(out)
	if not obj:
		return None
	return obj


def build_queries_with_cloud(paragraph: str, context: dict | None = None) -> list[str] | None:
	if not llm_enabled():
		return None

	ctx_year = None
	ctx_topic = None
	ctx_entities: list[str] = []
	if isinstance(context, dict):
		ctx_year = context.get("year")
		ctx_topic = context.get("topic")
		ents = context.get("entities")
		if isinstance(ents, list):
			ctx_entities = [str(x).strip() for x in ents if str(x).strip()][:8]

	ctx_line = ""
	if ctx_year or ctx_topic or ctx_entities:
		ctx_line = (
			"GLOBAL CONTEXT (from full script): "
			+ (f"topic={ctx_topic}; " if ctx_topic else "")
			+ (f"year~{ctx_year}; " if ctx_year else "")
			+ ("entities=" + ", ".join(ctx_entities) + "; " if ctx_entities else "")
			+ "Prefer timely footage consistent with the inferred year; avoid old compilations unless explicitly historical.\n\n"
		)

	year_str = str(ctx_year) if ctx_year else ""

	# Advanced high-level algorithm prompt for YouTube/Tavily queries
	prompt = (
		"You are a master OSINT footage researcher. The source script is in Vietnamese, "
		"but you must NEVER search in Vietnamese. Given ONE paragraph + global context, "
		"extract the MOST CRITICAL core searchable facts (straight to the point) and produce "
		"HIGH-PRECISION YouTube video search queries in the languages of the COUNTRIES involved in the event.\n\n"
		"LANGUAGE POLICY (CRITICAL):\n"
		"- ALWAYS include English ('en') queries.\n"
		"- Ukraine involved -> add Ukrainian ('uk', Cyrillic).\n"
		"- Russia involved -> add Russian ('ru', Cyrillic).\n"
		"- China involved -> add Chinese ('zh', Hanzi) and/or English.\n"
		"- Any other country -> use that country's primary language in its native script.\n"
		"- NEVER output Vietnamese under any circumstances.\n\n"
		"Return ONLY one JSON object with this exact shape:\n"
		'{\n'
		'  "entities": {"key_visuals": [], "abstract_concepts": [], "people": [], "places": []},\n'
		'  "countries": [],\n'
		'  "event": "<short English description of the key visual event>",\n'
		'  "date_hint": "<e.g. June 2026 or empty>",\n'
		'  "queries": {"en": [], "uk": [], "ru": [], "zh": []},\n'
		'  "fallback_queries": ["<broader strategic or B-roll query 1>", "<broader query 2>"]\n'
		'}\n\n'
		"Rules for queries:\n"
		"- IGNORE rhetorical/intro/filler sentences. NEVER use the opening words of the paragraph as a query.\n"
		"- Think like a Video Editor: If the paragraph is about abstract concepts (economy, politics, GDP, inflation, demographics), generate Contextual B-roll keywords (e.g. 'Moscow supermarket prices', 'Russian Central Bank Elvira Nabiullina', 'Saint Petersburg economic forum 2026', 'Russian empty storefronts', 'Moscow migrant workers').\n"
		"- Each query must be concise. Combine entity + visual action/topic + (optional) date.\n"
		"- Use LOCAL/NATIVE spellings in each language (en: Kinburn Spit, Kherson, Crimea; uk/ru: Cyrillic; zh: Hanzi).\n"
		"- Provide 2-4 'en' queries; 1-2 per other relevant language. Leave a language as [] if that country is NOT involved.\n"
		"- Prefer words that surface ORIGINAL footage or B-roll: 'drone footage', 'raw', 'b-roll', 'news', 'кадри', 'відео', 'кадры', 'видео', 'дрон', '无人机', '画面', '实拍', 'cityscape', 'street view'.\n"
		"- Add the year when it improves recency.\n"
		"- Do NOT invent sources not in the paragraph.\n"
		"- If the paragraph is purely historical, keep queries historical instead.\n"
		"- FALLBACK: Provide 2-3 broader `fallback_queries` capturing the wider context in case specific events yield no results.\n\n"
		+ (f"INFERRED YEAR: {year_str}\n" if year_str else "")
		+ ctx_line
		+ "Paragraph:\n" + (paragraph or "").strip()
	)

	try:
		res_str = cloud_generate(prompt, model=CLOUD.get("model", ""), timeout=60)
		if not res_str:
			print(f"[DEBUG] cloud_generate returned None for paragraph: {paragraph[:50]}")
			return None
			
		obj = _extract_json_obj(res_str)
		if not obj:
			print(f"[DEBUG] _extract_json_obj failed. res_str: {res_str[:200]}")
			arr = _extract_json_array(res_str)
			if arr:
				return [str(x).strip() for x in arr if str(x).strip()][:DEFAULT_MAX_QUERIES]
			return None
		else:
			print(f"[DEBUG] _extract_json_obj SUCCESS! queries: {obj.get('queries')}")

		queries: list[str] = []
		q = obj.get("queries")
		if isinstance(q, dict):
			lang_order = ["en"] + [k for k in q.keys() if k not in ("en", "vi")]
			buckets = []
			for lang in lang_order:
				vals = q.get(lang)
				if isinstance(vals, list):
					buckets.append([str(x).strip() for x in vals if str(x).strip()])
				else:
					buckets.append([])
			maxlen = max((len(b) for b in buckets), default=0)
			for i in range(maxlen):
				for b in buckets:
					if i < len(b):
						queries.append(b[i])
		elif isinstance(q, list):
			queries = [str(x).strip() for x in q if str(x).strip()]

		# Append fallback queries
		fb = obj.get("fallback_queries")
		if isinstance(fb, list):
			for item in fb:
				item_str = str(item).strip()
				if item_str:
					queries.append(item_str)
		elif isinstance(fb, str):
			if str(fb).strip():
				queries.append(str(fb).strip())

		queries = drop_vietnamese_queries(queries)
		seen = set()
		out_qs = []
		for x in queries:
			k = x.lower().strip()
			if k and k not in seen:
				seen.add(k)
				out_qs.append(x)
		if not out_qs:
			return None
		return out_qs[:DEFAULT_MAX_QUERIES]
	except Exception:
		return None


def build_queries_rules(paragraph: str) -> list[str]:
	"""Build SPECIFIC English queries by reading the paragraph's real entities
	(road numbers, places, units, people, weapons) — never the intro words."""
	terms = extract_key_terms(paragraph)
	tl = normalize_script(paragraph or "").lower()
	roads = terms["roads"]
	people = terms["people"]
	units = terms["units"]
	weapons = terms["weapons"]
	actions = terms["actions"]
	specific_places = [p for p in terms["places"] if p not in ("Russia", "Ukraine", "China")]

	# Visual action word
	action = None
	for a in ["attack", "strike", "airstrike", "shelling", "explosion", "artillery", "clash", "advance", "shoot down", "intercept"]:
		if a in actions:
			action = a
			break
	has_drone = ("drone" in tl or "fpv" in tl or any(w.lower() in ("drone", "fpv") for w in weapons))
	if not action:
		action = "strike" if (weapons or has_drone) else "footage"
	wword = "drone" if has_drone else (weapons[0] if weapons else "")

	qs: list[str] = []
	# 1) road-focused (e.g. "M14 highway Kherson drone strike")
	if roads:
		loc = specific_places[0] if specific_places else "Ukraine"
		qs.append(" ".join(x for x in [roads[0], "highway", loc, wword, action] if x))
		if len(roads) >= 2:
			loc2 = specific_places[1] if len(specific_places) >= 2 else loc
			qs.append(" ".join(x for x in [roads[1], "road", loc2, wword, action] if x))
	# 2) multi-place frontline
	if len(specific_places) >= 2:
		act_word = action if action != "footage" else ""
		qs.append(" ".join(x for x in [specific_places[0], specific_places[1], wword, act_word, "footage"] if x))
	elif specific_places:
		act_word = action if action != "footage" else ""
		qs.append(" ".join(x for x in [specific_places[0], wword, act_word, "footage"] if x))
	# 3) people / units
	if people:
		qs.append(f"{people[0]} statement")
	elif units:
		loc = specific_places[0] if specific_places else "Ukraine"
		qs.append(" ".join(x for x in [units[0], loc, "footage"] if x))
	# weapon-only fallback
	if not qs and weapons:
		qs.append(" ".join(x for x in [weapons[0], "Ukraine Russia war footage"] if x))
		if len(weapons) >= 2:
			qs.append(" ".join(x for x in [weapons[1], "Ukraine Russia war footage"] if x))
	if not qs:
		qs.append("Ukraine Russia war footage")

	# Dedup + never-Vietnamese + cap 4
	out: list[str] = []
	seen = set()
	for q in qs:
		k = q.lower().strip()
		if k and k not in seen:
			seen.add(k)
			out.append(q.strip())
	out = drop_vietnamese_queries(out)
	if not out:
		out = ["Ukraine Russia war footage"]
	return out[:4]


# ----------------------------
# YouTube helpers
# ----------------------------

def best_format_selector():
	# Prefer H.264 (avc1) at 1080/720, fallback >=720.
	return (
		"bestvideo[vcodec*=avc1][height<=1080][height>=720]+bestaudio/"
		"bestvideo[height<=1080][height>=720]+bestaudio/"
		"best[height<=1080][height>=720]"
	)


TAVILY_ENDPOINT = "https://api.tavily.com/search"


def canonical_video_url(url: str) -> str:
	"""Return a canonical single-video watch URL for supported platforms, or '' if unrecognized."""
	if not url:
		return ""
	
	# YouTube (Case sensitive ID!)
	m_yt = re.search(
		r"(?:youtube\.com/watch\?[^ ]*?v=|youtu\.be/|youtube\.com/shorts/|youtube\.com/embed/)([A-Za-z0-9_-]{11})",
		url,
		flags=re.IGNORECASE
	)
	if m_yt:
		return "https://www.youtube.com/watch?v=" + m_yt.group(1)
		
	# Other platforms (Case insensitive domains)
	url_lower = url.lower()
	
	# Twitter / X
	m_tw = re.search(r"(?:twitter\.com|x\.com)/(?:i|[^/]+)/status/(\d+)", url_lower)
	if m_tw:
		return f"https://x.com/i/status/{m_tw.group(1)}"
		
	# Telegram
	m_tg = re.search(r"t\.me/([^/]+)/(\d+)", url_lower)
	if m_tg:
		return f"https://t.me/{m_tg.group(1)}/{m_tg.group(2)}"
		
	# Facebook
	m_fb_watch = re.search(r"facebook\.com/watch/\?v=(\d+)", url_lower)
	if m_fb_watch:
		return f"https://www.facebook.com/watch/?v={m_fb_watch.group(1)}"
	m_fb_video = re.search(r"facebook\.com/[^/]+/videos/(\d+)", url_lower)
	if m_fb_video:
		return f"https://www.facebook.com/watch/?v={m_fb_video.group(1)}"
	m_fb_reel = re.search(r"facebook\.com/reel/(\d+)", url_lower)
	if m_fb_reel:
		return f"https://www.facebook.com/reel/{m_fb_reel.group(1)}"
		
	# VK
	m_vk = re.search(r"vk\.com/video(-?\d+_\d+)", url_lower)
	if m_vk:
		return f"https://vk.com/video{m_vk.group(1)}"
		
	# TikTok
	m_tk = re.search(r"tiktok\.com/@[^/]+/video/(\d+)", url_lower)
	if m_tk:
		return url.split("?")[0] # Just strip query parameters
		
	return ""


def title_fingerprint(title: str) -> str:
	"""Normalized title key to catch the SAME clip re-uploaded under another URL.
	Conservative: lowercase + strip punctuation + collapse spaces (no word removal),
	so only near-identical titles collide."""
	t = (title or "").lower().strip()
	t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
	t = re.sub(r"\s+", " ", t).strip()
	return t


def _tavily_request(api_key: str, query: str, max_results: int, web_search: bool = False):
	"""Single Tavily call. Returns (status, payload); status == 'ok' on success."""
	body = {
		"query": query,
		"search_depth": "basic",
		"max_results": int(max_results),
		"topic": "general",
	}
	if not web_search:
		body["include_domains"] = ["youtube.com", "youtu.be", "twitter.com", "x.com", "t.me", "facebook.com", "fb.watch", "vk.com", "tiktok.com"]
	data = json.dumps(body).encode("utf-8")
	req = urllib.request.Request(
		TAVILY_ENDPOINT,
		data=data,
		headers={
			"Content-Type": "application/json",
			"Authorization": f"Bearer {api_key}",
		},
		method="POST",
	)
	try:
		with urllib.request.urlopen(req, timeout=30) as resp:
			raw = resp.read().decode("utf-8", "replace")
			return ("ok", json.loads(raw))
	except urllib.error.HTTPError as e:
		try:
			msg = e.read().decode("utf-8", "replace")
		except Exception:
			msg = str(e)
		return (f"http_{e.code}", msg)
	except Exception as e:
		return ("error", str(e))


def reset_tavily_state(state: dict | None = None):
	"""Reset xoay vòng Tavily — gọi mỗi lần CHẠY để không bị kẹt circuit breaker từ phiên cũ."""
	target = state if state is not None else {}
	target["idx"] = 0
	target.pop("dead_keys", None)
	target.pop("circuit_open", None)


def tavily_search_rotate(query: str, max_results: int, keys: list, state: dict, logf=None, web_search: bool = False) -> list | None:
	"""Search Tavily, rotating across API keys when one is rate/quota limited.

	keys: list of API keys (one per line in the UI).
	state: dict with 'idx' to remember the current working key across calls.
	Returns a list of result dicts (url/title/content/...) or None.
	"""
	if not keys:
		return None
	if state.get("circuit_open"):
		if logf and not state.get("_circuit_logged"):
			logf("  [TAVILY] ⚠ Circuit breaker đang chặn — không gọi API (reset khi bấm CHẠY lại).")
			state["_circuit_logged"] = True
		return None
	dead = set(state.get("dead_keys") or [])
	n = len(keys)
	start = int(state.get("idx", 0)) % n
	tried = 0
	while tried < n:
		ki = (start + tried) % n
		if ki in dead:
			tried += 1
			continue
		key = keys[ki]
		status, payload = _tavily_request(key, query, max_results, web_search=web_search)
		if status == "ok":
			state["idx"] = ki
			if isinstance(payload, dict):
				res = payload.get("results")
				return res if isinstance(res, list) else []
			return []
		low = str(payload).lower()
		rotate = False
		if status.startswith("http_"):
			code = status.split("_", 1)[1]
			if code in {"401", "402", "403", "429", "432", "433"} or code.startswith("5"):
				rotate = True
		if any(w in low for w in ("limit", "quota", "credit", "usage", "exceeded", "unauthorized", "forbidden", "too many")):
			rotate = True
		if rotate:
			dead.add(ki)
			state["dead_keys"] = sorted(dead)
		if logf and tried == 0:
			logf(f"  [TAVILY] key #{ki + 1}/{n} lỗi ({status}) — {'xoay sang key khác' if rotate else 'bỏ qua'}")
		if not rotate:
			return None
		tried += 1
	if logf and len(dead) < n:
		logf("  [TAVILY] tất cả key đều hết quota/không dùng được.")
	if len(dead) >= n:
		state["circuit_open"] = True
	return None


URL_IN_TEXT_RE = re.compile(
	r"(?:https?://|www\.)[^\s<>\"'\]\)]+",
	re.IGNORECASE,
)

VIDEO_CONTENT_HOST_RE = re.compile(
	r"(?:"
	r"youtube\.com|youtu\.be|youtube-nocookie\.com|"
	r"(?:twitter|x)\.com|"
	r"tiktok\.com|vm\.tiktok\.com|"
	r"bilibili\.com|b23\.tv|"
	r"douyin\.com|iesdouyin\.com|"
	r"instagram\.com|"
	r"(?:facebook|fb)\.com/(?:watch|reel|videos?)|"
	r"vimeo\.com|dailymotion\.com|twitch\.tv|"
	r"vk\.com/video|"
	r"t\.me/"
	r")",
	re.IGNORECASE,
)


def normalize_script_url(raw: str) -> str:
	u = (raw or "").strip().rstrip(".,;:!?)\"'")
	if u.lower().startswith("www."):
		u = "https://" + u
	return u


def is_video_content_url(url: str) -> bool:
	u = (url or "").strip().lower()
	if not u:
		return False
	if VIDEO_CONTENT_HOST_RE.search(u):
		return True
	if re.search(r"\.(?:mp4|webm|mov|m3u8)(?:\?|$)", u):
		return True
	return False


def extract_article_urls(paragraph: str) -> list[str]:
	"""Extract news/article URLs in a paragraph; ignore video-platform links."""
	seen: set[str] = set()
	out: list[str] = []
	for m in URL_IN_TEXT_RE.finditer(paragraph or ""):
		url = normalize_script_url(m.group(0))
		if not url.lower().startswith(("http://", "https://")):
			continue
		if is_video_content_url(url):
			continue
		key = url.rstrip("/")
		if key not in seen:
			seen.add(key)
			out.append(url)
	return out


def detect_newspaper_reference(paragraph: str) -> str | None:
	"""Check if a paragraph mentions a newspaper/outlet.
	If so, return a search query for the article, else None.
	"""
	p = normalize_script(paragraph or "").lower()
	press_indicators = [
		"báo", "tờ báo", "hãng tin", "tạp chí", "trang tin", "tin tức từ",
		"thông tấn", "nhật báo", "đưa tin", "cho biết", "đăng tải", "công bố"
	]
	outlets = [
		"reuters", "ap", "afp", "cnn", "nytimes", "new york times", "wall street journal",
		"wsj", "bloomberg", "the guardian", "telegraph", "tass", "ria novosti", "ria",
		"sputnik", "bbc", "dw", "al jazeera", "wion", "politico", "newsweek", "forbes",
		"vnexpress", "tuổi trẻ", "thanh niên", "dân trí", "vietnamnet"
	]
	
	has_press_word = any(word in p for word in press_indicators)
	found_outlet = None
	for o in outlets:
		if re.search(r"\b" + re.escape(o) + r"\b", p):
			found_outlet = o
			break
			
	if has_press_word or found_outlet:
		terms = extract_key_terms(paragraph)
		outlet_name = found_outlet.upper() if found_outlet else "news article"
		query_parts = [outlet_name]
		
		specific_places = [pt for pt in terms["places"] if pt not in ("Russia", "Ukraine", "China")]
		if specific_places:
			query_parts.append(specific_places[0])
			
		if terms["people"]:
			query_parts.append(terms["people"][0])
		elif terms["units"]:
			query_parts.append(terms["units"][0])
		elif terms["weapons"]:
			query_parts.append(terms["weapons"][0])
			
		if terms["actions"]:
			query_parts.append(terms["actions"][0])
			
		if len(query_parts) <= 2:
			clean_p = re.sub(r'[^\w\s]', ' ', p)
			words = [w for w in clean_p.split() if w and w not in ["theo", "báo", "tờ", "hãng", "tin", "cho", "biết"]][:6]
			query_parts.extend(words)
			
		seen = set()
		final_parts = []
		for part in query_parts:
			part_clean = part.strip().lower()
			if part_clean and part_clean not in seen:
				seen.add(part_clean)
				final_parts.append(part.strip())
				
		return " ".join(final_parts)
	return None


def search_article(query: str, keys: list, state: dict, logf=None) -> tuple[str, str]:
	"""Search Tavily for a news article. Fallback to Google Search URL if Tavily is unavailable."""
	if keys:
		try:
			res = tavily_search_rotate(query, 3, keys, state, logf=logf, web_search=True)
			if res and isinstance(res, list) and len(res) > 0:
				return (res[0].get("title", "Article Link"), res[0].get("url", ""))
		except Exception as e:
			if logf:
				logf(f"  [TAVILY] article search error: {e}")
	
	gurl = "https://www.google.com/search?q=" + urllib.parse.quote(query)
	return (f"Google Search: {query}", gurl)


def run_bilibili_search(query: str, limit: int):
	import urllib.request
	import urllib.parse
	import json
	req = urllib.request.Request(f"https://api.bilibili.com/x/web-interface/search/all/v2?keyword={urllib.parse.quote(query)}", headers={"User-Agent": "Mozilla/5.0"})
	try:
		with urllib.request.urlopen(req) as resp:
			data = json.loads(resp.read().decode('utf-8'))
			results = []
			for result in data.get('data', {}).get('result', []):
				if result.get('result_type') == 'video':
					for v in result.get('data', []):
						if len(results) >= limit:
							break
						results.append({
							"webpage_url": f"https://www.bilibili.com/video/{v.get('bvid')}",
							"title": v.get('title', '').replace('<em class="keyword">', '').replace('</em>', ''),
							"duration": v.get('duration', ''), # e.g. "3:45"
							"uploader": v.get('author', ''),
							"upload_date": str(v.get('pubdate', '')), # unix timestamp
						})
			return {"entries": results}
	except Exception as e:
		print(f"Bilibili search error: {e}")
		return {"entries": []}


def run_yt_dlp_search(query: str, limit: int, cookies_browser: str, lang: str | None = None):
	# YouTube returns localized (Vietnamese) results for a VN account/region.
	# Force interface language (hl) + region (gl) so foreign-language footage surfaces.
	if not lang:
		lang = detect_query_lang(query)
	hl, gl = hl_gl_for_lang(lang)

	search_expr = f"ytsearch{limit}:{query}"
	cmd = [
		"yt-dlp",
		"--no-warnings",
		"--quiet",
		"--dump-single-json",
		# Force YouTube UI language + region so results are NOT localized to Vietnam.
		"--extractor-args", f"youtube:lang={hl}",
		"--geo-bypass-country", gl,
		search_expr,
	]
	# IMPORTANT: do NOT attach the logged-in (VN) cookies during search — they
	# personalize results back to Vietnamese. Cookies are still used for download.
	if cookies_browser and DEFAULT_SEARCH_USE_COOKIES:
		cmd += ["--cookies-from-browser", cookies_browser]
		
	with GLOBAL_YTDLP_SEM:
		time.sleep(random.uniform(1.0, 2.5))
		p = cool_run(cmd, 90)
		
	if p.returncode != 0:
		raise RuntimeError((p.stderr or "").strip() or "yt-dlp failed")
	return json.loads(p.stdout)


def run_yt_dlp_info(url: str, cookies_browser: str) -> dict:
	"""Fetch full metadata JSON for a single video URL (no download)."""
	cmd = [
		"yt-dlp",
		"--no-warnings",
		"--quiet",
		"--dump-single-json",
		url,
	]
	if cookies_browser:
		cmd += ["--cookies-from-browser", cookies_browser]
		
	with GLOBAL_YTDLP_SEM:
		time.sleep(random.uniform(1.0, 2.5))
		p = cool_run(cmd, 120)
		
	if p.returncode != 0:
		raise RuntimeError((p.stderr or "").strip() or "yt-dlp info failed")
	return json.loads(p.stdout)


def _vtt_to_text(vtt: str) -> str:
	"""Very small VTT -> plain text converter (good enough for LLM judging)."""
	if not vtt:
		return ""
	lines = []
	for ln in vtt.splitlines():
		ln = ln.strip("\ufeff").strip()
		if not ln:
			continue
		if ln.upper().startswith("WEBVTT"):
			continue
		if "-->" in ln:
			continue
		if re.match(r"^\d+$", ln):
			continue
		ln = re.sub(r"<[^>]+>", "", ln)
		lines.append(ln)
	text = " ".join(lines)
	text = re.sub(r"\s+", " ", text).strip()
	return text


def fetch_best_subtitle_text(url: str, cookies_browser: str) -> str:
	"""Fetch auto subs/captions for judging WITHOUT downloading the video."""
	try:
		tmp = SETTINGS_DIR / "_tmp_subs"
		tmp.mkdir(parents=True, exist_ok=True)
		for fp in tmp.glob("sub*.*"):
			try:
				fp.unlink()
			except Exception:
				pass

		cmd = [
			"yt-dlp",
			"--no-warnings",
			"--quiet",
			"--skip-download",
			"--write-auto-subs",
			"--write-subs",
			"--sub-format", "vtt",
			"--sub-langs", "en.*,en,vi.*,vi,und",
			"-o", str(tmp / "sub.%(language)s.%(ext)s"),
			url,
		]
		if cookies_browser:
			cmd += ["--cookies-from-browser", cookies_browser]
			
		with GLOBAL_YTDLP_SEM:
			time.sleep(random.uniform(1.0, 2.5))
			_ = cool_run(cmd, 120)

		vtts = list(tmp.glob("sub.*.vtt"))
		if not vtts:
			# fallback: sometimes yt-dlp uses different names
			vtts = list(tmp.glob("sub*.vtt"))
		if not vtts:
			return ""

		def _prio(p: Path):
			n = p.name.lower()
			return (
				0 if (".en." in n or n.startswith("sub.en")) else 1,
				0 if "auto" in n else 1,
				n,
			)
		vtts.sort(key=_prio)
		content = vtts[0].read_text(encoding="utf-8", errors="ignore")
		return _vtt_to_text(content)
	except Exception:
		return ""
import urllib.request
import zipfile
import platform

def ensure_bbdown():
	bin_dir = Path.home() / ".ob_newsvideo_bin"
	bin_dir.mkdir(parents=True, exist_ok=True)
	bbdown_path = bin_dir / "BBDown"
	if bbdown_path.exists():
		return bbdown_path

	arch = platform.machine().lower()
	if "arm" in arch or "aarch64" in arch:
		url = "https://github.com/nilaoda/BBDown/releases/download/1.6.3/BBDown_1.6.3_20240814_osx-arm64.zip"
	else:
		url = "https://github.com/nilaoda/BBDown/releases/download/1.6.3/BBDown_1.6.3_20240814_osx-x64.zip"
	
	zip_path = bin_dir / "bbdown.zip"
	print(f"Downloading BBDown from {url}")
	try:
		urllib.request.urlretrieve(url, zip_path)
		with zipfile.ZipFile(zip_path, 'r') as zip_ref:
			zip_ref.extractall(bin_dir)
		zip_path.unlink()
		bbdown_path.chmod(0o755)
	except Exception as e:
		print(f"Failed to download BBDown: {e}")
		return None
	return bbdown_path


def _run_bbdown_download(url, out_dir, cookies_browser, timeout_sec):
	bbdown = ensure_bbdown()
	if not bbdown:
		return 1, "Failed to prepare BBDown (Bilibili downloader)."
	
	import shutil
	cmd = [
		str(bbdown), url,
		"--work-dir", str(out_dir),
		"-mt"
	]
	cmd.append("--use-app-api")
	cmd.append("-e")
	cmd.append("hevc") # Force HEVC video
	
	ffmpeg_path = shutil.which("ffmpeg")
	if ffmpeg_path:
		cmd.append("--ffmpeg-path")
		cmd.append(ffmpeg_path)
	
	try:
		proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
		out, err = proc.communicate(timeout=timeout_sec)
		return proc.returncode, ((out or "") + "\n" + (err or ""))
	except subprocess.TimeoutExpired:
		try: proc.kill()
		except: pass
		return 124, "Timeout"


def _run_yt_dlp_download(url, out_dir, cookies_browser, conc, human_mode, fmt, timeout_sec, extra=None):
	"""Run ONE yt-dlp download attempt.
	Never raises: on a stall the process is killed and a synthetic code 124 is
	returned, so a single bad video can be skipped instead of crashing the run."""
	cmd = [
		"yt-dlp",
		"-f", fmt,
		"--merge-output-format", "mp4",
		"--concurrent-fragments", str(max(1, conc)),
		# Robustness: give up on dead sockets fast and retry fragments instead of
		# hanging for the full wall-clock timeout.
		"--socket-timeout", "30",
		"--retries", "5",
		"--fragment-retries", "20",
		"--retry-sleep", "3",
		"--no-playlist",
		"--force-ipv4",
		"--no-progress",
		"-o", str(out_dir / "%(id)s.%(ext)s"),
	]
	if extra:
		cmd += list(extra)
	if human_mode:
		cmd += ["--sleep-interval", "2", "--max-sleep-interval", "6"]
	if cookies_browser:
		cmd += ["--cookies-from-browser", cookies_browser]
	cmd.append(url)
	# Cool mode: run the CPU-heavy download+merge at lower priority.
	if COOL.get("on") and os.name == "posix" and shutil.which("nice"):
		cmd = ["nice", "-n", str(COOL.get("nice", 10))] + cmd
	try:
		proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
	except Exception as e:
		return 1, f"spawn failed: {e}"
	try:
		out, err = proc.communicate(timeout=timeout_sec)
		return proc.returncode, ((out or "") + "\n" + (err or ""))
	except subprocess.TimeoutExpired:
		# Kill the stalled download (and let any child ffmpeg die with it).
		try:
			proc.kill()
		except Exception:
			pass
		try:
			out, err = proc.communicate(timeout=15)
		except Exception:
			out, err = "", ""
		return 124, ((out or "") + "\n" + (err or "") + f"\n[TIMEOUT] killed after {timeout_sec}s")


def download_one(url: str, out_dir: Path, cookies_browser: str, threads: int, human_mode: bool):
	"""Robust single-video download.
	- Caps fragment concurrency (high values like 20 often CAUSE the YouTube
	  throttling/stall that leads to a timeout).
	- Uses a soft wall-clock timeout that NEVER crashes the whole run.
	- On timeout/failure, retries once with a lighter format + low concurrency.
	"""
	if "bilibili.com" in url or "b23.tv" in url:
		code, out = _run_bbdown_download(url, out_dir, cookies_browser, 600)
		return code, out

	# Too many parallel fragments is a common cause of stalls AND heat.
	cap = max(32, int(threads or 8))
	conc = max(1, min(int(threads or 1), cap))
	# Attempt 1: preferred AVC 720-1080.
	code, out = _run_yt_dlp_download(
		url, out_dir, cookies_browser, conc, human_mode,
		fmt=best_format_selector(), timeout_sec=600,
	)
	if code == 0:
		return code, out
	# Attempt 2 (fallback): any format up to 1080p, low concurrency, geo-bypass.
	# Recovers videos that stalled under high concurrency or that lack an AVC track.
	code2, out2 = _run_yt_dlp_download(
		url, out_dir, cookies_browser, conc=max(1, min(conc, 4)), human_mode=human_mode,
		fmt="best[height<=1080]/best", timeout_sec=480,
		extra=["--geo-bypass"],
	)
	if code2 == 0:
		return code2, out2

	if "x.com" in url or "twitter.com" in url:
		return code2, (out + "\n--- RETRY ---\n" + out2 + "\n\n[TWITTER ERROR] X.com/Twitter chặn tool tải ngoài. Hãy đảm bảo bạn đã chọn đúng trình duyệt (Chrome/Edge/Safari) đã đăng nhập Twitter ở giao diện, HOẶC thử dùng website bên thứ 3 như snaptwitter.com!")

	return code2, (out + "\n--- RETRY ---\n" + out2)


# ----------------------------
# Naming
# ----------------------------

def format_ddmmyy(yyyymmdd: str) -> str:
	"""YYYYMMDD → DD-MM-YY (8 chars), e.g. 20260701 → 01-07-26."""
	if not yyyymmdd or len(yyyymmdd) != 8:
		return ""
	return f"{yyyymmdd[6:8]}-{yyyymmdd[4:6]}-{yyyymmdd[2:4]}"


def safe_filename(s: str, max_len: int = 140) -> str:
	s = (s or "").strip()
	s = re.sub(r"[\\/:*?\"<>|]", "-", s)
	s = re.sub(r"\s+", " ", s)
	return s[:max_len]


def build_video_filename_stem(paragraph_index: int, translated_title: str, upload_date: str) -> str:
	"""ĐOẠN X - {title} - DD-MM-YY — max 90 chars; date (8 chars) always kept."""
	prefix = f"ĐOẠN {paragraph_index} - "
	date = format_ddmmyy(upload_date)
	date_part = f" - {date}" if date else ""
	max_len = MAX_VIDEO_FILENAME_LEN

	title = (translated_title or "Video").strip()
	title = re.sub(r"[\\/:*?\"<>|]", "-", title)
	title = re.sub(r"\s+", " ", title)

	available = max_len - len(prefix) - len(date_part)
	if available < 1:
		stem = prefix.rstrip(" -")
		if date_part:
			stem = stem[: max_len - len(date_part)] + date_part
	else:
		if len(title) > available:
			title = title[:available].rstrip(" -")
		stem = f"{prefix}{title}{date_part}"

	return safe_filename(stem, max_len=max_len)


def suggest_project_subfolder_name(script: str) -> str:
	now = datetime.datetime.now().strftime("%-d-%-m-%Y-%Hh%M")
	script_preview = ""
	if script:
		first_line = script.splitlines()[0].strip()
		clean_title = re.sub(r"[^\w\s-]", "", first_line)
		words = clean_title.split()[:5]
		if words:
			script_preview = "_" + "-".join(words)
	return f"{now}{script_preview}"


def sanitize_folder_name(name: str) -> str:
	s = (name or "").strip()
	s = re.sub(r"[\\/:*?\"<>|]", "-", s)
	s = re.sub(r"\s+", " ", s)
	return s.strip(" .") or "project"


def ask_project_subfolder_dialog(parent: tk.Misc, default_name: str) -> str | None:
	result: dict[str, str | None] = {"value": None}
	top = tk.Toplevel(parent)
	top.title("Tên thư mục dự án")
	top.geometry("460x130")
	top.resizable(False, False)
	top.transient(parent)
	top.grab_set()

	frame = ttk.Frame(top, padding=12)
	frame.pack(fill="both", expand=True)

	ttk.Label(frame, text="Đặt tên thư mục lưu footage:").pack(anchor="w")
	name_var = tk.StringVar(value=default_name)
	entry = ttk.Entry(frame, textvariable=name_var, width=52)
	entry.pack(fill="x", pady=(6, 10))
	entry.select_range(0, tk.END)
	entry.focus_set()

	btn_row = ttk.Frame(frame)
	btn_row.pack(fill="x")

	def confirm():
		val = sanitize_folder_name(name_var.get())
		if not val:
			messagebox.showwarning("Thiếu tên", "Vui lòng nhập tên thư mục.", parent=top)
			return
		result["value"] = val
		top.destroy()

	def cancel():
		top.destroy()

	ttk.Button(btn_row, text="Hủy", command=cancel).pack(side="right", padx=(6, 0))
	ttk.Button(btn_row, text="OK", command=confirm, style="Accent.TButton").pack(side="right")
	top.bind("<Return>", lambda _e: confirm())
	top.bind("<Escape>", lambda _e: cancel())

	top.update_idletasks()
	x = parent.winfo_rootx() + (parent.winfo_width() - top.winfo_width()) // 2
	y = parent.winfo_rooty() + (parent.winfo_height() - top.winfo_height()) // 2
	top.geometry(f"+{max(0, x)}+{max(0, y)}")

	parent.wait_window(top)
	return result["value"]


def vi_label_from_text(text: str) -> str | None:
	t = (text or "").lower()
	# Keep it short & useful
	if "drone" in t and ("strike" in t or "attack" in t):
		if "moscow" in t or "moskva" in t:
			return "Drone tấn công Moscow"
		return "Drone tấn công"
	if "zelensky" in t or "zelenskyy" in t:
		if "germany" in t or "berlin" in t:
			return "Zelensky họp tại Đức"
		return "Zelensky"
	if "syrskyi" in t:
		return "Tướng Syrskyi"
	if "putin" in t:
		if "moscow" in t or "moskva" in t:
			return "Putin ở Moscow"
		return "Putin"
	if "map" in t or "frontline" in t:
		return "Bản đồ chiến sự"
	return None


def translate_title_vi(title: str) -> str:
	"""Translate a title to Vietnamese using Google Translate.
	Fallback: return original title.
	"""
	t = (title or "").strip()
	if not t:
		return ""
	try:
		url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=vi&dt=t&q=" + urllib.parse.quote(t)
		req = urllib.request.Request(
			url, 
			headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
		)
		last_err = None
		for attempt in ("verified", "unverified"):
			try:
				if attempt == "unverified":
					ctx = ssl._create_unverified_context()
					resp = urllib.request.urlopen(req, timeout=10, context=ctx)
				else:
					resp = urllib.request.urlopen(req, timeout=10)
				with resp as r:
					data = json.loads(r.read().decode("utf-8"))
					if not data or not isinstance(data, list) or not data[0]:
						return t
					sentences = data[0]
					if not sentences:
						return t
					translated_text = "".join(
						sentence[0] for sentence in sentences
						if isinstance(sentence, (list, tuple)) and sentence and sentence[0]
					)
					return translated_text.strip() or t
			except ssl.SSLError as e:
				last_err = e
				continue
			except Exception as e:
				last_err = e
				break
		if last_err:
			write_log_line(f"[TRANSLATE] Google Translate error: {last_err}")
		return t
	except Exception as e:
		write_log_line(f"[TRANSLATE] Error preparing translate request: {e}")
		return t


@dataclass
class VideoCandidate:
	paragraph_index: int
	paragraph_hint: str
	query: str
	youtube_url: str
	title: str
	channel: str
	upload_date: str
	duration_sec: int
	resolution_hint: str
	platform: str = "youtube"


def platform_bucket(item: dict) -> str:
	"""Map a pool item to a download quota bucket."""
	p = str(item.get("platform") or "youtube")
	if item.get("source") in ("tavily", "llm_web"):
		return "youtube"
	if p in ("youtube", "x", "bilibili", "douyin"):
		return p
	return "youtube"


def compute_source_quotas(max_vids: int, use_x: bool, use_bilibili: bool) -> dict[str, int]:
	"""Split per-paragraph download slots across sources.

	When X is on (no Bilibili): 50/50 YouTube vs X — odd totals give YouTube the extra slot.
	  4 -> 2 YT + 2 X | 5 -> 3 YT + 2 X | 6 -> 3 YT + 3 X
	"""
	n = max(1, int(max_vids))
	if use_x and not use_bilibili:
		x_q = n // 2
		return {"youtube": n - x_q, "x": x_q, "bilibili": 0, "douyin": 0}
	if use_x and use_bilibili:
		china = n // 3
		rem = n - china
		x_q = rem // 2
		return {
			"youtube": rem - x_q,
			"x": x_q,
			"bilibili": (china + 1) // 2,
			"douyin": china // 2,
		}
	if use_bilibili:
		return {
			"youtube": n // 3 + (1 if n % 3 > 0 else 0),
			"x": 0,
			"bilibili": n // 3 + (1 if n % 3 > 1 else 0),
			"douyin": n // 3,
		}
	return {"youtube": n, "x": 0, "bilibili": 0, "douyin": 0}


def quota_allows_pick(bucket: str, picked_counts: dict[str, int], quotas: dict[str, int]) -> bool:
	return int(picked_counts.get(bucket, 0)) < int(quotas.get(bucket, 0))


def extract_video_id(url: str) -> str | None:
	import urllib.parse
	try:
		parsed = urllib.parse.urlparse(url)
		if parsed.hostname in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
			query = urllib.parse.parse_qs(parsed.query)
			v = query.get('v')
			if v:
				return v[0]
		elif parsed.hostname in ('youtu.be',):
			path = parsed.path.strip('/')
			if path:
				return path
	except Exception:
		pass
	m = re.search(r"v=([a-zA-Z0-9_-]{11})", url)
	if m:
		return m.group(1)
	m2 = re.search(r"youtu\.be/([a-zA-Z0-9_-]{11})", url)
	if m2:
		return m2.group(1)
	m3 = re.search(r"(?:twitter\.com|x\.com)/(?:i|[^/]+)/status/(\d+)", url or "", re.I)
	if m3:
		return m3.group(1)
	return None


def newest_mp4(download_dir: Path) -> Path | None:
	newest = None
	newest_mtime = -1
	for fp in download_dir.glob("*.mp4"):
		try:
			mt = fp.stat().st_mtime
		except Exception:
			continue
		if mt > newest_mtime:
			newest_mtime = mt
			newest = fp
	return newest


def rename_downloaded(download_dir: Path, c: VideoCandidate) -> Path | None:
	video_id = extract_video_id(c.youtube_url)
	fp = None
	if video_id:
		expected = download_dir / f"{video_id}.mp4"
		if expected.exists():
			fp = expected
	
	if fp is None:
		fp = newest_mp4(download_dir)
		
	if fp is None:
		return None
	# Max 90 chars; date DD-MM-YY (8 chars) always kept — truncate title if needed.
	translated = c.title
	try:
		translated = translate_title_vi(c.title)
	except Exception:
		translated = c.title
	translated = translated.strip() or c.title.strip() or (c.paragraph_hint or "Video")
	stem = build_video_filename_stem(c.paragraph_index, translated, c.upload_date)
	name = stem + ".mp4"
	target = fp.parent / name
	if target.exists() and target.resolve() != fp.resolve():
		for n in range(2, 999):
			cand = fp.parent / f"{stem} ({n}).mp4"
			if not cand.exists():
				target = cand
				break
	try:
		fp.rename(target)
		return target
	except Exception:
		return fp


# ----------------------------
# UI theme (Albula Pro — bundled for portability)
# ----------------------------

def _resource_path(*parts: str) -> Path:
	base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
	return base.joinpath(*parts)


def load_albula_fonts(root: tk.Misc) -> dict[str, tkfont.Font]:
	font_dir = _resource_path("assets", "fonts", "AlbulaPro")
	specs: dict[str, tuple[str, int]] = {
		"light": ("AlbulaPro-Light.otf", 11),
		"regular": ("AlbulaPro-Regular.otf", 11),
		"medium": ("AlbulaPro-Medium.otf", 11),
		"semibold": ("AlbulaPro-SemiBold.otf", 11),
		"bold": ("AlbulaPro-Bold.otf", 11),
		"title": ("AlbulaPro-Bold.otf", 22),
		"subtitle": ("AlbulaPro-Medium.otf", 11),
		"small": ("AlbulaPro-Regular.otf", 10),
		"compact": ("AlbulaPro-Regular.otf", 9),
		"compact_medium": ("AlbulaPro-Medium.otf", 9),
		"compact_semibold": ("AlbulaPro-SemiBold.otf", 9),
		"pin": ("AlbulaPro-Regular.otf", 14),
		"pin_header": ("AlbulaPro-Bold.otf", 14),
	}
	fonts: dict[str, tkfont.Font] = {}
	for key, (fname, size) in specs.items():
		fpath = font_dir / fname
		weight = "bold" if "bold" in fname.lower() else "normal"
		try:
			if fpath.is_file():
				fonts[key] = tkfont.Font(root=root, file=str(fpath), size=size)
			else:
				fonts[key] = tkfont.Font(root=root, family="Helvetica", size=size, weight=weight)
		except Exception:
			fonts[key] = tkfont.Font(root=root, family="Helvetica", size=size, weight=weight)
	fonts["family"] = fonts["regular"].actual("family")
	return fonts


def apply_professional_theme(root: tk.Tk, style: ttk.Style) -> dict[str, Any]:
	fonts = load_albula_fonts(root)
	colors = {
		"bg": "#090C10",
		"panel": "#0E1319",
		"panel_alt": "#131A22",
		"elevated": "#182030",
		"text": "#E6EDF6",
		"text_muted": "#8B98A8",
		"gold": "#D4A84B",
		"gold_soft": "#B8943F",
		"accent": "#4F8CFF",
		"accent_hover": "#3A73E0",
		"border": "#243040",
		"border_soft": "#1A2433",
		"input_bg": "#0C1118",
		"danger": "#E06B73",
		"danger_hover": "#C2555D",
	}

	root.configure(bg=colors["bg"])
	style.theme_use("clam")

	style.configure(".", background=colors["bg"], foreground=colors["text"], font=fonts["regular"])
	style.map(".", background=[("active", colors["panel_alt"])])

	style.configure("Header.TLabel", background=colors["panel"], foreground=colors["gold"], font=fonts["title"])
	style.configure("Subtitle.TLabel", background=colors["panel"], foreground=colors["text_muted"], font=fonts["subtitle"])
	style.configure("Section.TLabel", background=colors["bg"], foreground=colors["gold"], font=fonts["semibold"])
	style.configure("Muted.TLabel", background=colors["bg"], foreground=colors["text_muted"], font=fonts["small"])
	style.configure("Card.Section.TLabel", background=colors["panel_alt"], foreground=colors["gold"], font=fonts["semibold"])
	style.configure("Card.Muted.TLabel", background=colors["panel_alt"], foreground=colors["text_muted"], font=fonts["small"])

	# Compact sidebar (left settings panel)
	style.configure("Sidebar.TFrame", background=colors["panel_alt"])
	style.configure("Sidebar.TLabel", background=colors["bg"], foreground=colors["text"], font=fonts["compact"])
	style.configure("Sidebar.Muted.TLabel", background=colors["bg"], foreground=colors["text_muted"], font=fonts["compact"])
	style.configure("Sidebar.TCheckbutton", background=colors["bg"], foreground=colors["text"], font=fonts["compact"])
	style.configure("Sidebar.TRadiobutton", background=colors["bg"], foreground=colors["text"], font=fonts["compact"])
	style.configure("Sidebar.Card.TCheckbutton", background=colors["panel_alt"], foreground=colors["text"], font=fonts["compact"])
	style.configure("Sidebar.Card.TRadiobutton", background=colors["panel_alt"], foreground=colors["text"], font=fonts["compact"])
	style.configure("Sidebar.Card.TLabel", background=colors["panel_alt"], foreground=colors["text"], font=fonts["compact"])
	style.map("Sidebar.TCheckbutton", background=[("active", colors["bg"])])
	style.map("Sidebar.TRadiobutton", background=[("active", colors["bg"])])
	style.map("Sidebar.Card.TCheckbutton", background=[("active", colors["panel_alt"])])
	style.map("Sidebar.Card.TRadiobutton", background=[("active", colors["panel_alt"])])
	style.configure("Sidebar.TEntry", fieldbackground=colors["input_bg"], foreground=colors["text"], bordercolor=colors["border"], insertcolor=colors["gold"], padding=4, font=fonts["compact"])
	style.configure("Sidebar.TCombobox", fieldbackground=colors["input_bg"], foreground=colors["text"], bordercolor=colors["border"], padding=4, font=fonts["compact"])
	style.configure("Sidebar.TSpinbox", fieldbackground=colors["input_bg"], foreground=colors["text"], bordercolor=colors["border"], padding=2, font=fonts["compact"])
	style.configure("Sidebar.TButton", font=fonts["compact_medium"], padding=(6, 4))
	style.configure("Sidebar.Accent.TButton", background=colors["gold"], foreground="#10141A", bordercolor=colors["gold_soft"], font=fonts["compact_semibold"], padding=(8, 6))
	style.map("Sidebar.Accent.TButton", background=[("active", "#E8C06A"), ("pressed", colors["gold_soft"]), ("disabled", colors["panel"])], foreground=[("disabled", colors["text_muted"])])
	style.configure("Sidebar.Stop.TButton", background=colors["danger"], foreground="#FFFFFF", bordercolor=colors["danger"], font=fonts["compact_semibold"], padding=(8, 6))
	style.map("Sidebar.Stop.TButton", background=[("active", colors["danger_hover"]), ("pressed", "#A9444C")])
	style.configure("Sidebar.TNotebook.Tab", padding=(8, 5), font=fonts["compact_medium"])
	style.configure("Sidebar.Card.TLabelframe", background=colors["panel_alt"], foreground=colors["gold"], bordercolor=colors["border_soft"])
	style.configure("Sidebar.Card.TLabelframe.Label", background=colors["panel_alt"], foreground=colors["gold"], font=fonts["compact_semibold"])

	style.configure("TLabelframe", background=colors["bg"], foreground=colors["gold"], bordercolor=colors["border"])
	style.configure("TLabelframe.Label", background=colors["bg"], foreground=colors["gold"], font=fonts["semibold"])
	style.configure("Card.TLabelframe", background=colors["panel_alt"], foreground=colors["gold"], bordercolor=colors["border_soft"])
	style.configure("Card.TLabelframe.Label", background=colors["panel_alt"], foreground=colors["gold"], font=fonts["semibold"])
	style.configure("TLabel", background=colors["bg"], foreground=colors["text"], font=fonts["regular"])

	style.configure(
		"TButton",
		background=colors["elevated"],
		foreground=colors["text"],
		bordercolor=colors["border"],
		borderwidth=1,
		focuscolor=colors["accent"],
		padding=(10, 6),
		font=fonts["medium"],
	)
	style.map(
		"TButton",
		background=[("active", colors["accent"]), ("pressed", colors["accent_hover"]), ("disabled", colors["panel"])],
		foreground=[("active", "#FFFFFF"), ("disabled", colors["text_muted"])],
		bordercolor=[("active", colors["accent"])],
	)

	style.configure("TCheckbutton", background=colors["bg"], foreground=colors["text"], font=fonts["regular"])
	style.configure("TRadiobutton", background=colors["bg"], foreground=colors["text"], font=fonts["regular"])
	style.map("TCheckbutton", background=[("active", colors["bg"])])
	style.map("TRadiobutton", background=[("active", colors["bg"])])

	style.configure(
		"TEntry",
		fieldbackground=colors["input_bg"],
		foreground=colors["text"],
		bordercolor=colors["border"],
		insertcolor=colors["gold"],
		padding=6,
		font=fonts["regular"],
	)
	style.map("TEntry", bordercolor=[("focus", colors["accent"])])

	style.configure(
		"TCombobox",
		fieldbackground=colors["input_bg"],
		foreground=colors["text"],
		bordercolor=colors["border"],
		padding=6,
		font=fonts["regular"],
	)
	style.map("TCombobox", bordercolor=[("focus", colors["accent"])])

	style.configure(
		"TSpinbox",
		fieldbackground=colors["input_bg"],
		foreground=colors["text"],
		bordercolor=colors["border"],
		padding=6,
		font=fonts["regular"],
	)
	style.map("TSpinbox", bordercolor=[("focus", colors["accent"])])

	style.configure("TNotebook", background=colors["bg"], bordercolor=colors["border"], borderwidth=0, tabmargins=(2, 4, 2, 0))
	style.configure(
		"TNotebook.Tab",
		background=colors["panel_alt"],
		foreground=colors["text_muted"],
		bordercolor=colors["border_soft"],
		padding=(14, 8),
		font=fonts["medium"],
	)
	style.map(
		"TNotebook.Tab",
		background=[("selected", colors["elevated"]), ("active", colors["panel"])],
		foreground=[("selected", colors["gold"]), ("active", colors["text"])],
		bordercolor=[("selected", colors["gold_soft"])],
	)

	style.configure(
		"Horizontal.TProgressbar",
		troughcolor=colors["panel_alt"],
		background=colors["gold"],
		bordercolor=colors["bg"],
		lightcolor=colors["gold"],
		darkcolor=colors["gold_soft"],
		thickness=10,
	)

	style.configure(
		"Accent.TButton",
		background=colors["gold"],
		foreground="#10141A",
		bordercolor=colors["gold_soft"],
		borderwidth=1,
		padding=(12, 8),
		font=fonts["semibold"],
	)
	style.map(
		"Accent.TButton",
		background=[("active", "#E8C06A"), ("pressed", colors["gold_soft"]), ("disabled", colors["panel"])],
		foreground=[("active", "#10141A"), ("disabled", colors["text_muted"])],
		bordercolor=[("active", "#E8C06A")],
	)

	style.configure(
		"Stop.TButton",
		background=colors["danger"],
		foreground="#FFFFFF",
		bordercolor=colors["danger"],
		borderwidth=1,
		padding=(12, 8),
		font=fonts["semibold"],
	)
	style.map(
		"Stop.TButton",
		background=[("active", colors["danger_hover"]), ("pressed", "#A9444C")],
		foreground=[("active", "#FFFFFF")],
		bordercolor=[("active", colors["danger_hover"])],
	)

	style.configure("TFrame", background=colors["bg"])
	style.configure("Panel.TFrame", background=colors["panel_alt"])
	style.configure("Card.TFrame", background=colors["panel_alt"])
	style.configure("Card.TCheckbutton", background=colors["panel_alt"], foreground=colors["text"], font=fonts["regular"])
	style.configure("Card.TRadiobutton", background=colors["panel_alt"], foreground=colors["text"], font=fonts["regular"])
	style.map("Card.TCheckbutton", background=[("active", colors["panel_alt"])])
	style.map("Card.TRadiobutton", background=[("active", colors["panel_alt"])])
	style.configure("TScrollbar", background=colors["panel_alt"], troughcolor=colors["bg"], bordercolor=colors["border"])

	return {"fonts": fonts, **colors}


# ----------------------------
# App
# ----------------------------

def ui():
	ensure_homebrew_path()

	root = tk.Tk()
	root.title(f"{APP_NAME}  v{APP_VERSION}")
	# Window sizing: 80% of screen, centered
	screen_w = root.winfo_screenwidth()
	screen_h = root.winfo_screenheight()
	w = int(screen_w * 0.8)
	h = int(screen_h * 0.8)
	x = (screen_w - w) // 2
	y = (screen_h - h) // 2
	root.geometry(f"{w}x{h}+{x}+{y}")
	root.minsize(1120, 780)
	style = ttk.Style(root)
	theme = apply_professional_theme(root, style)
	fonts = theme["fonts"]
	BG_COLOR = theme["bg"]
	SECONDARY_BG = theme["panel_alt"]
	TEXT_COLOR = theme["text"]
	HIGHLIGHT_TEXT = theme["gold"]
	BORDER_COLOR = theme["border"]
	ACCENT_COLOR = theme["accent"]
	TEXT_BG = theme["input_bg"]
	UI_FONT = fonts["regular"]
	UI_FONT_BOLD = fonts["semibold"]

	header = tk.Frame(root, bg=theme["panel"], highlightthickness=1, highlightbackground=theme["border"])
	header.pack(fill="x", padx=14, pady=(14, 0))
	tk.Label(header, text=APP_NAME, font=fonts["title"], fg=theme["gold"], bg=theme["panel"]).pack(anchor="w", padx=18, pady=(14, 0))
	tk.Label(
		header,
		text="Tự động tìm & tải footage theo kịch bản — mỗi dòng là một ĐOẠN",
		font=fonts["subtitle"],
		fg=theme["text_muted"],
		bg=theme["panel"],
	).pack(anchor="w", padx=18, pady=(4, 14))

	main_frame = ttk.Frame(root, padding=(14, 10, 14, 14))
	main_frame.pack(fill="both", expand=True)

	left_panel = ttk.Frame(main_frame, width=468, style="Panel.TFrame")
	left_panel.pack(side="left", fill="y", padx=(0, 10))
	left_panel.pack_propagate(False)

	right_panel = ttk.Frame(main_frame)
	right_panel.pack(side="left", fill="both", expand=True)

	# Settings vars
	project_var = tk.StringVar()
	results_per_query_var = tk.IntVar(value=DEFAULT_RESULTS_PER_QUERY)
	videos_per_para_var = tk.IntVar(value=VIDEOS_PER_PARAGRAPH)
	use_bilibili_var = tk.BooleanVar(value=False)
	use_x_var = tk.BooleanVar(value=True)
	threads_var = tk.IntVar(value=DEFAULT_DOWNLOAD_THREADS)
	human_mode_var = tk.BooleanVar(value=True)
	cookies_browser_var = tk.StringVar(value="comet")
	normalize_var = tk.BooleanVar(value=True)
	ai_mode_var = tk.StringVar(value="ollama")
	ollama_model_var = tk.StringVar(value="llama3.1")
	rerank_var = tk.BooleanVar(value=True)
	rerank_topn_var = tk.IntVar(value=DEFAULT_RERANK_CANDIDATES)
	judge_var = tk.BooleanVar(value=True)
	use_tavily_var = tk.BooleanVar(value=False)
	tavily_max_var = tk.IntVar(value=DEFAULT_TAVILY_MAX)
	cool_mode_var = tk.BooleanVar(value=DEFAULT_COOL_MODE)
	ollama_threads_var = tk.IntVar(value=DEFAULT_OLLAMA_THREADS)
	use_cloud_var = tk.BooleanVar(value=True)
	cloud_provider_var = tk.StringVar(value="groq")
	cloud_base_url_var = tk.StringVar(value="")
	cloud_model_var = tk.StringVar(value="")
	use_llm_web_var = tk.BooleanVar(value=False)
	llm_search_model_var = tk.StringVar(value="")
	pin_font_var = tk.StringVar(value=fonts["family"])
	pin_size_var = tk.IntVar(value=14)
	github_token_var = tk.StringVar(value="")
	auto_check_update_var = tk.BooleanVar(value=True)
	cloud_cfg = {"keys": []}
	tavily_cfg = {"keys": []}
	tavily_state = {"idx": 0}
	llm_web_state = {"idx": 0}

	# Resume state
	resume = {
		"project_root": None,
		"paragraph_index": 0,
		"script": "",
	}

	def log(line: str):
		log_txt.insert("end", line + "\n")
		log_txt.see("end")
		root.update_idletasks()
		write_log_line(line)

	def set_progress(step: str, value=None, maximum=None):
		step_var.set("Chi tiết: " + step)
		if maximum is not None:
			pbar["maximum"] = maximum
		if value is not None:
			pbar["value"] = value
		root.update_idletasks()

	def set_overall_progress(step: str, value=None, maximum=None):
		overall_step_var.set("Tổng quan: " + step)
		if maximum is not None:
			overall_pbar["maximum"] = maximum
		if value is not None:
			overall_pbar["value"] = value
		root.update_idletasks()

	def check_cloud_keys():
		keys = cloud_keys()
		if not keys:
			messagebox.showinfo("Check API Keys", "Không có API key nào để kiểm tra.")
			return
		
		log("\n--- BẮT ĐẦU KIỂM TRA CLOUD API KEYS ---")
		prov = (cloud_provider_var.get().strip() or "groq").lower()
		prov_display = prov
		model = cloud_model_var.get().strip()
		
		def run_check():
			if prov == "ollama":
				summary = audit_ollama_keys_batch(keys, logf=log, max_workers=8)
				apply_ollama_audit_rotation(keys, logf=log)
				ready_n = len(summary.get("ready") or [])
				quota_n = len(summary.get("quota") or [])
				bad_n = len(summary.get("invalid") or []) + len(summary.get("error") or []) + len(summary.get("suspended") or [])
				log("--- HOÀN THÀNH KIỂM TRA CLOUD API KEYS ---\n")
				msg = (
					f"{summary['total']} Ollama keys\n"
					f"✅ Sẵn sàng: {ready_n}\n"
					f"⚠ Hết quota: {quota_n}\n"
					f"❌ Lỗi/khóa: {bad_n}"
				)
				if summary.get("ready"):
					msg += f"\n\nXoay vòng ưu tiên: {', '.join('#' + str(x) for x in summary['ready'][:12])}"
					if len(summary["ready"]) > 12:
						msg += " …"
				root.after(0, lambda: messagebox.showinfo("Ollama — Kết quả", msg))
				return
			for i, key in enumerate(keys, start=1):
				log(f"Đang check Key #{i} (nhà cung cấp: {prov_display})...")
				if prov == "gemini":
					mdl = model or "gemini-1.5-flash"
					url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key={urllib.parse.quote(key)}"
					payload = {"contents": [{"parts": [{"text": "OK"}]}]}
					headers = {"Content-Type": "application/json"}
					res, err = _http_post_json(url, payload, headers, 15)
					if err:
						log(f"  -> Key #{i}: ❌ LỖI - {err}")
					else:
						log(f"  -> Key #{i}:  HOẠT ĐỘNG (OK)")
				elif prov in ("openrouter", "custom"):
					mdl = model or DEFAULT_OPENROUTER_MODEL
					base = cloud_base_url_var.get().strip() or (DEFAULT_OPENROUTER_BASE if prov == "openrouter" else "")
					url = openai_chat_completions_url(base)
					payload = {"model": mdl, "messages": [{"role": "user", "content": "OK"}], "temperature": 0.2}
					headers = {
						"Authorization": f"Bearer {key}",
						"Content-Type": "application/json",
						**openai_compat_extra_headers(prov),
					}
					res, err = _http_post_json(url, payload, headers, 20)
					if err:
						log(f"  -> Key #{i}: ❌ LỖI - {err}")
					else:
						log(f"  -> Key #{i}: ✅ HOẠT ĐỘNG — {base} · model {mdl}")
				else:
					mdl = model or "llama-3.1-8b-instant"
					url = "https://api.groq.com/openai/v1/chat/completions"
					payload = {"model": mdl, "messages": [{"role": "user", "content": "OK"}], "temperature": 0.2}
					headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
					res, err = _http_post_json(url, payload, headers, 15)
					if err:
						friendly_err = err
						if "403" in err or "Access denied" in err:
							friendly_err += " (Lỗi 403: Groq chặn IP Việt Nam. Vui lòng bật VPN / 1.1.1.1 rồi kiểm tra lại)"
						log(f"  -> Key #{i}: ❌ LỖI - {friendly_err}")
					else:
						log(f"  -> Key #{i}:  HOẠT ĐỘNG (OK)")
			log("--- HOÀN THÀNH KIỂM TRA CLOUD API KEYS ---\n")
			messagebox.showinfo("Hoàn thành", "Đã kiểm tra xong các Cloud API Keys. Hãy xem chi tiết ở mục Log.")

		threading.Thread(target=run_check, daemon=True).start()

	def open_ollama_settings():
		import webbrowser
		webbrowser.open("https://ollama.com/settings")

	def check_ollama_keys():
		keys = cloud_keys()
		if not keys:
			messagebox.showinfo("Ollama", "Chưa có API key nào trong ô Cloud API Keys.")
			return

		log(f"\n--- KIỂM TRA {len(keys)} OLLAMA KEYS (tự động) ---")
		log("Cách hoạt động: xác thực key + thử gọi cloud 1 token → phát hiện hết quota/rate-limit.")
		log("Đang quét song song (có thể mất vài phút nếu nhiều key)…")

		def run_check():
			summary = audit_ollama_keys_batch(keys, logf=log, max_workers=8)
			apply_ollama_audit_rotation(keys, logf=log)
			ready_n = len(summary.get("ready") or [])
			quota_n = len(summary.get("quota") or [])
			bad_n = len(summary.get("invalid") or []) + len(summary.get("error") or []) + len(summary.get("suspended") or [])
			log("--- HOÀN THÀNH KIỂM TRA OLLAMA ---\n")
			if ready_n == 0:
				log("⚠ Không có key sẵn sàng — pipeline sẽ thử xoay vòng khi gặp lỗi quota.")
			msg = (
				f"{summary['total']} keys\n"
				f"✅ Sẵn sàng: {ready_n}\n"
				f"⚠ Hết quota: {quota_n}\n"
				f"❌ Lỗi/khóa: {bad_n}"
			)
			if summary.get("ready"):
				msg += f"\n\nXoay vòng ưu tiên: {', '.join('#' + str(x) for x in summary['ready'][:12])}"
				if len(summary["ready"]) > 12:
					msg += " …"
			msg += "\n\nĐã lưu thứ tự xoay vòng — bấm CHẠY sẽ tự dùng key sẵn sàng trước."
			root.after(0, lambda: messagebox.showinfo("Ollama — Kết quả", msg))

		threading.Thread(target=run_check, daemon=True).start()

	def check_tavily_keys():
		keys = tavily_keys()
		if not keys:
			messagebox.showinfo("Check API Keys", "Không có Tavily API key nào để kiểm tra.")
			return
			
		log("\n--- BẮT ĐẦU KIỂM TRA TAVILY CREDITS ---")
		
		def run_check():
			ctx = ssl._create_unverified_context()
			for i, key in enumerate(keys, start=1):
				log(f"Đang check Key #{i}...")
				url = "https://api.tavily.com/usage"
				req = urllib.request.Request(
					url,
					headers={
						"Authorization": f"Bearer {key}",
						"User-Agent": "Mozilla/5.0"
					}
				)
				try:
					with urllib.request.urlopen(req, timeout=15, context=ctx) as response:
						data = json.loads(response.read().decode("utf-8"))
						account = data.get("account", {})
						plan_limit = account.get("plan_limit", 1000)
						plan_usage = account.get("plan_usage", 0)
						if plan_limit is None:
							key_data = data.get("key", {})
							plan_limit = key_data.get("limit", 1000) or 1000
							plan_usage = key_data.get("usage", 0)
						
						remaining = plan_limit - plan_usage
						log(f"  -> Key #{i}:  HOẠT ĐỘNG - Còn lại {remaining}/{plan_limit} credits (Đã dùng: {plan_usage})")
				except Exception as e:
					log(f"  -> Key #{i}: ❌ LỖI - {e}")
			log("--- HOÀN THÀNH KIỂM TRA TAVILY CREDITS ---\n")
			messagebox.showinfo("Hoàn thành", "Đã kiểm tra xong các Tavily API Keys. Hãy xem chi tiết ở mục Log.")

		threading.Thread(target=run_check, daemon=True).start()

	def _clamp_tavily_max(val) -> int:
		try:
			n = int(val)
		except Exception:
			n = DEFAULT_TAVILY_MAX
		return max(1, min(10, n))

	def save_settings():
		try:
			SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
			data = {
				"project_folder": project_var.get().strip(),
				"results_per_query": int(results_per_query_var.get()),
				"videos_per_paragraph": int(videos_per_para_var.get()),
				"download_threads": int(threads_var.get()),
				"human_mode": bool(human_mode_var.get()),
				"cookies_browser": cookies_browser_var.get().strip(),
				"normalize": bool(normalize_var.get()),
				"ai_mode": ai_mode_var.get().strip(),
				"ollama_model": ollama_model_var.get().strip(),
				"rerank": bool(rerank_var.get()),
				"rerank_topn": int(rerank_topn_var.get()),
				"judge": bool(judge_var.get()),
				"use_bilibili": bool(use_bilibili_var.get()),
				"use_tavily": bool(use_tavily_var.get()),
				"tavily_max": _clamp_tavily_max(tavily_max_var.get()),
				"cool_mode": bool(cool_mode_var.get()),
				"ollama_threads": int(ollama_threads_var.get()),
				"use_cloud": bool(use_cloud_var.get()),
				"cloud_provider": cloud_provider_var.get().strip(),
				"cloud_base_url": cloud_base_url_var.get().strip(),
				"cloud_model": cloud_model_var.get().strip(),
				"use_llm_web": bool(use_llm_web_var.get()),
				"llm_search_model": llm_search_model_var.get().strip(),
				"cloud_api_keys": cloud_keys(),
				"tavily_api_keys": tavily_keys(),
				"use_x": bool(use_x_var.get()),
				"last_project_root": resume.get("project_root"),
				"pin_font": pin_font_var.get(),
				"pin_size": int(pin_size_var.get()),
				"github_token": github_token_var.get().strip(),
				"auto_check_update": bool(auto_check_update_var.get()),
			}
			SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
		except Exception:
			pass

	def load_settings():
		try:
			if SETTINGS_PATH.exists():
				data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
				project_var.set(data.get("project_folder", "") or "")
				results_per_query_var.set(int(data.get("results_per_query", DEFAULT_RESULTS_PER_QUERY)))
				videos_per_para_var.set(int(data.get("videos_per_paragraph", VIDEOS_PER_PARAGRAPH)))
				threads_var.set(int(data.get("download_threads", DEFAULT_DOWNLOAD_THREADS)))
				human_mode_var.set(bool(data.get("human_mode", True)))
				cookies_browser_var.set(str(data.get("cookies_browser", "comet")))
				normalize_var.set(bool(data.get("normalize", True)))
				if data.get("ai_mode") in {"rules", "ollama"}:
					ai_mode_var.set(data.get("ai_mode"))
				ollama_model_var.set(str(data.get("ollama_model", "llama3.1")))
				rerank_var.set(bool(data.get("rerank", True)))
				rerank_topn_var.set(int(data.get("rerank_topn", DEFAULT_RERANK_CANDIDATES)))
				judge_var.set(bool(data.get("judge", True)))
				use_bilibili_var.set(bool(data.get("use_bilibili", False)))
				use_tavily_var.set(bool(data.get("use_tavily", False)))
				use_x_var.set(bool(data.get("use_x", True)))
				_raw_tm = data.get("tavily_max", DEFAULT_TAVILY_MAX)
				try:
					if int(_raw_tm) > 10:
						_raw_tm = DEFAULT_TAVILY_MAX
				except Exception:
					_raw_tm = DEFAULT_TAVILY_MAX
				tavily_max_var.set(_clamp_tavily_max(_raw_tm))
				cool_mode_var.set(bool(data.get("cool_mode", DEFAULT_COOL_MODE)))
				ollama_threads_var.set(int(data.get("ollama_threads", DEFAULT_OLLAMA_THREADS)))
				use_cloud_var.set(bool(data.get("use_cloud", False)))
				cloud_provider_var.set(str(data.get("cloud_provider", "groq")))
				cloud_base_url_var.set(str(data.get("cloud_base_url", "")))
				cloud_model_var.set(str(data.get("cloud_model", "")))
				use_llm_web_var.set(bool(data.get("use_llm_web", False)))
				llm_search_model_var.set(str(data.get("llm_search_model", "")))
				_ck = data.get("cloud_api_keys")
				if isinstance(_ck, list):
					cloud_cfg["keys"] = [str(x).strip() for x in _ck if str(x).strip()]
				_ks = data.get("tavily_api_keys")
				if isinstance(_ks, list):
					tavily_cfg["keys"] = [str(x).strip() for x in _ks if str(x).strip()]
				resume["project_root"] = data.get("last_project_root")
				
				pin_font_var.set(str(data.get("pin_font", fonts["family"])))
				try:
					pin_size_var.set(int(data.get("pin_size", 14)))
				except Exception:
					pass
				if "github_token" in data:
					github_token_var.set(str(data.get("github_token") or ""))
				if "auto_check_update" in data:
					auto_check_update_var.set(bool(data.get("auto_check_update", True)))
		except Exception:
			pass

	def save_resume_state(project_root: Path, paragraph_index: int, script: str):
		try:
			meta = project_root / "03_metadata"
			meta.mkdir(parents=True, exist_ok=True)
			st = {
				"project_root": str(project_root),
				"paragraph_index": int(paragraph_index),
				"script": script,
				"article_links": resume.get("article_links", {}),
				"completed_paragraphs": resume.get("completed_paragraphs", []),
			}
			(meta / "state.json").write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
		except Exception:
			pass

	def load_resume_state() -> bool:
		try:
			pr = resume.get("project_root")
			if not pr:
				return False
			st_path = Path(pr) / "03_metadata" / "state.json"
			if not st_path.exists():
				return False
			st = json.loads(st_path.read_text(encoding="utf-8"))
			resume["project_root"] = st.get("project_root")
			resume["paragraph_index"] = int(st.get("paragraph_index", 0))
			resume["script"] = st.get("script", "")
			resume["article_links"] = st.get("article_links", {})
			resume["completed_paragraphs"] = st.get("completed_paragraphs", [])
			return True
		except Exception:
			return False

	load_settings()
	load_resume_state()

	# -----------------------------------------------------
	# Left Panel Controls (Settings Notebook & Action Buttons)
	# -----------------------------------------------------

	def choose_folder():
		d = filedialog.askdirectory(title="Chọn folder lưu Project")
		if d:
			project_var.set(d)

	# Notebook
	notebook = ttk.Notebook(left_panel, style="TNotebook")
	notebook.pack(fill="both", expand=True, pady=(0, 6))
	for tab_style in ("TNotebook.Tab", "Sidebar.TNotebook.Tab"):
		try:
			style.configure(tab_style, padding=(8, 5), font=fonts["compact_medium"])
		except Exception:
			pass

	# Tab 1: Cấu hình chung
	tab_general = ttk.Frame(notebook, padding=6)
	notebook.add(tab_general, text="Cấu hình chung")

	# Project Folder selector
	proj_row = ttk.Frame(tab_general)
	proj_row.pack(fill="x", pady=(2, 4))
	ttk.Label(proj_row, text="Thư mục lưu trữ:", style="Sidebar.TLabel").pack(anchor="w")
	
	proj_entry_row = ttk.Frame(proj_row)
	proj_entry_row.pack(fill="x", pady=(2, 0))
	ttk.Entry(proj_entry_row, textvariable=project_var, style="Sidebar.TEntry").pack(side="left", fill="x", expand=True, padx=(0, 4))
	ttk.Button(proj_entry_row, text="Chọn", command=choose_folder, width=7, style="Sidebar.TButton").pack(side="right")

	# Spinboxes — 2 hàng, tránh cắt chữ
	spin_row = ttk.Frame(tab_general)
	spin_row.pack(fill="x", pady=4)
	spin_row.columnconfigure(1, weight=1)
	spin_row.columnconfigure(3, weight=1)

	ttk.Label(spin_row, text="Kết quả/tìm:", style="Sidebar.TLabel").grid(row=0, column=0, sticky="w", pady=2)
	ttk.Spinbox(spin_row, from_=3, to=30, increment=1, textvariable=results_per_query_var, width=5, style="Sidebar.TSpinbox").grid(row=0, column=1, sticky="w", padx=(4, 12), pady=2)
	ttk.Label(spin_row, text="Video/đoạn:", style="Sidebar.TLabel").grid(row=0, column=2, sticky="w", pady=2)
	ttk.Spinbox(spin_row, from_=1, to=20, increment=1, textvariable=videos_per_para_var, width=5, style="Sidebar.TSpinbox").grid(row=0, column=3, sticky="w", padx=(4, 0), pady=2)
	ttk.Label(spin_row, text="Luồng tải:", style="Sidebar.TLabel").grid(row=1, column=0, sticky="w", pady=2)
	ttk.Spinbox(spin_row, from_=1, to=32, increment=1, textvariable=threads_var, width=5, style="Sidebar.TSpinbox").grid(row=1, column=1, sticky="w", padx=(4, 0), pady=2)

	# Nguồn video
	sources_frame = ttk.LabelFrame(tab_general, text="Nguồn", padding=6, style="Sidebar.Card.TLabelframe")
	sources_frame.pack(fill="x", pady=4)
	ttk.Checkbutton(sources_frame, text="Bili & Douyin (Trung Quốc)", variable=use_bilibili_var, style="Sidebar.Card.TCheckbutton").pack(anchor="w", pady=1)
	ttk.Checkbutton(sources_frame, text="X.com / Twitter (cần Chrome)", variable=use_x_var, style="Sidebar.Card.TCheckbutton").pack(anchor="w", pady=1)

	# Cookies
	cookies_frame = ttk.LabelFrame(tab_general, text="Cookies", padding=6, style="Sidebar.Card.TLabelframe")
	cookies_frame.pack(fill="x", pady=4)
	cookies_row = ttk.Frame(cookies_frame, style="Sidebar.TFrame")
	cookies_row.pack(fill="x")
	ttk.Label(cookies_row, text="Trình duyệt:", style="Sidebar.Card.TLabel").pack(side="left")
	ttk.Combobox(
		cookies_row,
		textvariable=cookies_browser_var,
		values=["chrome", "comet", "safari", "edge", "firefox", "opera"],
		width=9,
		state="readonly",
		style="Sidebar.TCombobox",
	).pack(side="left", padx=(4, 0))
	ttk.Checkbutton(cookies_frame, text="Mô phỏng người dùng", variable=human_mode_var, style="Sidebar.Card.TCheckbutton").pack(anchor="w", pady=(4, 0))

	# Mode Options Frame
	toggles_frame = ttk.LabelFrame(tab_general, text="Tùy chọn", padding=6, style="Sidebar.Card.TLabelframe")
	toggles_frame.pack(fill="x", pady=4)
	
	ttk.Checkbutton(
		toggles_frame,
		text="Đảo TTS → chuẩn tìm kiếm",
		variable=normalize_var,
		style="Sidebar.Card.TCheckbutton",
	).pack(anchor="w", pady=1)
	
	rerank_row = ttk.Frame(toggles_frame, style="Sidebar.TFrame")
	rerank_row.pack(fill="x", anchor="w", pady=1)
	ttk.Checkbutton(rerank_row, text="AI sắp xếp lại", variable=rerank_var, style="Sidebar.Card.TCheckbutton").pack(side="left")
	ttk.Label(rerank_row, text="Top:", style="Sidebar.Card.TLabel").pack(side="left", padx=(8, 2))
	ttk.Spinbox(rerank_row, from_=6, to=50, increment=1, textvariable=rerank_topn_var, width=4, style="Sidebar.TSpinbox").pack(side="left")
	
	ttk.Checkbutton(toggles_frame, text="Editor: kiểm tra phụ đề", variable=judge_var, style="Sidebar.Card.TCheckbutton").pack(anchor="w", pady=1)
	
	cool_row = ttk.Frame(toggles_frame, style="Sidebar.TFrame")
	cool_row.pack(fill="x", anchor="w", pady=1)
	ttk.Checkbutton(cool_row, text="Chống nóng CPU", variable=cool_mode_var, style="Sidebar.Card.TCheckbutton").pack(side="left")
	ttk.Label(cool_row, text="Luồng:", style="Sidebar.Card.TLabel").pack(side="left", padx=(8, 2))
	ttk.Spinbox(cool_row, from_=1, to=16, increment=1, textvariable=ollama_threads_var, width=4, style="Sidebar.TSpinbox").pack(side="left")

	# Brain Mode selector
	brain_frame = ttk.LabelFrame(tab_general, text="Brain", padding=6, style="Sidebar.Card.TLabelframe")
	brain_frame.pack(fill="x", pady=4)
	ttk.Radiobutton(brain_frame, text="Rules (offline)", variable=ai_mode_var, value="rules", style="Sidebar.Card.TRadiobutton").pack(anchor="w", pady=1)
	ttk.Radiobutton(brain_frame, text="Cloud AI (LLM)", variable=ai_mode_var, value="ollama", style="Sidebar.Card.TRadiobutton").pack(anchor="w", pady=1)

	# System / update
	sys_frame = ttk.LabelFrame(tab_general, text="Hệ thống & cập nhật", padding=6, style="Sidebar.Card.TLabelframe")
	sys_frame.pack(fill="x", pady=4)
	ttk.Label(
		sys_frame,
		text=f"v{APP_VERSION} · GitHub: {auto_update.GITHUB_OWNER}/{auto_update.GITHUB_REPO}",
		style="Sidebar.Card.TLabel",
	).pack(anchor="w")
	ttk.Checkbutton(
		sys_frame,
		text="Tự kiểm tra cập nhật khi mở app",
		variable=auto_check_update_var,
		style="Sidebar.Card.TCheckbutton",
	).pack(anchor="w", pady=(4, 2))
	tok_row = ttk.Frame(sys_frame, style="Sidebar.TFrame")
	tok_row.pack(fill="x", pady=2)
	ttk.Label(tok_row, text="GitHub token:", style="Sidebar.Card.TLabel").pack(side="left")
	ttk.Entry(tok_row, textvariable=github_token_var, show="•", width=22).pack(side="left", padx=6, fill="x", expand=True)
	sys_btn_row = ttk.Frame(sys_frame, style="Sidebar.TFrame")
	sys_btn_row.pack(fill="x", pady=(6, 0))

	def do_install_deps(force=True):
		log("[INFO] Auto-setup dependencies…")
		ok = run_auto_setup_dialog(root, log_fn=log, force=force, need_gallery_dl=True)
		if ok:
			messagebox.showinfo("Dependencies", "Đã đủ: yt-dlp, ffmpeg, ffprobe, gallery-dl.")
		else:
			miss = preflight_missing_bins(check_gallery_dl=True)
			messagebox.showerror(
				"Dependencies",
				"Vẫn thiếu: " + (", ".join(miss) if miss else "không rõ") + "\n\nLog: ~/.newsfootage_hunter/app.log",
			)

	def do_check_update(silent: bool = False):
		token = github_token_var.get().strip() or None
		log("[UPDATE] Kiểm tra bản mới trên GitHub…")

		def on_info(info: dict):
			def ui_handle():
				if not info.get("ok"):
					err = info.get("error") or "Không kiểm tra được update"
					if silent:
						log(f"[UPDATE] (im lặng) {err}")
						return
					messagebox.showwarning("Cập nhật", err)
					return
				if not info.get("update_available"):
					msg = f"Bạn đang dùng bản mới nhất (v{info.get('local_version') or APP_VERSION})."
					if info.get("remote_version"):
						msg += f"\nRemote: v{info.get('remote_version')}"
					if info.get("error"):
						msg += f"\n\nGhi chú: {info.get('error')}"
					log(f"[UPDATE] {msg.replace(chr(10), ' ')}")
					if not silent:
						messagebox.showinfo("Cập nhật", msg)
					return

				remote = info.get("remote_version")
				body = (info.get("body") or "").strip()
				preview = body[:500] + ("…" if len(body) > 500 else "")
				ask = (
					f"Có bản mới: v{remote} (bạn đang dùng v{info.get('local_version')}).\n\n"
					f"{preview}\n\n"
					"Tải về, thoát app, thay thế và mở lại bản mới?"
				)
				if not messagebox.askyesno("Cập nhật có sẵn", ask):
					log("[UPDATE] User bỏ qua bản mới.")
					return

				win = tk.Toplevel(root)
				win.title("Đang cập nhật…")
				win.geometry("560x360")
				win.transient(root)
				win.grab_set()
				status = tk.StringVar(value=f"Đang tải v{remote}…")
				ttk.Label(win, textvariable=status).pack(anchor="w", padx=12, pady=10)
				utxt = tk.Text(win, height=14, wrap="word")
				utxt.pack(fill="both", expand=True, padx=12, pady=6)
				p = ttk.Progressbar(win, mode="indeterminate")
				p.pack(fill="x", padx=12, pady=8)
				p.start(12)
				cancel_ev = threading.Event()

				def alog(line: str):
					def _():
						try:
							utxt.insert("end", line + "\n")
							utxt.see("end")
						except Exception:
							pass
						log(line)

					win.after(0, _)

				def worker():
					try:
						out = auto_update.perform_update(
							token=token,
							log=alog,
							cancel=cancel_ev.is_set,
							release_info=info,
						)
						staged = out.get("staged_app")
						if not staged:
							raise RuntimeError(out.get("error") or "Không tải được bản mới")

						def ask_apply():
							try:
								p.stop()
							except Exception:
								pass
							win.destroy()
							if not auto_update.running_from_app():
								messagebox.showinfo(
									"Đã tải xong",
									"Bạn đang chạy từ source (python), không thể tự thay .app.\n"
									f"Bản mới:\n{staged}",
								)
								return
							if messagebox.askyesno(
								"Cài đặt bản mới",
								"Tải xong. App sẽ thoát, thay .app cũ và tự mở lại.\nTiếp tục?",
							):
								try:
									auto_update.apply_update_and_relaunch(Path(staged), log=log)
								except Exception as e:
									messagebox.showerror("Update", str(e))
									return
								save_settings()
								root.destroy()
								os._exit(0)

						root.after(0, ask_apply)
					except Exception as e:
						def fail():
							try:
								p.stop()
							except Exception:
								pass
							status.set("Lỗi cập nhật")
							alog(f"[ERR] {e}")
							messagebox.showerror("Update", str(e))
							win.destroy()

						root.after(0, fail)

				threading.Thread(target=worker, daemon=True).start()

			root.after(0, ui_handle)

		auto_update.check_for_update_async(on_done=on_info, token=token)

	ttk.Button(sys_btn_row, text="Cài dependencies", command=lambda: do_install_deps(True)).pack(
		side="left", expand=True, fill="x", padx=(0, 4)
	)
	ttk.Button(sys_btn_row, text="Kiểm tra cập nhật", command=lambda: do_check_update(False)).pack(
		side="left", expand=True, fill="x", padx=(4, 0)
	)

	# Tab 2: Cloud AI (LLM) controls
	tab_cloud = ttk.Frame(notebook, padding=10)
	notebook.add(tab_cloud, text="Cloud AI")
	
	use_cloud_row = ttk.Frame(tab_cloud)
	use_cloud_row.pack(fill="x", pady=(0, 6))
	ttk.Checkbutton(use_cloud_row, text="Bật Cloud AI", variable=use_cloud_var).pack(side="left")
	
	provider_row = ttk.Frame(tab_cloud)
	provider_row.pack(fill="x", pady=4)
	ttk.Label(provider_row, text="Nhà cung cấp:").pack(side="left")
	cloud_provider_cb = ttk.Combobox(
		provider_row,
		textvariable=cloud_provider_var,
		values=["groq", "gemini", "ollama", "openrouter", "custom"],
		width=11,
		state="readonly",
	)
	cloud_provider_cb.pack(side="left", padx=(6, 12))
	ttk.Label(provider_row, text="Model:").pack(side="left")
	ttk.Entry(provider_row, textvariable=cloud_model_var, width=22).pack(side="left", padx=6)

	cloud_base_row = ttk.Frame(tab_cloud)
	cloud_base_row.pack(fill="x", pady=(0, 4))
	ttk.Label(cloud_base_row, text="Base URL:").pack(side="left")
	ttk.Entry(cloud_base_row, textvariable=cloud_base_url_var, width=42).pack(side="left", padx=(6, 0), fill="x", expand=True)

	def _sync_cloud_provider_ui(*_):
		prov = (cloud_provider_var.get().strip() or "groq").lower()
		if prov == "openrouter":
			if not cloud_base_url_var.get().strip():
				cloud_base_url_var.set(DEFAULT_OPENROUTER_BASE)
			if not cloud_model_var.get().strip():
				cloud_model_var.set(DEFAULT_OPENROUTER_MODEL)
			cloud_base_row.pack(fill="x", pady=(0, 4))
		elif prov == "custom":
			cloud_base_row.pack(fill="x", pady=(0, 4))
		else:
			cloud_base_row.pack_forget()

	cloud_provider_cb.bind("<<ComboboxSelected>>", _sync_cloud_provider_ui)
	_sync_cloud_provider_ui()
	
	btn_row = ttk.Frame(tab_cloud)
	btn_row.pack(fill="x", pady=6)
	ttk.Button(btn_row, text="Quét Ollama — 1 click (tất cả keys)", command=check_ollama_keys, style="Sidebar.Accent.TButton").pack(fill="x", pady=(0, 4))
	btn_row2 = ttk.Frame(tab_cloud)
	btn_row2.pack(fill="x", pady=(0, 4))
	ttk.Button(btn_row2, text="Kiểm tra status (nhà cung cấp hiện tại)", command=check_cloud_keys, style="Sidebar.TButton").pack(side="left", expand=True, fill="x", padx=(0, 3))
	ttk.Button(btn_row2, text="Mở ollama.com/settings", command=open_ollama_settings, style="Sidebar.TButton").pack(side="left", expand=True, fill="x", padx=(3, 0))

	ttk.Label(
		tab_cloud,
		text="API Keys (mỗi dòng 1 key, xoay vòng). OpenRouter/Custom: OpenAI-compatible /v1/chat/completions",
		style="Sidebar.TLabel",
	).pack(anchor="w", pady=(4, 2))
	cloud_keys_txt = tk.Text(tab_cloud, height=7, wrap="none", bg=TEXT_BG, fg=HIGHLIGHT_TEXT, insertbackground=HIGHLIGHT_TEXT, relief="flat", bd=1, highlightbackground=BORDER_COLOR, highlightcolor=ACCENT_COLOR, font=fonts["compact"])
	cloud_keys_txt.pack(fill="both", expand=True, pady=(0, 4))

	def cloud_keys() -> list:
		try:
			raw = cloud_keys_txt.get("1.0", "end")
		except Exception:
			return []
		out = []
		for ln in raw.splitlines():
			s = ln.strip()
			if s and s not in out:
				out.append(s)
		return out

	if cloud_cfg.get("keys"):
		try:
			cloud_keys_txt.insert("1.0", "\n".join(cloud_cfg["keys"]))
		except Exception:
			pass

	# Tab 3: Web search (Tavily + LLM)
	tab_tavily = ttk.Frame(notebook, padding=10)
	notebook.add(tab_tavily, text="Web Search")
	
	llm_web_row = ttk.LabelFrame(tab_tavily, text="LLM Web (OpenRouter / Custom)", padding=6)
	llm_web_row.pack(fill="x", pady=(0, 8))
	ttk.Checkbutton(
		llm_web_row,
		text="Bật tìm web qua LLM (khuyên dùng — thay Tavily)",
		variable=use_llm_web_var,
	).pack(anchor="w", pady=(0, 4))
	llm_model_row = ttk.Frame(llm_web_row)
	llm_model_row.pack(fill="x", pady=2)
	ttk.Label(llm_model_row, text="Model tìm web:").pack(side="left")
	ttk.Entry(llm_model_row, textvariable=llm_search_model_var, width=34).pack(side="left", padx=(6, 0), fill="x", expand=True)
	ttk.Label(
		llm_web_row,
		text="Để trống → dùng model Cloud AI. OpenRouter tự bật web_search tool (giống Deer Flow).",
		style="Sidebar.TLabel",
	).pack(anchor="w", pady=(4, 0))

	def check_llm_web():
		ok, err = sync_cloud_from_ui(
			cloud_provider_var, cloud_base_url_var, cloud_model_var,
			llm_search_model_var, cloud_keys,
		)
		if not ok:
			messagebox.showwarning("LLM Web", err)
			return
		_prov = (CLOUD.get("provider") or "").lower()
		mdl = effective_llm_search_model(use_web_tool=_prov == "openrouter")
		log(f"\n--- KIỂM TRA LLM WEB ---")
		log(f"Provider: {CLOUD['provider']} · Base: {CLOUD.get('base_url')}")
		log(f"Model tìm web: {mdl}")
		log("Đang thử tìm: Russia fuel shortage drone footage …")

		def run_test():
			state = {"idx": 0}
			try:
				res = llm_web_search_rotate(
					"Russia fuel shortage drone footage",
					max(3, int(tavily_max_var.get())),
					CLOUD["keys"],
					state,
					logf=log,
				)
				n = len(res or [])
				log(f"--- KẾT QUẢ: {n} link video ---")
				for i, item in enumerate((res or [])[:5], 1):
					log(f"  #{i}: {(item.get('title') or '?')[:60]}")
					log(f"       {item.get('url') or ''}")
				if n == 0:
					log("⚠ Không có link — thử model có :online (vd. google/gemini-2.0-flash-exp:free:online)")
				log("--- HOÀN THÀNH KIỂM TRA LLM WEB ---\n")
				msg = f"Model: {mdl}\nTìm thấy: {n} link video"
				if n:
					msg += f"\n\nVí dụ:\n{(res[0].get('url') or '')[:80]}"
				else:
					msg += "\n\nGợi ý: để trống Model tìm web, hoặc dùng model :online trên OpenRouter."
				root.after(0, lambda: messagebox.showinfo("LLM Web — Kết quả", msg))
			except Exception as e:
				log(f"❌ LLM Web lỗi: {e}")
				root.after(0, lambda: messagebox.showerror("LLM Web", str(e)))

		threading.Thread(target=run_test, daemon=True).start()

	llm_test_row = ttk.Frame(llm_web_row)
	llm_test_row.pack(fill="x", pady=(6, 0))
	ttk.Button(llm_test_row, text="Kiểm tra LLM Web (1 click)", command=check_llm_web, style="Sidebar.Accent.TButton").pack(fill="x")

	use_tavily_row = ttk.Frame(tab_tavily)
	use_tavily_row.pack(fill="x", pady=(0, 6))
	ttk.Checkbutton(use_tavily_row, text="Bật Tavily (tìm trên web — dự phòng)", variable=use_tavily_var).pack(side="left")
	
	results_row = ttk.Frame(tab_tavily)
	results_row.pack(fill="x", pady=4)
	ttk.Label(results_row, text="Kết quả/truy vấn:").pack(side="left")
	ttk.Spinbox(results_row, from_=1, to=10, increment=1, textvariable=tavily_max_var, width=6).pack(side="left", padx=6)
	ttk.Label(results_row, text="(mặc định 5 — mỗi từ khóa, 1 lần gọi Tavily)", style="Sidebar.TLabel").pack(side="left", padx=(4, 0))
	
	tavily_btn_row = ttk.Frame(tab_tavily)
	tavily_btn_row.pack(fill="x", pady=6)
	ttk.Button(tavily_btn_row, text="Kiểm tra credit", command=check_tavily_keys).pack(side="left", expand=True, fill="x")
	
	ttk.Label(tab_tavily, text="Tavily API Keys (mỗi dòng 1 key, hết hạn tự xoay):").pack(anchor="w", pady=(8, 2))
	tavily_keys_txt = tk.Text(tab_tavily, height=6, wrap="none", bg=TEXT_BG, fg=HIGHLIGHT_TEXT, insertbackground=HIGHLIGHT_TEXT, relief="flat", bd=1, highlightbackground=BORDER_COLOR, highlightcolor=ACCENT_COLOR, font=UI_FONT)
	tavily_keys_txt.pack(fill="both", expand=True, pady=(0, 4))

	def tavily_keys() -> list:
		try:
			raw = tavily_keys_txt.get("1.0", "end")
		except Exception:
			return []
		out = []
		for ln in raw.splitlines():
			s = ln.strip()
			if s and s not in out:
				out.append(s)
		return out

	if tavily_cfg.get("keys"):
		try:
			tavily_keys_txt.insert("1.0", "\n".join(tavily_cfg["keys"]))
		except Exception:
			pass

	_settings_save_timer = {"id": None}

	def schedule_save_settings(*_):
		tid = _settings_save_timer.get("id")
		if tid:
			try:
				root.after_cancel(tid)
			except Exception:
				pass
		_settings_save_timer["id"] = root.after(600, save_settings)

	for _sv in (
		project_var, results_per_query_var, videos_per_para_var, threads_var,
		human_mode_var, cookies_browser_var, normalize_var, ai_mode_var,
		ollama_model_var, rerank_var, rerank_topn_var, judge_var,
		use_bilibili_var, use_tavily_var, tavily_max_var, cool_mode_var,
		ollama_threads_var, use_cloud_var, cloud_provider_var, cloud_base_url_var, cloud_model_var,
		use_llm_web_var, llm_search_model_var,
		use_x_var, pin_font_var, pin_size_var,
	):
		try:
			_sv.trace_add("write", schedule_save_settings)
		except Exception:
			pass
	for _tw in (cloud_keys_txt, tavily_keys_txt):
		_tw.bind("<KeyRelease>", schedule_save_settings, add="+")
		_tw.bind("<FocusOut>", schedule_save_settings, add="+")

	# Action Buttons Frame
	buttons = ttk.Frame(left_panel)
	buttons.pack(fill="x", pady=(10, 6))

	# -----------------------------------------------------
	# Right Panel Controls (Script Input & Log Console)
	# -----------------------------------------------------

	# Jina Reader Fetch URL
	url_card = ttk.LabelFrame(right_panel, text="Nhập link bài báo", padding=10, style="Card.TLabelframe")
	url_card.pack(fill="x", pady=(0, 8))
	url_frame = ttk.Frame(url_card, style="Card.TFrame")
	url_frame.pack(fill="x")
	ttk.Label(url_frame, text="URL bài viết:", style="Card.Section.TLabel").pack(side="left")
	url_var = tk.StringVar()
	url_entry = ttk.Entry(url_frame, textvariable=url_var)
	url_entry.pack(side="left", fill="x", expand=True, padx=4)

	def fetch_url_content():
		url = url_var.get().strip()
		if not url:
			messagebox.showwarning("Cảnh báo", "Vui lòng nhập URL để lấy nội dung.")
			return
		
		def worker():
			run_btn.state(["disabled"])
			try:
				import urllib.request
				if not url.startswith(("http://", "https://")):
					full_url = "https://" + url
				else:
					full_url = url
				
				jina_url = f"https://r.jina.ai/{full_url}"
				req = urllib.request.Request(
					jina_url,
					headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36", "Accept": "text/plain"},
				)
				log(f"Đang trích xuất nội dung từ: {full_url}")
				with urllib.request.urlopen(req, timeout=30) as resp:
					content_text = resp.read().decode("utf-8")
				
				def _update():
					script_txt.delete("1.0", "end")
					script_txt.insert("1.0", content_text)
					log(f"[Jina Reader] Đã trích xuất xong! Vui lòng kiểm tra kịch bản bên dưới.")
					run_btn.state(["!disabled"])
				root.after(0, _update)
			except Exception as e:
				def _err():
					log(f"[Jina Reader LỖI] Không thể tải URL: {e}")
					messagebox.showerror("Lỗi trích xuất URL", str(e))
					run_btn.state(["!disabled"])
				root.after(0, _err)
		
		import threading
		threading.Thread(target=worker, daemon=True).start()

	fetch_btn = ttk.Button(url_frame, text="Tải Kịch Bản (Jina)", command=fetch_url_content)
	fetch_btn.pack(side="left")

	script_frame = ttk.LabelFrame(right_panel, text="Kịch bản", padding=10, style="Card.TLabelframe")
	script_frame.pack(fill="both", expand=True, pady=(0, 8))

	script_header = ttk.Frame(script_frame, style="Card.TFrame")
	script_header.pack(fill="x", pady=(0, 6))
	ttk.Label(script_header, text="Mỗi dòng = 1 ĐOẠN", style="Card.Muted.TLabel").pack(side="left")

	def run_rules_normalization():
		text = script_txt.get("1.0", "end").strip()
		lines = text.splitlines()
		normalized_lines = []
		for line in lines:
			norm_line = standardize_script_for_search_and_export(line)
			normalized_lines.append(norm_line)
		normalized = "\n".join(normalized_lines)
		script_txt.delete("1.0", "end")
		script_txt.insert("1.0", normalized)
		log("[INFO] Đã chuẩn hóa kịch bản bằng bộ quy tắc tĩnh.")
		set_progress("Chuẩn hóa bằng bộ quy tắc hoàn thành")

	def trigger_normalize():
		text = script_txt.get("1.0", "end").strip()
		if not text:
			messagebox.showwarning("Thông báo", "Bạn chưa nhập kịch bản để chuẩn hóa.")
			return

		keys = cloud_keys()
		is_ai_enabled = use_cloud_var.get() and len(keys) > 0

		if is_ai_enabled:
			ans = messagebox.askyesnocancel(
				"Lựa chọn chuẩn hóa kịch bản",
				"Bạn có muốn sử dụng Cloud AI để tự động sửa lỗi chính tả và chuẩn hóa các từ phiên âm ngoại văn phức tạp không?\n\n"
				"- Bấm 'Yes' (Có): Sử dụng AI để chuẩn hóa thông minh (cần kết nối mạng và mất vài giây).\n"
				"- Bấm 'No' (Không): Sử dụng bộ quy tắc tĩnh (Rules) có sẵn (nhanh, offline).\n"
				"- Bấm 'Cancel' (Hủy): Hủy bỏ thao tác."
			)
			if ans is None:  # Cancel
				return
			elif ans:  # Yes -> AI
				normalize_btn.config(state="disabled")
				set_progress("Chuẩn hóa kịch bản bằng AI...")
				log("\n[AI Normalization] Đang gửi kịch bản lên Cloud AI để xử lý lỗi chính tả và từ phiên âm...")
				
				def run_ai_normalize():
					prov = (cloud_provider_var.get().strip() or "groq").lower()
					model = cloud_model_var.get().strip()
					
					prompt = (
						"Bạn đảo ngược kịch bản TTS (việt hoá để AI đọc) về dạng CHUẨN để tìm kiếm video.\n"
						"KHÔNG dịch sang tiếng Anh. Giữ tiếng Việt, chỉ đổi số/ngày/cụm cố định/tên phiên âm.\n"
						"\n"
						"Quy tắc ĐẢO NGƯỢC (TTS → chuẩn):\n"
						"1. Giữ nguyên từng dòng (mỗi ĐOẠN một dòng). KHÔNG gộp/tách dòng.\n"
						"2. Năm: 'năm hai không hai lăm' / 'hai'không'hai'mươi' → 2025 / 2020.\n"
						"3. Ngày: 'ngày X tháng Y năm Z' (chữ hoặc số) → X/Y/Z; 'ngày X tháng Y' → X/Y.\n"
						"4. Số: 'năm nghìn bốn trăm tám mươi' → 5480; 'mười hai phẩy bảy' → 12.7; '12 phần trăm' → 12%.\n"
						"5. Cụm cố định: kilomet trên giờ→km/h; đô la Mỹ→USD; Thế chiến 1→Thế chiến I; ym lặng→im lặng;\n"
						"   Lực lượng Phòng vệ Israel→IDF; Đảng Cộng sản Trung Quốc→ĐCSTQ; chế độ chuyên chế→Cộng sản; toàn trị→độc tài; 10 mét→10 m.\n"
						"6. Tên phiên âm → chuẩn: Mốtscâu→Moscow, Dê-len-sky→Zelensky, Hai-mác→HIMARS, pa-tri-ốt→Patriot.\n"
						"7. KHÔNG thêm/bớt/sửa câu từ làm sai nội dung. Chỉ trả về kịch bản đã chuẩn hóa.\n"
						"\n"
						"Kịch bản TTS cần đảo ngược:\n"
						f"{text}"
					)
					
					try:
						result = cloud_generate(prompt, model, timeout=30)
						if result and result.strip():
							normalized = result.strip()
							def update_ui_success():
								script_txt.delete("1.0", "end")
								script_txt.insert("1.0", normalized)
								log("[INFO] Đã chuẩn hóa kịch bản thành công bằng Cloud AI.")
								set_progress("Chuẩn hóa hoàn thành")
								normalize_btn.config(state="normal")
							root.after(0, update_ui_success)
						else:
							err_msg = CLOUD.get("last_error") or "Không nhận được phản hồi từ server"
							log(f"[WARNING] Cloud AI không phản hồi hoặc gặp lỗi: {err_msg}. Tự động chuyển sang bộ quy tắc tĩnh...")
							def update_ui_fallback():
								run_rules_normalization()
								normalize_btn.config(state="normal")
							root.after(0, update_ui_fallback)
					except Exception as ex:
						log(f"[ERROR] Lỗi trong quá trình chuẩn hóa AI: {ex}. Tự động chuyển sang bộ quy tắc tĩnh...")
						def update_ui_err_fallback():
							run_rules_normalization()
							normalize_btn.config(state="normal")
						root.after(0, update_ui_err_fallback)
						
				threading.Thread(target=run_ai_normalize, daemon=True).start()
				return
			else:  # No -> Rules
				run_rules_normalization()
		else:
			log("[INFO] Cloud AI chưa bật hoặc chưa cấu hình API key. Sử dụng bộ quy tắc tĩnh...")
			run_rules_normalization()

	normalize_btn = ttk.Button(script_header, text="Đảo TTS → chuẩn tìm kiếm", command=trigger_normalize)
	normalize_btn.pack(side="right")

	def open_pinned_script():
		raw_text = script_txt.get("1.0", tk.END).strip()
		if not raw_text:
			return
		
		top = tk.Toplevel(root)
		top.title("Kịch Bản (Ghim)")
		top.attributes("-topmost", True)
		top.geometry("420x580")
		top.configure(bg=theme["panel"])
		
		ctrl_frame = ttk.Frame(top, style="Panel.TFrame")
		ctrl_frame.pack(side="top", fill="x", padx=8, pady=8)
		
		cur_size = pin_size_var.get() or 14
		pin_family = pin_font_var.get() or fonts["family"]
		
		t = tk.Text(
			top,
			wrap="word",
			bg=TEXT_BG,
			fg=TEXT_COLOR,
			font=(pin_family, cur_size),
			padx=16,
			pady=16,
			highlightthickness=1,
			highlightbackground=BORDER_COLOR,
			highlightcolor=ACCENT_COLOR,
			relief="flat",
			borderwidth=0,
			insertbackground=HIGHLIGHT_TEXT,
		)
		t.pack(side="left", fill="both", expand=True)
		
		scr = ttk.Scrollbar(top, orient="vertical", command=t.yview)
		t.configure(yscrollcommand=scr.set)
		scr.pack(side="right", fill="y")
		
		def update_font(*args):
			try:
				f = font_cb.get()
				s = int(size_spin.get())
				t.configure(font=(f, s))
				t.tag_configure("header", font=(f, s, "bold"))
				t.tag_configure("body", font=(f, s), spacing3=s)
				pin_font_var.set(f)
				pin_size_var.set(s)
				save_settings()
			except Exception:
				pass

		font_cb = ttk.Combobox(
			ctrl_frame,
			values=[fonts["family"], "Helvetica", "Arial", "Menlo", "Georgia"],
			width=15,
			state="readonly",
			textvariable=pin_font_var,
		)
		font_cb.pack(side="left", padx=(4, 2))
		font_cb.bind("<<ComboboxSelected>>", update_font)

		size_spin = ttk.Spinbox(ctrl_frame, from_=8, to=72, width=5, command=update_font, textvariable=pin_size_var)
		size_spin.pack(side="left", padx=(2, 4))
		size_spin.bind("<KeyRelease>", update_font)
		
		def open_footage_folder():
			root_dir = resume.get("project_root")
			if root_dir:
				folder = Path(root_dir) / "01_footage_selected"
				if folder.exists():
					os.system(f'open "{folder}"')
				else:
					os.system(f'open "{root_dir}"')

		folder_btn = ttk.Button(ctrl_frame, text="Mở Footage", command=open_footage_folder, width=12)
		folder_btn.pack(side="right", padx=2)
		
		def open_newsdrop():
			try:
				newsdrop_path = Path("/Applications/OB-NewsDrag.app")
				if newsdrop_path.exists():
					os.system(f'open "{newsdrop_path}"')
				else:
					messagebox.showwarning("Thiếu App", "Không tìm thấy OB-NewsDrag.app trong Applications!")
			except Exception as e:
				log(f"Lỗi mở OB-NewsDrag: {e}")

		newsdrop_btn = ttk.Button(ctrl_frame, text="Mở Vệ Tinh", command=open_newsdrop, width=12)
		newsdrop_btn.pack(side="right", padx=2)
		
		t.tag_configure("header", font=fonts["pin_header"], foreground=HIGHLIGHT_TEXT)
		t.tag_configure("body", font=fonts["pin"], foreground=TEXT_COLOR, spacing3=14)
		
		paragraphs = split_paragraphs(raw_text)
		for i, p in enumerate(paragraphs, start=1):
			doan_i, content = parse_doan_prefix(p)
			idx = doan_i if doan_i is not None else i
			t.insert(tk.END, f"ĐOẠN {idx}:\n", "header")
			t.insert(tk.END, f"{content}\n", "body")
			
		t.config(state="disabled")

	pin_btn = ttk.Button(script_header, text="Ghim cửa sổ trên cùng", command=open_pinned_script)
	pin_btn.pack(side="right", padx=(0, 8))

	script_text_container = ttk.Frame(script_frame, style="Card.TFrame")
	script_text_container.pack(fill="both", expand=True)

	script_txt = tk.Text(
		script_text_container,
		height=10,
		wrap="word",
		bg=TEXT_BG,
		fg=TEXT_COLOR,
		insertbackground=HIGHLIGHT_TEXT,
		relief="flat",
		bd=0,
		highlightthickness=1,
		highlightbackground=BORDER_COLOR,
		highlightcolor=ACCENT_COLOR,
		font=UI_FONT,
	)
	script_scroll = ttk.Scrollbar(script_text_container, orient="vertical", command=script_txt.yview)
	script_txt.configure(yscrollcommand=script_scroll.set)
	script_scroll.pack(side="right", fill="y")
	script_txt.pack(side="left", fill="both", expand=True)

	# Log Console
	log_frame = ttk.LabelFrame(right_panel, text="Nhật ký hệ thống", padding=10, style="Card.TLabelframe")
	log_frame.pack(fill="both", expand=True, pady=(0, 0))

	log_text_container = ttk.Frame(log_frame, style="Card.TFrame")
	log_text_container.pack(fill="both", expand=True)

	log_txt = tk.Text(
		log_text_container,
		height=14,
		wrap="word",
		bg=TEXT_BG,
		fg=theme["text_muted"],
		insertbackground=HIGHLIGHT_TEXT,
		relief="flat",
		bd=0,
		highlightthickness=1,
		highlightbackground=BORDER_COLOR,
		highlightcolor=ACCENT_COLOR,
		font=fonts["small"],
	)
	log_scroll = ttk.Scrollbar(log_text_container, orient="vertical", command=log_txt.yview)
	log_txt.configure(yscrollcommand=log_scroll.set)
	log_scroll.pack(side="right", fill="y")
	log_txt.pack(side="left", fill="both", expand=True)

	# Progress
	progress_frame = ttk.Frame(right_panel)
	progress_frame.pack(fill="x", pady=(10, 0))
	
	overall_step_var = tk.StringVar(value="Tổng quan: Đang chờ...")
	ttk.Label(progress_frame, textvariable=overall_step_var, style="Section.TLabel").pack(anchor="w")
	overall_pbar = ttk.Progressbar(progress_frame, mode="determinate")
	overall_pbar.pack(fill="x", pady=(2, 8))

	step_var = tk.StringVar(value="Chi tiết: Idle")
	ttk.Label(progress_frame, textvariable=step_var).pack(anchor="w")
	pbar = ttk.Progressbar(progress_frame, mode="determinate")
	pbar.pack(fill="x", pady=(2, 0))

	stop_flag = {"stop": False}

	def request_stop():
		stop_flag["stop"] = True
		log("[INFO] Stop requested — will stop after current paragraph.")

	def build_queries(paragraph: str) -> list[str]:
		if llm_enabled():
			qs = build_queries_with_cloud(paragraph, context=resume.get("script_context"))
			if qs:
				return qs
		return build_queries_rules(paragraph)

	def context_year() -> int | None:
		ctx = resume.get("script_context")
		if isinstance(ctx, dict):
			try:
				y = ctx.get("year")
				return int(y) if y is not None and str(y).strip() != "" else None
			except Exception:
				return None
		return None

	def run_pipeline(start_from_resume: bool, subfolder_name: str | None = None):
		need_gdl = bool(use_x_var.get())
		missing = preflight_missing_bins(check_gallery_dl=need_gdl)
		if missing:
			log("[SETUP] Thiếu công cụ: " + ", ".join(missing) + " — tự cài…")
			ok = run_auto_setup_dialog(root, log_fn=log, force=False, need_gallery_dl=need_gdl or True)
			missing = preflight_missing_bins(check_gallery_dl=need_gdl)
			if not ok or missing:
				messagebox.showerror(
					"Missing dependencies",
					"Thiếu công cụ: "
					+ ", ".join(missing)
					+ "\n\nApp đã thử tự cài nhưng chưa đủ.\n"
					"Thử «Cài dependencies» hoặc:\n"
					"brew install yt-dlp ffmpeg gallery-dl\n\n"
					"Log: ~/.newsfootage_hunter/app.log",
				)
				enable_run_buttons()
				return

		save_settings()

		project_folder = project_var.get().strip()
		if not project_folder:
			messagebox.showwarning("Missing folder", "Bạn chưa chọn Project folder.")
			enable_run_buttons()
			return

		if start_from_resume:
			if not resume.get("project_root"):
				messagebox.showwarning("No resume", "Không có job nào để resume.")
				enable_run_buttons()
				return
			project_root = Path(resume["project_root"])
			if not project_root.exists():
				messagebox.showwarning("No resume", "Project resume không tồn tại.")
				enable_run_buttons()
				return
			script = resume.get("script", "")
			start_para_idx = int(resume.get("paragraph_index", 0))
		else:
			script = script_txt.get("1.0", "end").strip()
			if not script:
				messagebox.showwarning("Missing script", "Bạn chưa dán kịch bản.")
				return
			if normalize_var.get():
				lines = script.splitlines()
				normalized_lines = [standardize_script_for_search_and_export(line) for line in lines]
				script = "\n".join(normalized_lines)

			folder_name = sanitize_folder_name(subfolder_name) if subfolder_name else suggest_project_subfolder_name(script)
			project_root = Path(project_folder) / folder_name
			(project_root / "01_footage_selected").mkdir(parents=True, exist_ok=True)
			(project_root / "03_metadata").mkdir(parents=True, exist_ok=True)
			(project_root / "03_metadata" / "script.txt").write_text(script, encoding="utf-8")
			start_para_idx = 0

		# update resume pointer
		resume["project_root"] = str(project_root)
		resume["script_context"] = None
		save_settings()

		# NOTE: global-context inference was moved INTO worker() below, so it runs
		# AFTER Cloud AI is turned on (otherwise it would always say "unavailable").
		paragraphs = split_paragraphs(script)
		# export HTML once at start
		try:
			export_numbered_script_html(project_root, paragraphs)
			log("Saved: 03_metadata/script_numbered.html")
		except Exception as e:
			log(f"[WARN] HTML export failed: {e}")

		# for each paragraph: search + download
		out_dir = project_root / "01_footage_selected"
		meta_dir = project_root / "03_metadata"
		csv_path = meta_dir / "footage_pack.csv"

		# init CSV
		if not csv_path.exists():
			with open(csv_path, "w", newline="", encoding="utf-8") as f:
				w = csv.DictWriter(f, fieldnames=[
					"paragraph_index",
					"paragraph_hint",
					"query",
					"youtube_url",
					"title",
					"channel",
					"upload_date",
					"duration_sec",
					"resolution_hint",
				])
				w.writeheader()

		def append_csv(rows: list[VideoCandidate]):
			if not rows:
				return
			with open(csv_path, "a", newline="", encoding="utf-8") as f:
				w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
				for r in rows:
					w.writerow(asdict(r))

		stop_flag["stop"] = False

		def worker():
			try:
				log(f"Project: {project_root}")
				set_progress("Running…", start_para_idx, max(1, len(paragraphs)))

				# Cooling: lower priority + fewer threads + cooldowns to keep the laptop cool.
				COOL["on"] = bool(cool_mode_var.get())
				COOL["download_conc"] = DEFAULT_COOL_DOWNLOAD_CONC
				COOL["sleep"] = DEFAULT_OLLAMA_SLEEP_SEC
				apply_cool_mode_env(COOL["on"], int(ollama_threads_var.get()))
				if COOL["on"]:
					log(f"[COOL] Cool mode BẬT — nice={COOL['nice']}, ollama_threads={int(ollama_threads_var.get())}, fragment song song ≤ {COOL['download_conc']}, nghỉ {COOL['sleep']}s giữa các lệnh nặng.")

				# Cloud AI: route all LLM reasoning to a free cloud model (no local heat).
				CLOUD["on"] = bool(use_cloud_var.get())
				CLOUD["provider"] = (cloud_provider_var.get().strip() or "groq")
				_prov = (CLOUD["provider"] or "").lower()
				_base_in = cloud_base_url_var.get().strip()
				if _prov == "openrouter":
					CLOUD["base_url"] = normalize_api_base_url(_base_in or DEFAULT_OPENROUTER_BASE)
				elif _prov == "custom":
					CLOUD["base_url"] = _base_in.rstrip("/")
				else:
					CLOUD["base_url"] = ""
				CLOUD["model"] = cloud_model_var.get().strip()
				CLOUD["search_model"] = llm_search_model_var.get().strip()
				CLOUD["keys"] = cloud_keys()
				CLOUD["idx"] = 0
				llm_web_state["idx"] = 0
				if CLOUD["on"] and CLOUD["keys"]:
					_base_note = f" @ {CLOUD['base_url']}" if CLOUD.get("base_url") else ""
					log(f"[CLOUD] AI đám mây BẬT — {CLOUD['provider']}{_base_note} ({CLOUD['model'] or 'model mặc định'}), {len(CLOUD['keys'])} key. Suy luận chạy trên server -> máy mát.")
					if (CLOUD["provider"] or "").strip().lower() == "ollama" and ollama_audit_matches_keys(CLOUD["keys"]):
						s = OLLAMA_AUDIT.get("summary") or {}
						ready_n = len(s.get("ready") or [])
						log(f"[CLOUD] Dùng kết quả quét Ollama gần nhất — {ready_n} key sẵn sàng, bỏ qua key hết quota.")
						apply_ollama_audit_rotation(CLOUD["keys"], logf=log)
					_test = cloud_generate("Trả lời đúng 1 từ: OK", CLOUD["model"], 20)
					if _test:
						log(f"[CLOUD] Gọi thử key OK (model: {CLOUD.get('model_ok') or CLOUD['model'] or 'mặc định'}) -> {(_test or '').strip()[:30]}")
					else:
						log(f"[CLOUD] ⚠ Gọi thử key THẤT BẠI — {CLOUD.get('last_error') or 'không rõ'}. App tạm dùng Rules.")

				_tk = tavily_keys()
				_was_tavily_blocked = bool(tavily_state.get("circuit_open"))
				reset_tavily_state(tavily_state)
				if use_tavily_var.get() and _tk:
					if _was_tavily_blocked:
						log("[TAVILY] Đã reset circuit breaker (phiên trước tất cả key lỗi — thử lại từ đầu).")
					log(f"[TAVILY] BẬT — {len(_tk)} key, tối đa {int(tavily_max_var.get())} kết quả/query.")
				elif use_tavily_var.get():
					log("[TAVILY] ⚠ Bật nhưng chưa có API key — chỉ dùng YouTube + X.com.")
				else:
					log("[TAVILY] TẮT — chỉ tìm qua YouTube (yt-dlp) + X.com. Bật ở tab «Web Search» nếu cần thêm nguồn.")

				_llm_web_ok = (
					use_llm_web_var.get()
					and _prov in ("openrouter", "custom")
					and bool(CLOUD.get("keys"))
					and bool(CLOUD.get("base_url") or _prov == "openrouter")
				)
				if use_llm_web_var.get():
					if _llm_web_ok:
						log(
							f"[LLM-WEB] BẬT — {_prov} · model {effective_llm_search_model(use_web_tool=_prov == 'openrouter')} · "
							f"tối đa {int(tavily_max_var.get())} link/query (dùng API tab Cloud AI)."
						)
					else:
						log("[LLM-WEB] ⚠ Bật nhưng thiếu cấu hình — chọn OpenRouter/Custom + Base URL + API key ở tab Cloud AI.")

				log_lock = threading.Lock()
				def ts_log(line: str):
					write_log_line(line)
					def _update():
						with log_lock:
							log_txt.insert("end", line + "\n")
							log_txt.see("end")
							root.update_idletasks()
					root.after(0, _update)

				# Global de-dup across the WHOLE project (not just within one paragraph).
				# Seeded from any existing CSV so resume / re-runs never repeat a video.
				global_seen = set()
				global_titles = set()
				global_seen_lock = threading.Lock()
				try:
					if csv_path.exists():
						with open(csv_path, newline="", encoding="utf-8") as _f:
							for _row in csv.DictReader(_f):
								_k = canonical_video_url(_row.get("youtube_url") or "") or (_row.get("youtube_url") or "")
								if _k:
									global_seen.add(_k)
								_tf = title_fingerprint(_row.get("title") or "")
								if _tf:
									global_titles.add(_tf)
						if global_seen:
							log(f"[DEDUP] Đã nạp {len(global_seen)} video đã tải trước đó để tránh trùng.")
				except Exception as e:
					log(f"[WARN] Lỗi nạp DEDUP từ CSV: {e}")

				global_tavily_lock = threading.Semaphore(4)
				global_llm_web_lock = threading.Semaphore(2)
				_llm_web_active = _llm_web_ok
				global_x_search_lock = threading.Semaphore(2)
				analysis_sem = threading.Semaphore(5)

				def analyze_paragraph_worker(i, result_queue):
					analysis_sem.acquire()
					try:
						if stop_flag["stop"]:
							return
						try:
							p = paragraphs[i]
							doan_i, content = parse_doan_prefix(p)
							doan_idx = doan_i if doan_i is not None else (i + 1)
							hint = paragraph_hint(content)

							ts_log(f"\n=== ĐOẠN {doan_idx} (PHÂN TÍCH): {hint} ===")

							article_urls = extract_article_urls(content)
							if article_urls:
								ts_log(f"  [SKIP] ĐOẠN {doan_idx}: có link bài báo — bỏ qua tải video")
								for u in article_urls:
									ts_log(f"    → {u}")
								result_queue.put({
									"index": i,
									"type": "article_skip",
									"urls": article_urls,
								})
								return
						
							# Parallel search
							qs = build_queries(content)
							qs = drop_vietnamese_queries(qs, logf=ts_log)
							if not qs:
								qs = ["Ukraine Russia war footage"]
							ts_log(f"Đoạn {doan_idx} Queries: {', '.join(qs)}")

							search_results = {}
							tavily_results = {}
							web_source_flags: dict[str, str] = {}
							bili_results = {}
							douyin_results = {}
							x_results = {}

							x_since, x_until = x_search.extract_event_date_range(content, context_year())
							x_qs = build_x_search_queries(content, youtube_queries=qs)
							cloud_xq = x_queries_with_cloud(content)
							if cloud_xq:
								for cq in cloud_xq:
									sq = x_search.simplify_x_query(cq, max_words=5)
									if sq and sq not in x_qs:
										x_qs.insert(0, sq)
							x_qs = [q for q in x_qs if q][:2]
							if x_since or x_until:
								ts_log(f"  [X] Lọc ngày: since={x_since or '—'} until={x_until or '—'}")

							def perform_search_and_tavily(query_str):
								try:
									res_data = run_yt_dlp_search(
										query_str,
										limit=int(results_per_query_var.get()),
										cookies_browser=cookies_browser_var.get().strip(),
									)
									search_results[query_str] = res_data
								except Exception as e:
									ts_log(f"[WARN] Search failed: {query_str} ({e})")
									search_results[query_str] = {}
							
								_web_max = int(tavily_max_var.get())
								if _llm_web_active:
									try:
										with global_llm_web_lock:
											tav = llm_web_search_rotate(
												query_str,
												_web_max,
												CLOUD.get("keys") or [],
												llm_web_state,
												logf=ts_log,
											)
										tavily_results[query_str] = tav or []
										if tav:
											web_source_flags[query_str] = "llm_web"
											ts_log(f"  [LLM-WEB] +{len(tav)} kết quả — {_safe_snip(query_str, 65)}")
									except Exception as e:
										ts_log(f"  [LLM-WEB] error: {e}")
										tavily_results[query_str] = []
								elif use_tavily_var.get() and tavily_keys():
									try:
										with global_tavily_lock:
											tav = tavily_search_rotate(
												query_str, 
												_web_max, 
												tavily_keys(), 
												tavily_state, 
												logf=ts_log
											)
										tavily_results[query_str] = tav or []
										if tav:
											web_source_flags[query_str] = "tavily"
											ts_log(f"  [TAVILY] +{len(tav)} kết quả — {_safe_snip(query_str, 65)}")
									except Exception as e:
										ts_log(f"  [TAVILY] error: {e}")
										tavily_results[query_str] = []
								
								if use_bilibili_var.get():
									try:
										bili_data = run_bilibili_search(
											query_str,
											limit=int(results_per_query_var.get())
										)
										bili_results[query_str] = bili_data
									except Exception as e:
										ts_log(f"[WARN] Bili search failed: {query_str} ({e})")
										bili_results[query_str] = {}
									
									if tavily_keys():
										try:
											with global_tavily_lock:
												dy_tav = tavily_search_rotate(
													query_str + " site:douyin.com", 
													int(results_per_query_var.get()), 
													tavily_keys(), 
													tavily_state, 
													logf=ts_log,
													web_search=True
												)
											douyin_results[query_str] = dy_tav
										except Exception as e:
											ts_log(f"  [DOUYIN] error: {e}")
											douyin_results[query_str] = []

							def perform_x_search_all():
								if not use_x_var.get() or not x_qs:
									return
								combined: list[dict] = []
								seen_x: set[str] = set()
								try:
									with global_x_search_lock:
										for query_str in x_qs:
											if len(combined) >= int(results_per_query_var.get()):
												break
											x_data = x_search.run_x_search(
												query_str,
												limit=int(results_per_query_var.get()),
												cookies_browser=cookies_browser_var.get().strip(),
												since=x_since,
												until=x_until,
												logf=ts_log,
											)
											for e in (x_data or {}).get("entries") or []:
												u = str(e.get("webpage_url") or "")
												if u and u not in seen_x:
													seen_x.add(u)
													combined.append(e)
											if combined:
												ts_log(f"  [X] Tìm thấy {len(combined)} video — dừng thử query khác")
												break
									x_results["__combined__"] = {"entries": combined, "query": " | ".join(x_qs)}
								except Exception as e:
									ts_log(f"  [X] search error: {e}")
									x_results["__combined__"] = {"entries": []}

							search_threads = []
							for q in qs:
								t = threading.Thread(target=perform_search_and_tavily, args=(q,), daemon=True)
								t.start()
								search_threads.append(t)
							if use_x_var.get():
								t = threading.Thread(target=perform_x_search_all, daemon=True)
								t.start()
								search_threads.append(t)
							for t in search_threads:
								t.join()

							picked_candidates = []
							seen_urls = set()
							merged_pool = []
							seen_pool = set()
							ctx_year = context_year()
						
							pool_cap = max(12, min(90, int(rerank_topn_var.get()) * 2)) if bool(rerank_var.get()) else max(12, min(90, int(results_per_query_var.get()) * 3))
							if COOL.get("on"):
								pool_cap = min(pool_cap, 18)

							for q in qs:
								data = search_results.get(q) or {}
								entries = data.get("entries") or []
								try:
									entries = sorted(entries, key=lambda x: str(x.get("upload_date") or ""), reverse=True)
								except Exception:
									pass

								added_this_q = 0
								for e in entries:
									if not isinstance(e, dict):
										continue
									if len(merged_pool) >= pool_cap:
										break
									url = e.get("webpage_url")
									key = canonical_video_url(url or "") or (url or "")
									tf = title_fingerprint(e.get("title") or "")
								
									with global_seen_lock:
										is_dup = key in global_seen or (tf and tf in global_titles)
								
									if not key or key in seen_pool or is_dup:
										continue

									dur = e.get("duration")
									if isinstance(dur, (int, float)):
										if int(dur) > MAX_DURATION_SECONDS:
											continue
									elif isinstance(dur, str) and ":" in dur:
										try:
											parts = dur.split(":")
											d = sum(int(x) * 60 ** i for i, x in enumerate(reversed(parts)))
											if d > MAX_DURATION_SECONDS:
												continue
											dur = d
										except:
											pass
									else:
										if len(merged_pool) > 0:
											continue

									up = str(e.get("upload_date") or "")
									if not in_year_window(up, ctx_year, window_years=DEFAULT_YEAR_WINDOW):
										continue

									seen_pool.add(key)
									merged_pool.append({
										"url": key,
										"title": str(e.get("title") or ""),
										"channel": str(e.get("channel") or e.get("uploader") or ""),
										"upload_date": up,
										"duration_sec": int(dur) if isinstance(dur, (int, float)) else -1,
										"resolution": str(e.get("resolution") or ""),
										"query": q,
										"platform": "youtube"
									})
									added_this_q += 1

								bili_data = bili_results.get(q) or {}
								b_entries = bili_data.get("entries") or []
								for e in b_entries:
									if not isinstance(e, dict):
										continue
									if len(merged_pool) >= pool_cap * 2:
										break
									url = e.get("webpage_url")
									key = canonical_video_url(url or "") or (url or "")
									tf = title_fingerprint(e.get("title") or "")
								
									with global_seen_lock:
										is_dup = key in global_seen or (tf and tf in global_titles)
								
									if not key or key in seen_pool or is_dup:
										continue

									dur = e.get("duration")
									if isinstance(dur, str) and ":" in dur:
										try:
											parts = dur.split(":")
											d = sum(int(x) * 60 ** i for i, x in enumerate(reversed(parts)))
											if d > MAX_DURATION_SECONDS:
												continue
											dur = d
										except:
											pass

									seen_pool.add(key)
									merged_pool.append({
										"url": key,
										"title": str(e.get("title") or ""),
										"channel": str(e.get("uploader") or ""),
										"upload_date": str(e.get("upload_date") or ""),
										"duration_sec": int(dur) if isinstance(dur, (int, float)) else -1,
										"resolution": "Unknown",
										"query": q,
										"platform": "bilibili"
									})

								# Douyin
								dy_data = douyin_results.get(q) or []
								for tr in dy_data:
									if not isinstance(tr, dict):
										continue
									if len(merged_pool) >= pool_cap * 2:
										break
									url = canonical_video_url(str(tr.get("url") or ""))
									tf = title_fingerprint(str(tr.get("title") or ""))
								
									with global_seen_lock:
										is_dup = url in global_seen or (tf and tf in global_titles)
								
									if not url or url in seen_pool or is_dup:
										continue

									seen_pool.add(url)
									merged_pool.append({
										"url": url,
										"title": str(tr.get("title") or ""),
										"channel": str(tr.get("content") or "")[:50],
										"upload_date": "",
										"duration_sec": -1,
										"resolution": "Unknown",
										"query": q,
										"platform": "douyin"
									})

								# Tavily
								tav = tavily_results.get(q) or []
								for tr in tav:
									if not isinstance(tr, dict):
										continue
									if len(merged_pool) >= pool_cap:
										break
									turl = canonical_video_url(str(tr.get("url") or ""))
								
									with global_seen_lock:
										is_dup = turl in global_seen or (title_fingerprint(tr.get("title") or "") in global_titles)
									
									if not turl or turl in seen_pool or is_dup:
										continue
									try:
										info = run_yt_dlp_info(turl, cookies_browser=cookies_browser_var.get().strip())
									except Exception:
										info = {}
									dur2 = info.get("duration")
									if isinstance(dur2, (int, float)) and int(dur2) > MAX_DURATION_SECONDS:
										continue
									up2 = str(info.get("upload_date") or "")
									if up2 and not in_year_window(up2, ctx_year, window_years=DEFAULT_YEAR_WINDOW):
										continue
									ttf = title_fingerprint(info.get("title") or tr.get("title") or "")
								
									with global_seen_lock:
										is_dup_title = ttf and ttf in global_titles
									
									if is_dup_title:
										continue
									seen_pool.add(turl)
									merged_pool.append({
										"url": turl,
										"title": str(info.get("title") or tr.get("title") or ""),
										"channel": str(info.get("channel") or info.get("uploader") or ""),
										"upload_date": up2,
										"duration_sec": int(dur2) if isinstance(dur2, (int, float)) else -1,
										"resolution": str(info.get("resolution") or ""),
										"query": q,
										"source": web_source_flags.get(q) or "tavily",
									})
									added_this_q += 1
								ts_log(f"  [POOL] Đoạn {doan_idx}: +{added_this_q} from: {_safe_snip(q, 70)} (pool={len(merged_pool)})")

							# X.com (Twitter) — merged once per paragraph
							x_added = 0
							for xq, x_data in (x_results or {}).items():
								x_entries = (x_data or {}).get("entries") or []
								for e in x_entries:
									if not isinstance(e, dict):
										continue
									if len(merged_pool) >= pool_cap * 2:
										break
									url = e.get("webpage_url")
									key = canonical_video_url(url or "") or (url or "")
									tf = title_fingerprint(e.get("title") or "")
									with global_seen_lock:
										is_dup = key in global_seen or (tf and tf in global_titles)
									if not key or key in seen_pool or is_dup:
										continue
									up = str(e.get("upload_date") or "")
									if up and not in_year_window(up, ctx_year, window_years=DEFAULT_YEAR_WINDOW):
										continue
									seen_pool.add(key)
									merged_pool.append({
										"url": key,
										"title": str(e.get("title") or ""),
										"channel": str(e.get("channel") or ""),
										"upload_date": up,
										"duration_sec": int(e.get("duration") or -1) if isinstance(e.get("duration"), (int, float)) else -1,
										"resolution": "Unknown",
										"query": xq,
										"platform": "x",
									})
									x_added += 1
							if x_added:
								ts_log(f"  [X] Đoạn {doan_idx}: +{x_added} video X.com (pool={len(merged_pool)})")

							if not merged_pool:
								ts_log(f"[WARN] Đoạn {doan_idx}: No candidates found.")
								result_queue.put({"index": i, "type": "none", "picked": []})
								return

							# Rerank
							ranked_pool = merged_pool
							if ai_mode_var.get() == "ollama" and bool(rerank_var.get()):
								model = (ollama_model_var.get().strip() or "llama3.1")
								ranked = rerank_candidates_with_ollama(
									paragraph=content,
									candidates=merged_pool,
									context=resume.get("script_context"),
									model=model,
								)
								if ranked:
									ranked_pool = ranked

							max_vids = int(videos_per_para_var.get())
							quotas = compute_source_quotas(
								max_vids,
								use_x=bool(use_x_var.get()),
								use_bilibili=bool(use_bilibili_var.get()),
							)
							yt_quota = quotas.get("youtube", max_vids)
							x_quota = quotas.get("x", 0)
							bili_quota = quotas.get("bilibili", 0)
							dy_quota = quotas.get("douyin", 0)
							if use_x_var.get():
								ts_log(
									f"  [QUOTA] Đoạn {doan_idx}: YouTube={yt_quota}, X.com={x_quota}"
									+ (f", Bili={bili_quota}, Douyin={dy_quota}" if use_bilibili_var.get() else "")
									+ f" (tổng {max_vids})"
								)

							yt_cands = [x for x in ranked_pool if x.get("platform", "youtube") in ("youtube",) or x.get("source") in ("tavily", "llm_web")]
							x_cands = [x for x in ranked_pool if x.get("platform") == "x"]
							bili_cands = [x for x in ranked_pool if x.get("platform") == "bilibili"]
							dy_cands = [x for x in ranked_pool if x.get("platform") == "douyin"]

							yt_picked_cands = yt_cands[:yt_quota * 3]
							x_picked_cands = x_cands[:x_quota * 3]
							bili_picked_cands = bili_cands[:bili_quota * 3]
							dy_picked_cands = dy_cands[:dy_quota * 3]

							final_pool = []
							picked_lists = [yt_picked_cands, x_picked_cands, bili_picked_cands, dy_picked_cands]
							max_len = max((len(pl) for pl in picked_lists), default=0)
							for i in range(max_len):
								if i < len(yt_picked_cands):
									final_pool.append(yt_picked_cands[i])
								if i < len(x_picked_cands):
									final_pool.append(x_picked_cands[i])
								if i < len(bili_picked_cands):
									final_pool.append(bili_picked_cands[i])
								if i < len(dy_picked_cands):
									final_pool.append(dy_picked_cands[i])

							rem = (max_vids * 3) - len(final_pool)
							if rem > 0:
								unused = (
									yt_cands[yt_quota * 3:]
									+ x_cands[x_quota * 3:]
									+ bili_cands[bili_quota * 3:]
									+ dy_cands[dy_quota * 3:]
								)
								final_pool.extend(unused[:rem])
							
							ranked_pool = final_pool

							# Judge
							if ai_mode_var.get() == "ollama" and bool(judge_var.get()):
								candidates_to_judge = ranked_pool[:max_vids*2]
								judge_results = [None] * len(candidates_to_judge)
							
								def judge_worker_thread(idx, item):
									url = str(item.get("url") or "")
									ts_log(f"  [JUDGE] Thẩm định video #{idx+1} đoạn {doan_idx}: {_safe_snip(item.get('title') or '', 50)}...")
									
									# Skip heavy info extraction for Bilibili / X
									if item.get("platform") in ("bilibili", "x"):
										judge_results[idx] = (True, 85, item)
										return
									
									try:
										info = run_yt_dlp_info(url, cookies_browser=cookies_browser_var.get().strip())
									except Exception as e:
										ts_log(f"  [JUDGE] Lỗi info video #{idx+1}: {e}")
										info = {}
								
									subs = ""
									if not COOL.get("on"):
										try:
											subs = fetch_best_subtitle_text(url, cookies_browser=cookies_browser_var.get().strip())
										except Exception:
											subs = ""
								
									cand = {
										"title": item.get("title"),
										"channel": item.get("channel"),
										"upload_date": item.get("upload_date"),
										"duration_sec": item.get("duration_sec"),
										"description": info.get("description") or info.get("fulltitle") or "",
										"subs_text": subs,
									}
								
									judge_val = editorial_judge_with_ollama(
										paragraph=content,
										candidate=cand,
										context=resume.get("script_context"),
										model=(ollama_model_var.get().strip() or "llama3.1"),
									)
									if judge_val:
										try:
											ok = bool(judge_val.get("ok"))
											sc = int(float(judge_val.get("score") or 0))
											why = _safe_snip(str(judge_val.get("why") or ""), 110)
											ts_log(f"  [JUDGE] Kết quả video #{idx+1} đoạn {doan_idx}: score={sc} ok={ok} — {why}")
											judge_results[idx] = (ok, sc, item)
										except Exception:
											judge_results[idx] = (True, 70, item)
									else:
										judge_results[idx] = (True, 70, item)

								j_threads = []
								for idx, item in enumerate(candidates_to_judge):
									t = threading.Thread(target=judge_worker_thread, args=(idx, item), daemon=True)
									t.start()
									j_threads.append(t)
								for t in j_threads:
									t.join()

								scored = [r for r in judge_results if r is not None]
								scored.sort(key=lambda r: int(r[1]) if r else 0, reverse=True)
								picked_counts: dict[str, int] = {}
								for res in scored:
									ok, score, item = res
									if not ok:
										continue
									if len(picked_candidates) >= max_vids:
										break
									bucket = platform_bucket(item)
									if not quota_allows_pick(bucket, picked_counts, quotas):
										continue
									url = str(item.get("url") or "")
									key = canonical_video_url(url) or url
									tf = title_fingerprint(item.get("title") or "")
									with global_seen_lock:
										is_dup = key in seen_urls or key in global_seen or (tf and tf in global_titles)
										if not is_dup:
											seen_urls.add(key)
											global_seen.add(key)
											if tf:
												global_titles.add(tf)
									if is_dup:
										continue
									picked_counts[bucket] = picked_counts.get(bucket, 0) + 1
									picked_candidates.append(VideoCandidate(
										paragraph_index=doan_idx,
										paragraph_hint=hint,
										query=str(item.get("query") or ""),
										youtube_url=key,
										title=str(item.get("title") or ""),
										channel=str(item.get("channel") or ""),
										upload_date=str(item.get("upload_date") or ""),
										duration_sec=int(item.get("duration_sec") or -1),
										resolution_hint=str(item.get("resolution") or ""),
										platform=str(item.get("platform") or "youtube")
									))
							else:
								picked_counts = {}
								for e2 in ranked_pool:
									if len(picked_candidates) >= max_vids:
										break
									bucket = platform_bucket(e2)
									if not quota_allows_pick(bucket, picked_counts, quotas):
										continue
									url = str(e2.get("url") or "")
									key = canonical_video_url(url) or url
									tf = title_fingerprint(e2.get("title") or "")
									with global_seen_lock:
										is_dup = key in seen_urls or key in global_seen or (tf and tf in global_titles)
										if not is_dup:
											seen_urls.add(key)
											global_seen.add(key)
											if tf:
												global_titles.add(tf)
									if is_dup:
										continue
									picked_counts[bucket] = picked_counts.get(bucket, 0) + 1
									picked_candidates.append(VideoCandidate(
										paragraph_index=doan_idx,
										paragraph_hint=hint,
										query=str(e2.get("query") or ""),
										youtube_url=key,
										title=str(item.get("title") or ""),
										channel=str(e2.get("channel") or ""),
										upload_date=str(item.get("upload_date") or ""),
										duration_sec=int(e2.get("duration_sec") or -1),
										resolution_hint=str(e2.get("resolution") or ""),
										platform=str(e2.get("platform") or "youtube")
									))

							result_queue.put({
								"index": i,
								"type": "video",
								"picked": picked_candidates
							})
						except Exception as ex:
							ts_log(f"[ERROR] Phân tích Đoạn {i+1} lỗi: {ex}")
							result_queue.put({"index": i, "type": "error", "error": str(ex)})
					finally:
						analysis_sem.release()

				completed_set = set(resume.get("completed_paragraphs", [])) if start_from_resume else set()
				paragraphs_to_process = [idx for idx in range(len(paragraphs)) if idx not in completed_set]
				num_expected = len(paragraphs_to_process)
				
				if num_expected > 0:
					set_overall_progress(f"0 / {num_expected} ĐOẠN", 0, num_expected)
					download_queue = queue.Queue()
					
					# Spawn producer threads (one per remaining paragraph, concurrent limit is 5 via Semaphore)
					for idx in paragraphs_to_process:
						t = threading.Thread(target=analyze_paragraph_worker, args=(idx, download_queue), daemon=True)
						t.start()
					
					# Consumer loop running synchronously in the background worker thread
					completed_count = 0
					analyzed_count = 0
					while completed_count < num_expected:
						if stop_flag["stop"]:
							break
						try:
							res = download_queue.get(timeout=1)
						except queue.Empty:
							continue
						
						analyzed_count += 1
						def update_analyzed(a=analyzed_count, c=completed_count, n=num_expected):
							set_overall_progress(f"Đã phân tích: {a}/{n} | Tải xong: {c}/{n}", a, n)
						root.after(0, update_analyzed)
						
						i = res["index"]
						p = paragraphs[i]
						doan_i, content_str = parse_doan_prefix(p)
						doan_idx = doan_i if doan_i is not None else (i + 1)
						
						if res["type"] == "video":
							picked = res["picked"]
							if picked:
								append_csv(picked)
								log(f"Đã chọn {len(picked)} video cho ĐOẠN {doan_idx}. Đang tải ngầm…")
								set_progress(f"ĐOẠN {doan_idx}: đang tải", 0, len(picked))
								
								para_dir = out_dir
								def download_task(idx, c):
									if stop_flag["stop"]:
										return
									j = idx + 1
									ts_log(f"DL {j}/{len(picked)} (Đoạn {doan_idx}): {c.youtube_url}")
									
									duration_min = (c.duration_sec or 0) / 60.0
									dl_threads = 32 if duration_min >= 20.0 else 16
									
									if c.platform == "douyin":
										ts_log(f"  -> [DOUYIN] Đang gửi request tới Comet extension (đảm bảo Comet đang mở trang Douyin)...")
										url_to_fetch = c.youtube_url.split('?')[0].split('/')[-1] if '/video/' in c.youtube_url else c.youtube_url
										direct_url = douyin_ws.request_douyin_url(url_to_fetch)
										if direct_url:
											import requests
											temp_mp4 = para_dir / f"douyin_{idx}.mp4"
											try:
												ts_log(f"  -> [DOUYIN] Đang tải MP4 trực tiếp...")
												r = requests.get(direct_url, stream=True, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
												r.raise_for_status()
												with open(temp_mp4, 'wb') as f:
													for chunk in r.iter_content(chunk_size=8192):
														f.write(chunk)
												code = 0
												out = "Douyin downloaded via WebSocket."
											except Exception as e:
												code = 1
												out = f"Lỗi tải Douyin: {e}"
												ts_log(f"  -> [DOUYIN] {out}")
										else:
											code = 1
											out = "Không lấy được link Douyin từ tiện ích Comet."
											ts_log(f"  -> [DOUYIN] {out}")
									else:
										code, out = download_one(
											c.youtube_url,
											para_dir,
											cookies_browser=cookies_browser_var.get().strip(),
											threads=dl_threads,
											human_mode=bool(human_mode_var.get()),
										)
									if code != 0:
										ts_log(f"  -> SKIP (download failed) for {c.youtube_url}")
									else:
										renamed = rename_downloaded(para_dir, c)
										if renamed:
											ts_log(f"  -> Named: {renamed.name}")
									
									def update_progress(done_count=j, doan=doan_idx):
										set_progress(f"ĐOẠN {doan}: đang tải", done_count, len(picked))
									root.after(0, update_progress)

								with concurrent.futures.ThreadPoolExecutor(max_workers=3) as dl_executor:
									futures = [dl_executor.submit(download_task, idx, c) for idx, c in enumerate(picked)]
									concurrent.futures.wait(futures)

								log(f"[OK] Done ĐOẠN {doan_idx}. Bạn có thể import folder: {out_dir}")
							else:
								log(f"[WARN] No candidates found for ĐOẠN {doan_idx}.")
								
						elif res["type"] == "article_skip":
							urls = res.get("urls") or []
							link_note = urls[0] if urls else ""
							log(f"[SKIP] ĐOẠN {doan_idx}: link bài báo — không tải video. {link_note}")
							with global_seen_lock:
								article_links = dict(resume.get("article_links") or {})
								article_links[str(doan_idx)] = urls[0] if urls else ""
								resume["article_links"] = article_links
						elif res["type"] == "none":
							log(f"[WARN] No candidates found for ĐOẠN {doan_idx}.")
						elif res["type"] == "error":
							log(f"[ERROR] Phân tích Đoạn {doan_idx} thất bại: {res.get('error')}")
						
						with global_seen_lock:
							completed_set.add(i)
							resume["completed_paragraphs"] = sorted(list(completed_set))
							uncompleted = [idx for idx in range(len(paragraphs)) if idx not in completed_set]
							resume["paragraph_index"] = min(uncompleted) if uncompleted else len(paragraphs)
							resume["script"] = script
							save_resume_state(project_root, resume["paragraph_index"], script)
						
						completed_count += 1
						
						def update_overall(a=analyzed_count, c=completed_count, n=num_expected):
							set_overall_progress(f"Đã phân tích: {a}/{n} | Tải xong: {c}/{n}", a, n)
						root.after(0, update_overall)
						
						download_queue.task_done()

				if stop_flag["stop"]:
					messagebox.showinfo("Stopped", f"Đã dừng. Output ở:\n{project_root}")
					return
				
				log("HOÀN THÀNH.")
				set_progress("Hoàn thành", 100, 100)
				messagebox.showinfo("Done", f"Xong rồi! Output ở:\n{project_root}")
			except Exception as e:
				import traceback
				tb = traceback.format_exc()
				log(f"[ERROR] {e}")
				write_log_line(f"[TRACEBACK]\n{tb}")
				set_progress("Lỗi", 0, 100)
				messagebox.showerror("Error", str(e))
			finally:
				try:
					run_btn.state(["!disabled"])
				except Exception:
					pass
				try:
					resume_btn.state(["!disabled"])
				except Exception:
					pass

		threading.Thread(target=worker, daemon=True).start()

	def enable_run_buttons():
		try:
			run_btn.state(["!disabled"])
		except Exception:
			pass
		try:
			resume_btn.state(["!disabled"])
		except Exception:
			pass

	def start_run():
		try:
			run_btn.state(["disabled"])
		except Exception:
			pass

		project_folder = project_var.get().strip()
		if not project_folder:
			messagebox.showwarning("Missing folder", "Bạn chưa chọn Project folder.")
			enable_run_buttons()
			return

		script = script_txt.get("1.0", "end").strip()
		if not script:
			messagebox.showwarning("Missing script", "Bạn chưa dán kịch bản.")
			enable_run_buttons()
			return

		default_folder = suggest_project_subfolder_name(script)
		folder_name = ask_project_subfolder_dialog(root, default_folder)
		if not folder_name:
			enable_run_buttons()
			return

		log("[INFO] RUN")
		run_pipeline(start_from_resume=False, subfolder_name=folder_name)

	def start_resume():
		try:
			run_btn.state(["disabled"])
		except Exception:
			pass
		log("[INFO] RESUME")
		run_pipeline(start_from_resume=True)

	# Buttons
	run_btn = ttk.Button(buttons, text="CHẠY", command=start_run, style="Sidebar.Accent.TButton")
	run_btn.pack(side="left", fill="x", expand=True, padx=(0, 3))
	resume_btn = ttk.Button(buttons, text="TIẾP TỤC", command=start_resume, state=("normal" if resume.get("project_root") else "disabled"), style="Sidebar.Accent.TButton")
	resume_btn.pack(side="left", fill="x", expand=True, padx=(3, 3))
	stop_btn = ttk.Button(buttons, text="DỪNG", command=request_stop, style="Sidebar.Stop.TButton")
	stop_btn.pack(side="left", fill="x", expand=True, padx=(3, 0))

	def on_close():
		save_settings()
		root.destroy()

	root.protocol("WM_DELETE_WINDOW", on_close)

	hint = (
		f"v{APP_VERSION} · Mỗi ĐOẠN: tìm (YT + X) → tải video (≤30p) → đoạn tiếp.\n"
		"X.com: bật nguồn X, đăng nhập Chrome, chọn cookies chrome.\n"
		"Tự cài yt-dlp/ffmpeg/gallery-dl · Tự cập nhật qua GitHub Releases."
	)
	tk.Label(
		left_panel,
		text=hint,
		font=fonts["compact"],
		fg=theme["text_muted"],
		bg=theme["panel_alt"],
		wraplength=440,
		justify="left",
	).pack(anchor="w", padx=2, pady=(6, 0))

	def _startup_tasks():
		miss = preflight_missing_bins(check_gallery_dl=True)
		if miss:
			log("[SETUP] Mở app — thiếu: " + ", ".join(miss))
			run_auto_setup_dialog(root, log_fn=log, force=False, need_gallery_dl=True)
		else:
			paths = auto_setup.which_map()
			log("[OK] Tools: " + ", ".join(f"{k}={v}" for k, v in paths.items() if v))
		if auto_check_update_var.get():
			root.after(1200, lambda: do_check_update(silent=True))

	root.after(400, _startup_tasks)
	root.mainloop()


if __name__ == "__main__":
	ui()
