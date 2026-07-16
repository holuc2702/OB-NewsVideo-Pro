import tkinter as tk
from tkinter import ttk
root = tk.Tk()
style = ttk.Style(root)
style.theme_use('clam')
print(style.theme_names())
