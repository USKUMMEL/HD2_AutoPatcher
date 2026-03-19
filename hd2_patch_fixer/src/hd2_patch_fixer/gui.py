import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .archive import create_fixed_patch
from .constants import TYPE_LABELS


class PatchFixerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("HD2 Patch Fixer")
        self.root.geometry("860x700")
        self.root.minsize(760, 620)

        self.game_path_var = tk.StringVar()
        self.patch_path_var = tk.StringVar()
        self.export_path_var = tk.StringVar()
        self.keep_unknown_var = tk.BooleanVar(value=True)
        self.raw_fallback_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Ready")

        self.type_vars = {
            type_id: tk.BooleanVar(value=True)
            for type_id, _label in TYPE_LABELS
        }

        self.log_queue = queue.Queue()
        self.is_running = False

        self._build_layout()
        self._poll_logs()

    def _build_layout(self):
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill="both", expand=True)

        header = ttk.Label(
            container,
            text="Fix broken Helldivers 2 patch by rebuilding it from the default archive",
            font=("Segoe UI", 11, "bold"),
        )
        header.pack(anchor="w", pady=(0, 10))

        desc = ttk.Label(
            container,
            text=(
                "Flow: choose game data folder, choose broken patch, choose export folder, "
                "pick the data types to keep, then press Fix."
            ),
            wraplength=820,
        )
        desc.pack(anchor="w", pady=(0, 12))

        self._build_path_picker(
            container,
            "Game Data Folder",
            self.game_path_var,
            lambda: self._choose_directory(self.game_path_var),
        )
        self._build_path_picker(
            container,
            "Broken Patch File",
            self.patch_path_var,
            lambda: self._choose_patch_file(self.patch_path_var),
        )
        self._build_path_picker(
            container,
            "Export Folder",
            self.export_path_var,
            lambda: self._choose_directory(self.export_path_var),
        )

        options_frame = ttk.LabelFrame(container, text="Data Types To Keep", padding=10)
        options_frame.pack(fill="x", pady=(8, 12))

        buttons_row = ttk.Frame(options_frame)
        buttons_row.pack(fill="x", pady=(0, 8))

        ttk.Button(buttons_row, text="Select All", command=lambda: self._set_all_types(True)).pack(
            side="left"
        )
        ttk.Button(buttons_row, text="Clear All", command=lambda: self._set_all_types(False)).pack(
            side="left",
            padx=(8, 0),
        )
        ttk.Checkbutton(
            buttons_row,
            text="Keep Unknown Types",
            variable=self.keep_unknown_var,
        ).pack(side="right")
        ttk.Checkbutton(
            options_frame,
            text="Fallback raw copy for unsupported types (recommended for Unit mods)",
            variable=self.raw_fallback_var,
        ).pack(anchor="w", pady=(0, 8))

        grid = ttk.Frame(options_frame)
        grid.pack(fill="x")

        for index, (type_id, label) in enumerate(TYPE_LABELS):
            row = index // 2
            col = index % 2
            check = ttk.Checkbutton(grid, text=label, variable=self.type_vars[type_id])
            check.grid(row=row, column=col, sticky="w", padx=(0, 24), pady=4)

        actions = ttk.Frame(container)
        actions.pack(fill="x", pady=(0, 12))

        self.fix_button = ttk.Button(actions, text="Fix Patch", command=self._start_fix)
        self.fix_button.pack(side="left")

        ttk.Label(actions, textvariable=self.status_var).pack(side="left", padx=(12, 0))

        log_frame = ttk.LabelFrame(container, text="Log", padding=8)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(log_frame, height=18, wrap="word", state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _build_path_picker(self, parent, label, variable, browse_command):
        frame = ttk.LabelFrame(parent, text=label, padding=8)
        frame.pack(fill="x", pady=4)

        entry = ttk.Entry(frame, textvariable=variable)
        entry.pack(side="left", fill="x", expand=True)

        ttk.Button(frame, text="Browse", command=browse_command).pack(side="left", padx=(8, 0))

    def _choose_directory(self, variable):
        selected = filedialog.askdirectory()
        if selected:
            variable.set(selected)

    def _choose_patch_file(self, variable):
        selected = filedialog.askopenfilename(
            title="Select broken patch file",
            filetypes=[("Patch or archive files", "*")],
        )
        if selected:
            variable.set(selected)

    def _set_all_types(self, value: bool):
        for var in self.type_vars.values():
            var.set(value)

    def _append_log(self, message: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_logs(self):
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "done":
                    self.is_running = False
                    self.fix_button.configure(state="normal")
                    self.status_var.set("Done")
                    self._append_log("Fix completed successfully.")
                    output_path = payload["output_path"]
                    kept_entries = payload["kept_entries"]
                    skipped_entries = payload["skipped_entries"]
                    messagebox.showinfo(
                        "Fix completed",
                        (
                            f"Fixed patch created:\n{output_path}\n\n"
                            f"Copied entries: {kept_entries}\n"
                            f"Skipped entries: {skipped_entries}"
                        ),
                    )
                elif kind == "error":
                    self.is_running = False
                    self.fix_button.configure(state="normal")
                    self.status_var.set("Failed")
                    self._append_log(f"ERROR: {payload}")
                    messagebox.showerror("Fix failed", payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_logs)

    def _validate_inputs(self):
        game_dir = self.game_path_var.get().strip()
        patch_path = self.patch_path_var.get().strip()
        export_dir = self.export_path_var.get().strip()

        if not game_dir:
            raise ValueError("Please choose the game data folder first.")
        if not patch_path:
            raise ValueError("Please choose the broken patch file.")
        if not export_dir:
            raise ValueError("Please choose the export folder.")
        if Path(patch_path).suffix.lower() in [".stream", ".gpu_resources"]:
            raise ValueError("Please select the base patch file, not the .stream or .gpu_resources file.")

        keep_type_ids = {
            type_id for type_id, var in self.type_vars.items() if var.get()
        }
        if not keep_type_ids and not self.keep_unknown_var.get():
            raise ValueError("Please keep at least one type, or enable Keep Unknown Types.")

        return game_dir, patch_path, export_dir, keep_type_ids

    def _start_fix(self):
        if self.is_running:
            return

        try:
            game_dir, patch_path, export_dir, keep_type_ids = self._validate_inputs()
        except ValueError as exc:
            messagebox.showerror("Missing input", str(exc))
            return

        self.is_running = True
        self.fix_button.configure(state="disabled")
        self.status_var.set("Running")
        self._append_log("")
        self._append_log("Starting fix operation...")

        thread = threading.Thread(
            target=self._run_fix,
            args=(
                game_dir,
                patch_path,
                export_dir,
                keep_type_ids,
                self.keep_unknown_var.get(),
                self.raw_fallback_var.get(),
            ),
            daemon=True,
        )
        thread.start()

    def _run_fix(self, game_dir, patch_path, export_dir, keep_type_ids, keep_unknown, raw_fallback):
        try:
            result = create_fixed_patch(
                game_data_folder=game_dir,
                broken_patch_path=patch_path,
                export_dir=export_dir,
                keep_type_ids=keep_type_ids,
                keep_unknown_types=keep_unknown,
                raw_fallback_for_unsupported=raw_fallback,
                log=lambda message: self.log_queue.put(("log", message)),
            )
            self.log_queue.put(("done", result))
        except Exception as exc:
            self.log_queue.put(("error", str(exc)))


def run():
    root = tk.Tk()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    PatchFixerApp(root)
    root.mainloop()
