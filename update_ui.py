import re

with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Fonts:
content = content.replace('font=("Helvetica Neue", 10)', 'font=(".AppleSystemUIFont", 11)')
content = content.replace('font=("Helvetica Neue", 10, "bold")', 'font=(".AppleSystemUIFont", 11, "bold")')
content = content.replace('font=("Helvetica Neue", 9, "bold")', 'font=(".AppleSystemUIFont", 11, "bold")')
content = content.replace('font=("Helvetica Neue", 11)', 'font=(".AppleSystemUIFont", 12)')
content = content.replace('font=("Menlo", 10)', 'font=(".AppleSystemUIFont", 11)')

# 2. Labels & Strings:
content = content.replace('text="Project folder:"', 'text="Thư mục lưu trữ (Project folder):"')
content = content.replace('text="Choose…"', 'text="Chọn..."')
content = content.replace('text="Results/query:"', 'text="Số kết quả / lần tìm:"')
content = content.replace('text="Download threads:"', 'text="Số luồng tải xuống:"')
content = content.replace('text="Use cookies from:"', 'text="Dùng cookies từ:"')
content = content.replace('text="Human throttling"', 'text="Mô phỏng người dùng"')
content = content.replace('text=" Tùy chọn chế độ "', 'text=" Tùy chọn chế độ "')
content = content.replace('text="Auto-normalize phonetic words"', 'text="Tự động chuẩn hóa phiên âm"')
content = content.replace('text="Heavy mode: AI re-rank candidates"', 'text="Chế độ nặng: AI sắp xếp lại kết quả"')
content = content.replace('text="Top-N:"', 'text="Lấy Top:"')
content = content.replace('text="Editor mode: judge with subtitles (strict)"', 'text="Chế độ Editor: Kiểm tra gắt gao qua phụ đề"')
content = content.replace('text="Cool mode: chống nóng CPU (ít luồng)"', 'text="Chế độ mát máy: Chống nóng CPU"')
content = content.replace('text="Threads:"', 'text="Số luồng:"')
content = content.replace('text=" Phương thức xử lý chính (Brain) "', 'text=" Phương thức xử lý chính (Brain) "')
content = content.replace('text="Rules (Chạy offline theo bộ quy tắc)"', 'text="Rules (Chạy offline theo bộ quy tắc)"')
content = content.replace('text="Cloud AI (Sử dụng trí tuệ nhân tạo LLM)"', 'text="Cloud AI (Sử dụng trí tuệ nhân tạo LLM)"')
content = content.replace('text=" Paste script (mỗi dòng = 1 ĐOẠN): "', 'text=" Dán kịch bản vào đây (mỗi dòng = 1 ĐOẠN): "')
content = content.replace('text="Paste script (mỗi dòng = 1 ĐOẠN):"', 'text="Dán kịch bản vào đây (mỗi dòng = 1 ĐOẠN):"')
content = content.replace('text="Ghim màn hình"', 'text="Ghim cửa sổ trên cùng"')

content = content.replace('text="RUN (gối đầu)"', 'text="CHẠY (Tải trước gối đầu)"')
content = content.replace('text="START/RESUME"', 'text="BẮT ĐẦU / TIẾP TỤC"')
content = content.replace('text="STOP"', 'text="DỪNG LẠI"')

# 3. Add Overall Progress Bar & update set_progress
old_progress_ui = '''	# Progress
	progress_frame = ttk.Frame(right_panel)
	progress_frame.pack(fill="x", pady=(10, 0))
	step_var = tk.StringVar(value="Idle")
	ttk.Label(progress_frame, textvariable=step_var).pack(anchor="w")
	pbar = ttk.Progressbar(progress_frame, mode="determinate")
	pbar.pack(fill="x", pady=(4, 0))'''

new_progress_ui = '''	# Progress
	progress_frame = ttk.Frame(right_panel)
	progress_frame.pack(fill="x", pady=(10, 0))
	
	overall_step_var = tk.StringVar(value="Tổng quan: Đang chờ...")
	ttk.Label(progress_frame, textvariable=overall_step_var, font=(".AppleSystemUIFont", 11, "bold")).pack(anchor="w")
	overall_pbar = ttk.Progressbar(progress_frame, mode="determinate")
	overall_pbar.pack(fill="x", pady=(2, 8))

	step_var = tk.StringVar(value="Chi tiết: Idle")
	ttk.Label(progress_frame, textvariable=step_var).pack(anchor="w")
	pbar = ttk.Progressbar(progress_frame, mode="determinate")
	pbar.pack(fill="x", pady=(2, 0))'''

content = content.replace(old_progress_ui, new_progress_ui)

old_set_progress = '''	def set_progress(step: str, value=None, maximum=None):
		step_var.set(step)
		if maximum is not None:
			pbar["maximum"] = maximum
		if value is not None:
			pbar["value"] = value
		root.update_idletasks()'''

new_set_progress = '''	def set_progress(step: str, value=None, maximum=None):
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
		root.update_idletasks()'''

content = content.replace(old_set_progress, new_set_progress)

# 4. Update the worker loop to use set_overall_progress
# A. At initialization
old_init_progress = '''				if num_expected > 0:
					download_queue = queue.Queue()'''

new_init_progress = '''				if num_expected > 0:
					set_overall_progress(f"0 / {num_expected} ĐOẠN", 0, num_expected)
					download_queue = queue.Queue()'''

content = content.replace(old_init_progress, new_init_progress)

# B. At completion step
old_completed_count = '''						completed_count += 1
						ts_log(f"\\n✅ HOÀN THÀNH ĐOẠN {doan_idx}. (Đã xong {completed_count}/{num_expected} đoạn chờ xử lý)")'''

new_completed_count = '''						completed_count += 1
						set_overall_progress(f"{completed_count} / {num_expected} ĐOẠN", completed_count, num_expected)
						ts_log(f"\\n✅ HOÀN THÀNH ĐOẠN {doan_idx}. (Đã xong {completed_count}/{num_expected} đoạn chờ xử lý)")'''

content = content.replace(old_completed_count, new_completed_count)

with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated app.py successfully!")
