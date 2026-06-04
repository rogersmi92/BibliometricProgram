from __future__ import annotations

import tkinter as tk
from tkinter import ttk


def main() -> None:
    root = tk.Tk()
    root.title("Minimal Tk Paint Test")
    root.geometry("900x600")
    root.configure(bg="red")
    root.grid_rowconfigure(1, weight=1)
    root.grid_columnconfigure(0, weight=1)

    tk.Label(
        root,
        text="ROOT PAINT TEST - yellow tk.Label on red root",
        bg="yellow",
        fg="black",
        font=("Arial", 24, "bold"),
    ).grid(row=0, column=0, sticky="ew")

    style = ttk.Style(root)
    print(f"[minimal_paint_test] ttk themes available: {style.theme_names()}", flush=True)
    style.theme_use("clam")
    print(f"[minimal_paint_test] ttk theme in use: {style.theme_use()}", flush=True)
    style.configure("Paint.TNotebook", background="magenta")
    style.configure("Paint.TNotebook.Tab", background="yellow", foreground="black", padding=(12, 6))
    style.configure("Paint.TFrame", background="lime")
    style.configure("Paint.TLabel", background="lime", foreground="black")

    notebook = ttk.Notebook(root, style="Paint.TNotebook")
    notebook.grid(row=1, column=0, sticky="nsew", padx=20, pady=20)

    tk_tab = tk.Frame(notebook, bg="blue")
    ttk_tab = ttk.Frame(notebook, style="Paint.TFrame")
    notebook.add(tk_tab, text="Plain tk.Frame tab")
    notebook.add(ttk_tab, text="ttk.Frame tab")

    tk.Label(
        tk_tab,
        text="PLAIN TK TAB PAINT TEST",
        bg="orange",
        fg="black",
        font=("Arial", 22, "bold"),
    ).pack(fill="x", padx=20, pady=20)
    tk.Button(tk_tab, text="Plain tk.Button").pack(padx=20, pady=20)

    tk.Label(
        ttk_tab,
        text="TTK TAB WITH PLAIN TK LABEL",
        bg="cyan",
        fg="black",
        font=("Arial", 22, "bold"),
    ).pack(fill="x", padx=20, pady=20)
    ttk.Label(ttk_tab, text="ttk.Label inside ttk tab", style="Paint.TLabel").pack(fill="x", padx=20, pady=20)
    ttk.Button(ttk_tab, text="ttk.Button").pack(padx=20, pady=20)

    root.update_idletasks()
    print(f"[minimal_paint_test] root children: {root.winfo_children()}", flush=True)
    print(f"[minimal_paint_test] notebook mapped: {notebook.winfo_ismapped()} size={notebook.winfo_width()}x{notebook.winfo_height()}", flush=True)
    root.mainloop()


if __name__ == "__main__":
    main()
