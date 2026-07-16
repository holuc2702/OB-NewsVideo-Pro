#!/usr/bin/env bash
# Build OB-NewsVideo Pro.app + zip + push + GitHub Release
#
# One-time:
#   brew install gh && gh auth login
#
# Every release:
#   1) Bump APP_VERSION in auto_update.py
#   2) ./build_release.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# Prefer owner from auto_update.py so app check-update matches published releases
DEFAULT_OWNER=$(python3 -c "import re; from pathlib import Path; t=Path('auto_update.py').read_text(encoding='utf-8'); o=re.search(r'GITHUB_OWNER\\s*=\\s*[\"\\']([^\"\\']+)', t); r=re.search(r'GITHUB_REPO\\s*=\\s*[\"\\']([^\"\\']+)', t); print(f\"{(o.group(1) if o else 'holuc272')}/{(r.group(1) if r else 'OB-NewsVideo-Pro')}\")")
REPO="${GITHUB_REPO:-$DEFAULT_OWNER}"
APP_NAME="OB-NewsVideo Pro"
APP_BUNDLE="OB-NewsVideo Pro.app"
BUILD_ONLY=0
NOTES=""
SKIP_PUSH=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-only) BUILD_ONLY=1; shift ;;
    --skip-push) SKIP_PUSH=1; shift ;;
    --notes) NOTES="${2:-}"; shift 2 ;;
    --repo) REPO="${2:-}"; shift 2 ;;
    -h|--help)
      echo "Usage: GH_TOKEN=ghp_xxx ./build_release.sh [--build-only] [--skip-push] [--notes TEXT]"
      exit 0
      ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

echo "==> Project: $ROOT"
echo "==> Repo:    $REPO"

VERSION=$(python3 -c "import re; from pathlib import Path; t=Path('auto_update.py').read_text(encoding='utf-8'); m=re.search(r'APP_VERSION\\s*=\\s*[\"\\']([^\"\\']+)[\"\\']', t); assert m, 'APP_VERSION missing'; print(m.group(1).strip().lstrip('v'))")
TAG="v${VERSION}"
echo "==> Version: $VERSION  (tag $TAG)"

if ! command -v python3 >/dev/null; then
  echo "ERROR: python3 not found"; exit 1
fi
if ! command -v pyinstaller >/dev/null; then
  echo "==> Installing pyinstaller..."
  python3 -m pip install -U pyinstaller
fi
if [[ "$BUILD_ONLY" -eq 0 ]]; then
  if ! command -v gh >/dev/null; then
    echo "ERROR: gh missing. Run: brew install gh"
    exit 1
  fi
  # Accept either interactive login OR GH_TOKEN / GITHUB_TOKEN env
  if [[ -n "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ]]; then
    export GH_TOKEN="${GH_TOKEN:-$GITHUB_TOKEN}"
    export GITHUB_TOKEN="$GH_TOKEN"
    echo "==> Using GH_TOKEN from environment"
  elif ! gh auth status >/dev/null 2>&1; then
    echo "ERROR: gh not logged in."
    echo "  Option A: gh auth login"
    echo "  Option B: GH_TOKEN=ghp_xxx ./build_release.sh"
    exit 1
  fi
  # Verify token can talk to API (read:org not required when GH_TOKEN is set)
  if ! gh api user --jq .login >/dev/null 2>&1; then
    echo "ERROR: GitHub token invalid or missing scopes (need 'repo')."
    exit 1
  fi
  GH_USER=$(gh api user --jq .login)
  echo "==> GitHub user: $GH_USER"
fi

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

echo "==> PyInstaller build..."
rm -rf build/OB-NewsVideo build/OBNewsVideoPro \
  "dist/OBNewsVideoPro" "dist/OB-NewsVideo Pro" "dist/OB-NewsVideo Pro.app" \
  dist/OB-NewsVideo 2>/dev/null || true

pyinstaller --noconfirm OB-NewsVideo.spec

APP_PATH="dist/${APP_BUNDLE}"
if [[ ! -d "$APP_PATH" ]]; then
  echo "ERROR: missing $APP_PATH after build"
  ls -la dist || true
  exit 1
fi

ZIP_NAME="OB-NewsVideo-Pro-macOS-v${VERSION}.zip"
ZIP_PATH="dist/${ZIP_NAME}"
echo "==> Zipping -> $ZIP_PATH"
rm -f "$ZIP_PATH"
(
  cd dist
  ditto -c -k --sequesterRsrc --keepParent "${APP_BUNDLE}" "${ZIP_NAME}"
)

SIZE=$(du -h "$ZIP_PATH" | awk '{print $1}')
echo "==> Zip size: $SIZE"
echo "    $ZIP_PATH"

if [[ "$BUILD_ONLY" -eq 1 ]]; then
  echo "==> Done (build-only)."
  echo "    Upload: gh release create $TAG \"$ZIP_PATH\" --repo $REPO --title \"$APP_NAME $VERSION\" --generate-notes"
  exit 0
fi

if [[ "$SKIP_PUSH" -eq 0 ]]; then
  if [[ ! -d .git ]]; then
    echo "==> git init + remote"
    git init
    git branch -M main
    git remote add origin "https://github.com/${REPO}.git" 2>/dev/null || \
      git remote set-url origin "https://github.com/${REPO}.git"
  fi

  if [[ ! -f .gitignore ]]; then
    printf '%s\n' \
      '.venv/' '.venv_py312/' '__pycache__/' '*.pyc' '.DS_Store' \
      'build/' 'dist/' 'logs/' '*.log' \
      > .gitignore
  fi

  git add -A
  if git diff --cached --quiet; then
    echo "==> No source changes to commit"
  else
    git commit -m "Release ${TAG}: OB-NewsVideo Pro auto-setup + auto-update" || true
  fi

  echo "==> Push source -> origin main"
  # Prefer GH_TOKEN so we don't hit wrong Keychain account (holuc2702 vs holuc272)
  PUSH_OK=0
  if [[ -n "${GH_TOKEN:-}" ]]; then
    git remote set-url origin "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git"
    if git push -u origin main; then
      PUSH_OK=1
    fi
    git remote set-url origin "https://github.com/${REPO}.git"
  else
    if git push -u origin main; then
      PUSH_OK=1
    fi
  fi
  if [[ "$PUSH_OK" -eq 1 ]]; then
    echo "    Push OK"
  else
    echo "WARN: git push failed. Repo may need an initial commit via GH_TOKEN."
    echo "      Continuing with Release upload if repo already has commits..."
  fi
fi

if [[ -z "$NOTES" ]]; then
  NOTES=$(printf '%s\n' \
    "OB-NewsVideo Pro ${VERSION}" \
    "" \
    "- Auto-setup: Homebrew + yt-dlp + ffmpeg + gallery-dl" \
    "- Auto-update via GitHub Releases" \
    "- YouTube + X.com + Bilibili pipeline" \
    "- Asset: ${ZIP_NAME}" \
    "" \
    "Install: unzip, move app to Applications, open it." \
    "If blocked: right-click Open, or xattr -dr com.apple.quarantine \"${APP_BUNDLE}\"")
fi

echo "==> Publish GitHub Release $TAG"
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo "    Release $TAG exists -> replace asset"
  gh release upload "$TAG" "$ZIP_PATH" --repo "$REPO" --clobber
else
  gh release create "$TAG" "$ZIP_PATH" \
    --repo "$REPO" \
    --title "${APP_NAME} ${VERSION}" \
    --notes "$NOTES"
fi

echo ""
echo "DONE"
echo "  Release: https://github.com/${REPO}/releases/tag/${TAG}"
echo "  Asset:   ${ZIP_NAME}"
echo ""
