import re

with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

jina_ui = '''	# -----------------------------------------------------
	# Right Panel Controls (Script Input & Log Console)
	# -----------------------------------------------------

	# Jina Reader Fetch URL
	url_frame = ttk.Frame(right_panel)
	url_frame.pack(fill="x", pady=(0, 6))
	ttk.Label(url_frame, text="Lấy kịch bản từ bài báo (Nhập Link):", font=(".AppleSystemUIFont", 11, "bold")).pack(side="left")
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

	script_frame = ttk.Frame(right_panel)'''

# Replace the beginning of Right Panel Controls
old_right_panel = '''	# -----------------------------------------------------
	# Right Panel Controls (Script Input & Log Console)
	# -----------------------------------------------------

	script_frame = ttk.Frame(right_panel)'''

if old_right_panel in content:
    content = content.replace(old_right_panel, jina_ui)
    with open('app.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Patched Jina successfully")
else:
    print("Could not find old_right_panel")
