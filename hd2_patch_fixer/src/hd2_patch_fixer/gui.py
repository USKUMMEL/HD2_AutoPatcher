import queue
import threading
import tkinter as tk
from math import ceil
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .archive import (
    create_fixed_mod_archive,
    create_fixed_patch,
    normalize_archive_selection,
)
from .constants import (
    ParticleID,
    TYPE_LABELS,
    WwiseBankID,
    WwiseDepID,
    WwiseMetaDataID,
    WwiseStreamID,
)


BG_APP = "#16181c"
BG_PANEL = "#20242a"
BG_INPUT = "#2a2f36"
FG_MAIN = "#f1f3f5"
FG_MUTED = "#a8b0b8"
ACCENT = "#4f5863"
ACCENT_HOVER = "#626d79"
BORDER = "#343b44"
DEFAULT_UNCHECKED_TYPE_IDS = {
    ParticleID,
    WwiseBankID,
    WwiseDepID,
    WwiseStreamID,
    WwiseMetaDataID,
}


class PatchFixerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("HD2 Patch Fixer")
        self.root.geometry("920x760")
        self.root.minsize(820, 660)
        self.root.configure(bg=BG_APP)

        self.game_path_var = tk.StringVar()
        self.patch_path_var = tk.StringVar()
        self.patch_export_path_var = tk.StringVar()
        self.mod_archive_path_var = tk.StringVar()
        self.mod_export_zip_var = tk.StringVar()
        self.keep_unknown_var = tk.BooleanVar(value=True)
        self.raw_fallback_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Ready")

        self.type_vars = {
            type_id: tk.BooleanVar(value=type_id not in DEFAULT_UNCHECKED_TYPE_IDS)
            for type_id, _label in TYPE_LABELS
        }

        self.log_queue = queue.Queue()
        self.is_running = False

        self._configure_style()
        self._build_layout()
        self._poll_logs()

    def _configure_style(self):
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure(".", background=BG_APP, foreground=FG_MAIN)
        style.configure("Root.TFrame", background=BG_APP)
        style.configure("Card.TFrame", background=BG_PANEL, relief="flat")
        style.configure(
            "Title.TLabel",
            background=BG_APP,
            foreground=FG_MAIN,
            font=("Segoe UI", 18, "bold"),
        )
        style.configure(
            "Body.TLabel",
            background=BG_APP,
            foreground=FG_MUTED,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Section.TLabel",
            background=BG_PANEL,
            foreground=FG_MAIN,
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "TLabelframe",
            background=BG_PANEL,
            bordercolor=BORDER,
            relief="solid",
            borderwidth=1,
            padding=12,
        )
        style.configure(
            "TLabelframe.Label",
            background=BG_PANEL,
            foreground=FG_MAIN,
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "TNotebook",
            background=BG_APP,
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "TNotebook.Tab",
            background="#2b3138",
            foreground=FG_MAIN,
            padding=(16, 8),
            font=("Segoe UI", 10, "bold"),
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", "#353c45"), ("active", "#303740")],
        )
        style.configure(
            "TButton",
            background=ACCENT,
            foreground="#ffffff",
            borderwidth=0,
            focusthickness=0,
            padding=(14, 8),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "TButton",
            background=[("active", ACCENT_HOVER), ("disabled", "#3a4048")],
            foreground=[("disabled", "#8f98a1")],
        )
        style.configure(
            "Secondary.TButton",
            background="#3a4048",
            foreground=FG_MAIN,
        )
        style.map(
            "Secondary.TButton",
            background=[("active", "#4a525c"), ("disabled", "#30353c")],
            foreground=[("disabled", "#7f8790")],
        )
        style.configure(
            "TCheckbutton",
            background=BG_PANEL,
            foreground=FG_MAIN,
            font=("Segoe UI", 10),
        )
        style.map("TCheckbutton", background=[("active", BG_PANEL)])
        style.configure(
            "TEntry",
            fieldbackground=BG_INPUT,
            foreground=FG_MAIN,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=7,
        )

    def _build_layout(self):
        container = ttk.Frame(self.root, style="Root.TFrame", padding=18)
        container.pack(fill="both", expand=True)

        header = ttk.Label(container, text="HD2 Patch Fixer", style="Title.TLabel")
        header.pack(anchor="w")

        desc = ttk.Label(
            container,
            text=(
                "Fix one patch directly or import a compressed mod package and rebuild "
                "every patch inside it."
            ),
            style="Body.TLabel",
            wraplength=880,
            justify="left",
        )
        desc.pack(anchor="w", pady=(4, 14))

        self._build_path_picker(
            container,
            "Game Data Folder",
            self.game_path_var,
            lambda: self._choose_directory(self.game_path_var),
        )

        self.mode_notebook = ttk.Notebook(container)
        self.mode_notebook.pack(fill="x", pady=(10, 14))

        self.single_tab = ttk.Frame(self.mode_notebook, style="Card.TFrame", padding=14)
        self.compressed_tab = ttk.Frame(self.mode_notebook, style="Card.TFrame", padding=14)
        self.mode_notebook.add(self.single_tab, text="Single Patch")
        self.mode_notebook.add(self.compressed_tab, text="Compressed Mods")

        self._build_single_patch_tab()
        self._build_compressed_mods_tab()

        self._build_actions(container)
        self._build_type_options(container)
        self._build_log(container)

    def _build_single_patch_tab(self):
        self._build_path_picker(
            self.single_tab,
            "Broken Patch File",
            self.patch_path_var,
            lambda: self._choose_patch_file(self.patch_path_var),
            card_style=True,
        )
        self._build_path_picker(
            self.single_tab,
            "Export Folder",
            self.patch_export_path_var,
            lambda: self._choose_directory(self.patch_export_path_var),
            card_style=True,
        )

        hint = ttk.Label(
            self.single_tab,
            text=(
                "You can select the base patch, .stream, or .gpu_resources file. "
                "The tool will normalize it automatically."
            ),
            style="Body.TLabel",
            wraplength=820,
            justify="left",
        )
        hint.pack(anchor="w", pady=(8, 0))

    def _build_compressed_mods_tab(self):
        self._build_path_picker(
            self.compressed_tab,
            "Compressed Mod File",
            self.mod_archive_path_var,
            lambda: self._choose_mod_archive_file(),
            card_style=True,
        )
        self._build_path_picker(
            self.compressed_tab,
            "Export Zip File",
            self.mod_export_zip_var,
            self._choose_export_zip_file,
            card_style=True,
        )

        hint = ttk.Label(
            self.compressed_tab,
            text=(
                "Supported input formats: .zip, .7z, .rar. Output is always a .zip with "
                "manifest.json and folder layout preserved."
            ),
            style="Body.TLabel",
            wraplength=820,
            justify="left",
        )
        hint.pack(anchor="w", pady=(8, 0))

    def _build_type_options(self, parent):
        options_frame = ttk.LabelFrame(parent, text="Data Types To Keep")
        options_frame.pack(fill="x", pady=(0, 14))

        buttons_row = ttk.Frame(options_frame, style="Card.TFrame")
        buttons_row.pack(fill="x", pady=(0, 8))

        ttk.Button(
            buttons_row,
            text="Select All",
            command=lambda: self._set_all_types(True),
        ).pack(side="left")
        ttk.Button(
            buttons_row,
            text="Clear All",
            style="Secondary.TButton",
            command=lambda: self._set_all_types(False),
        ).pack(side="left", padx=(8, 0))

        ttk.Checkbutton(
            buttons_row,
            text="Keep Unknown Types",
            variable=self.keep_unknown_var,
        ).pack(side="right")

        ttk.Checkbutton(
            options_frame,
            text="Fallback raw copy for unsupported types (recommended for Unit mods)",
            variable=self.raw_fallback_var,
        ).pack(anchor="w", pady=(0, 10))

        grid = ttk.Frame(options_frame, style="Card.TFrame")
        grid.pack(fill="x", pady=(0, 8))

        rows = 3
        columns = ceil(len(TYPE_LABELS) / rows)
        for index, (type_id, label) in enumerate(TYPE_LABELS):
            row = index % rows
            col = index // rows
            check = ttk.Checkbutton(grid, text=label, variable=self.type_vars[type_id])
            check.grid(row=row, column=col, sticky="w", padx=(0, 24), pady=(2, 2))

        for col in range(columns):
            grid.columnconfigure(col, weight=1)

    def _build_actions(self, parent):
        actions = ttk.Frame(parent, style="Root.TFrame")
        actions.pack(fill="x", pady=(0, 14))

        inner = ttk.Frame(actions, style="Root.TFrame")
        inner.pack(anchor="center")

        self.fix_button = ttk.Button(inner, text="Fix", command=self._start_fix)
        self.fix_button.pack(side="left")

        status = ttk.Label(inner, textvariable=self.status_var, style="Body.TLabel")
        status.pack(side="left", padx=(12, 0))

    def _build_log(self, parent):
        log_frame = ttk.LabelFrame(parent, text="Log")
        log_frame.pack(fill="both", expand=True)

        text_wrap = ttk.Frame(log_frame, style="Card.TFrame")
        text_wrap.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            text_wrap,
            height=10,
            wrap="word",
            state="disabled",
            bg=BG_INPUT,
            fg=FG_MAIN,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            insertbackground=FG_MAIN,
            font=("Consolas", 10),
            padx=10,
            pady=10,
        )
        self.log_text.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(text_wrap, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _build_path_picker(self, parent, label, variable, browse_command, card_style=False):
        frame_parent = parent
        style = "Card.TFrame" if card_style else "Root.TFrame"

        frame = ttk.LabelFrame(frame_parent, text=label)
        frame.pack(fill="x", pady=4)

        row = ttk.Frame(frame, style=style)
        row.pack(fill="x")

        entry = ttk.Entry(row, textvariable=variable)
        entry.pack(side="left", fill="x", expand=True)

        ttk.Button(row, text="Browse", command=browse_command).pack(side="left", padx=(8, 0))

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

    def _choose_mod_archive_file(self):
        selected = filedialog.askopenfilename(
            title="Select compressed mod archive",
            filetypes=[
                ("Compressed mod archives", "*.zip *.7z *.rar"),
                ("All files", "*"),
            ],
        )
        if selected:
            self.mod_archive_path_var.set(selected)
            if not self.mod_export_zip_var.get().strip():
                default_name = f"{Path(selected).stem}_fixed.zip"
                self.mod_export_zip_var.set(str(Path(selected).parent / default_name))

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
        return "compressed_mods" if self.mode_notebook.index("current") == 1 else "single_patch"

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
        self._append_log(
            "Starting single patch fix operation..."
            if job["mode"] == "single_patch"
            else "Starting compressed mod fix operation..."
        )

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
    PatchFixerApp(root)
    root.mainloop()
