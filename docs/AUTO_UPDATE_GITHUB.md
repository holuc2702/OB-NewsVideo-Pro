# Auto-update qua GitHub Releases

App kiểm tra bản mới từ:

- Repo: `holuc2702/OB-NewsVideo-Pro`
- API: `GET /repos/holuc2702/OB-NewsVideo-Pro/releases/latest`
- Version local: hằng `APP_VERSION` trong `auto_update.py` / hiển thị trong app

## 1. Chuẩn bị repo

Repo hiện có thể **private** hoặc **chưa public** (API trả 404 nếu không auth).

### Public (đơn giản nhất)

1. Tạo repo `OB-NewsVideo-Pro` (public)
2. Publish **Release** + file zip (xem bên dưới)
3. User không cần token

### Private

1. Tạo Fine-grained hoặc classic PAT với quyền `contents: read` (và access repo)
2. User dán token trong app (settings) **hoặc** set env:

```bash
export GITHUB_TOKEN=ghp_xxx
```

Token được lưu trong `~/.newsfootage_hunter/settings.json` key `github_token` nếu user nhập trong UI.

## 2. Phát hành bản mới (bạn — dev)

```bash
# 1) Tăng version trong auto_update.py  (vd 1.2.0)
# 2) Build
cd "/path/to/project"
source .venv/bin/activate   # nếu có
pyinstaller --noconfirm OB-NewsVideo.spec

# 3) Zip .app (macOS)
cd dist
ditto -c -k --sequesterRsrc --keepParent OB-NewsVideo.app OB-NewsVideo-macOS.zip

# 4) Tạo GitHub Release
#    Tag: v1.2.0  (hoặc 1.2.0 — app tự bỏ chữ v)
#    Asset: OB-NewsVideo-macOS.zip
```

Dùng `gh` CLI:

```bash
gh release create v1.2.0 dist/OB-NewsVideo-macOS.zip \
  --repo holuc2702/OB-NewsVideo-Pro \
  --title "OB-NewsVideo 1.2.0" \
  --notes "Changelog..."
```

## 3. Luồng trên máy user

1. Mở app → (tuỳ chọn) check update im lặng
2. Có bản mới → hỏi: *Có muốn cập nhật không?*
3. Đồng ý → tải zip → giải nén → script helper:
   - đợi app thoát
   - backup `.app` cũ
   - thay bằng `.app` mới
   - `xattr -dr com.apple.quarantine`
   - `open` app mới
4. Log: `~/.newsfootage_hunter/update.log`

## 4. Lưu ý

| Vấn đề | Cách xử lý |
|--------|------------|
| Gatekeeper chặn app | User: System Settings → Privacy → Open Anyway; app đã cố gỡ quarantine |
| Chạy từ `python3 app.py` | Không tự replace `.app` — chỉ báo có bản mới + link release |
| Asset `.dmg` | Chưa auto-replace; hãy upload **zip chứa .app** |
| 404 Not Found | Repo private/sai tên, hoặc chưa có Release — kiểm tra tag + asset |

## 5. Kiểm thử nhanh

```bash
python3 -c "from auto_update import check_for_update, APP_VERSION; print(APP_VERSION, check_for_update())"
```
