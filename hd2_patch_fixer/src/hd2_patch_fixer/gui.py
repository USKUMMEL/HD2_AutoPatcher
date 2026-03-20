import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .archive import (
    create_fixed_mod_archive,
    create_fixed_patch,
    normalize_archive_selection,
)
from .constants import TYPE_LABELS


class PatchFixerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("HD2 Patch Fixer")
        self.root.geometry("920x760")
        self.root.minsize(820, 680)

        self.game_path_var = tk.StringVar()
        self.patch_path_var = tk.StringVar()
        self.patch_export_path_var = tk.StringVar()
        self.mod_archive_path_var = tk.StringVar()
        self.mod_export_zip_var = tk.StringVar()
        self.keep_unknown_var = tk.BooleanVar(value=True)
        self.raw_fallback_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Ready")

        self.type_vars = {
            type_id: tk.BooleanVar(value=True)
            for type_id, _label in TYPE_LABELS
        }

        self.log_queue = queue.Queue()
        self.is_running = False
        self.fix_button = None
        self.mode_notebook = None
        self.single_tab = None
        self.compressed_tab = None

        self._build_layout()
        self._poll_logs()

    def _build_layout(self):
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill="both", expand=True)

        header = ttk.Label(
            container,
            text="Fix broken Helldivers 2 patch files and compressed mod packages",
            font=("Segoe UI", 11, "bold"),
        )
        header.pack(anchor="w", pady=(0, 10))

        desc = ttk.Label(
            container,
            text=(
                "Choose the game data folder, then either fix one patch directly or import a "
                "compressed mod archive and rebuild every patch inside it."
            ),
            wraplength=880,
        )
        desc.pack(anchor="w", pady=(0, 12))

        self._build_path_picker(
            container,
            "Game Data Folder",
            self.game_path_var,
            lambda: self._choose_directory(self.game_path_var),
        )

        self.mode_notebook = ttk.Notebook(container)
        self.mode_notebook.pack(fill="x", pady=(8, 12))

        self.single_tab = ttk.Frame(self.mode_notebook, padding=10)
        self.compressed_tab = ttk.Frame(self.mode_notebook, padding=10)
        self.mode_notebook.add(self.single_tab, text="Single Patch")
        self.mode_notebook.add(self.compressed_tab, text="Compressed Mods")

        self._build_path_picker(
            self.single_tab,
            "Broken Patch File",
            self.patch_path_var,
            lambda: self._choose_patch_file(self.patch_path_var),
        )
        self._build_path_picker(
            self.single_tab,
            "Export Folder",
            self.patch_export_path_var,
            lambda: self._choose_directory(self.patch_export_path_var),
        )

        self._build_path_picker(
            self.compressed_tab,
            "Compressed Mod File",
            self.mod_archive_path_var,
            lambda: self._choose_mod_archive_file(self.mod_archive_path_var),
        )
        self._build_path_picker(
            self.compressed_tab,
            "Export Zip File",
            self.mod_export_zip_var,
            self._choose_export_zip_file,
        )

        archive_help = ttk.Label(
            self.compressed_tab,
            text=(
                "Supported input formats: .zip, .7z, .rar. The output is always a .zip with the "
                "same folder layout and manifest files preserved."
            ),
            wraplength=840,
        )
        archive_help.pack(anchor="w", pady=(4, 0))

        options_frame = ttk.LabelFrame(container, text="Data Types To Keep", padding=10)
        options_frame.pack(fill="x", pady=(0, 12))

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

        self.fix_button = ttk.Button(actions, text="Fix", command=self._start_fix)
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
            variable.set(normalize_archive_selection(selected))

    def _choose_mod_archive_file(self, variable):
        selected = filedialog.askopenfilename(
            title="Select compressed mod archive",
            filetypes=[
                ("Compressed mod archives", "*.zip *.7z *.rar"),
                ("All files", "*"),
            ],
        )
        if selected:
            variable.set(selected)
            if not self.mod_export_zip_var.get().strip():
                default_name = f"{Path(selected).stem}_fixed.zip"
                variable_parent = Path(selected).parent
                self.mod_export_zip_var.set(str(variable_parent / default_name))

    def _choose_export_zip_file(self):
        initial_file = self.mod_export_zip_var.get().strip() or "fixed_mod.zip"
        selected = filedialog.asksaveasfilename(
            title="Choose output zip file",
            defaultextension=".zip",
            initialfile=Path(initial_file).name,
            filetypes=[("Zip archives", "*.zip")],
        )
        if selected:
            self.mod_export_zip_var.set(selected)

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
                    self._show_completion(payload)
                elif kind == "error":
                    self.is_running = False
                    self.fix_button.configure(state="normal")
                    self.status_var.set("Failed")
                    self._append_log(f"ERROR: {payload}")
                    messagebox.showerror("Fix failed", payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_logs)

    def _show_completion(self, payload: dict):
        if payload["mode"] == "single_patch":
            messagebox.showinfo(
                "Fix completed",
                (
                    f"Fixed patch created:\n{payload['output_path']}\n\n"
                    f"Copied entries: {payload['kept_entries']}\n"
                    f"Skipped entries: {payload['skipped_entries']}"
                ),
            )
            return

        messagebox.showinfo(
            "Fix completed",
            (
                f"Fixed mod archive created:\n{payload['output_path']}\n\n"
                f"Fixed patch files inside archive: {payload['fixed_patch_count']}"
            ),
        )

    def _current_mode(self):
        return "compressed_mods" if self.mode_notebook.select() == str(self.compressed_tab) else "single_patch"

    def _collect_keep_type_ids(self):
        keep_type_ids = {
            type_id for type_id, var in self.type_vars.items() if var.get()
        }
        if not keep_type_ids and not self.keep_unknown_var.get():
            raise ValueError("Please keep at least one type, or enable Keep Unknown Types.")
        return keep_type_ids

    def _validate_inputs(self):
        game_dir = self.game_path_var.get().strip()
        if not game_dir:
            raise ValueError("Please choose the game data folder first.")

        keep_type_ids = self._collect_keep_type_ids()
        mode = self._current_mode()

        if mode == "single_patch":
            patch_path = self.patch_path_var.get().strip()
            export_dir = self.patch_export_path_var.get().strip()
            if not patch_path:
                raise ValueError("Please choose the broken patch file.")
            if not export_dir:
                raise ValueError("Please choose the export folder.")
            return {
                "mode": mode,
                "game_dir": game_dir,
                "patch_path": normalize_archive_selection(patch_path),
                "export_dir": export_dir,
                "keep_type_ids": keep_type_ids,
            }

        archive_path = self.mod_archive_path_var.get().strip()
        export_zip = self.mod_export_zip_var.get().strip()
        if not archive_path:
            raise ValueError("Please choose the compressed mod file.")
        if not export_zip:
            raise ValueError("Please choose the export zip file.")
        return {
            "mode": mode,
            "game_dir": game_dir,
            "archive_path": archive_path,
            "export_zip": export_zip,
            "keep_type_ids": keep_type_ids,
        }

    def _start_fix(self):
        if self.is_running:
            return

        try:
            job = self._validate_inputs()
        except ValueError as exc:
            messagebox.showerror("Missing input", str(exc))
            return

        self.is_running = True
        self.fix_button.configure(state="disabled")
        self.status_var.set("Running")
        self._append_log("")
        if job["mode"] == "single_patch":
            self._append_log("Starting single patch fix operation...")
        else:
            self._append_log("Starting compressed mod fix operation...")

        thread = threading.Thread(
            target=self._run_fix,
            args=(job,),
            daemon=True,
        )
        thread.start()

    def _run_fix(self, job: dict):
        try:
            if job["mode"] == "single_patch":
                result = create_fixed_patch(
                    game_data_folder=job["game_dir"],
                    broken_patch_path=job["patch_path"],
                    export_dir=job["export_dir"],
                    keep_type_ids=job["keep_type_ids"],
                    keep_unknown_types=self.keep_unknown_var.get(),
                    raw_fallback_for_unsupported=self.raw_fallback_var.get(),
                    log=lambda message: self.log_queue.put(("log", message)),
                )
                result["mode"] = "single_patch"
            else:
                result = create_fixed_mod_archive(
                    game_data_folder=job["game_dir"],
                    input_archive_path=job["archive_path"],
                    output_zip_path=job["export_zip"],
                    keep_type_ids=job["keep_type_ids"],
                    keep_unknown_types=self.keep_unknown_var.get(),
                    raw_fallback_for_unsupported=self.raw_fallback_var.get(),
                    log=lambda message: self.log_queue.put(("log", message)),
                )
                result["mode"] = "compressed_mods"
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
