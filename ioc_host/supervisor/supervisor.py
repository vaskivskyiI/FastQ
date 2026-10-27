"""GUI supervisor for the IOCs in this stack.

Run it from the repository folder (creates / updates .venv first):

    start_ioc_host.bat [<host file>] [--headless]
    (or: python ioc_host\\supervisor\\supervisor.py [--config <host file>])

Without a window (service / systemd, see deploy/README.md and headless.py):

    python ioc_host\\supervisor\\supervisor.py --headless --config ioc_host\\hosts\\<host>.yaml

What it does:
  - Checks this repo's Python dependencies (requirements-ioc-host.txt) on startup
    and installs anything missing before starting any IOC - including
    before the main window itself is built, since building it needs
    pyyaml (for supervisor_config.yaml), which may not be installed yet
    on a machine that has never run this before. See _bootstrap_dependencies()
    for why that ordering matters.
  - Discovers every iocs/*_ioc.py TYPE; "Add device..." adds an INSTANCE of
    one (nothing is added automatically) - it starts disabled with blank args;
    fill its args in, then enable it. Either way, a device
    with no/wrong args crash-loops with the error visible in its log until
    its args are right - that's expected, not a bug.
  - Runs each enabled device instance as its own subprocess, restarts it
    on crash with exponential backoff (visible per-row), and lets you
    enable/disable, edit args, add, or delete instances from the table -
    all of it persists to supervisor_config.yaml immediately.

Tkinter ships with standard Python on Windows, so there's nothing extra to
install for the GUI itself.
"""
from __future__ import annotations

import queue
import os
import sys
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__" and {"-h", "--help"} & set(sys.argv[1:]):
    print(__doc__)
    sys.exit(0)

if __name__ == "__main__" and "--headless" in sys.argv[1:]:
    # before importing tkinter: a service / Linux server may have no Tk at all
    import headless

    sys.exit(headless.main([a for a in sys.argv[1:] if a != "--headless"]))

import tkinter as tk  # noqa: E402
from tkinter import messagebox, simpledialog, ttk  # noqa: E402

import deps  # noqa: E402  stdlib-only - safe to import and use before any dependency is installed

TICK_MS = 1000
RESCAN_EVERY_TICKS = 10  # rescan iocs/ for new *_ioc.py files roughly every 10s

STATUS_COLORS = {
    "running": "#1a7f37",
    "starting": "#9a6700",
    "stopped": "#57606a",
    "disabled": "#8c959f",
    "missing": "#cf222e",
}


class ArgsDialog(simpledialog.Dialog):
    def __init__(self, parent, title, initial_value):
        self.initial_value = initial_value
        self.result_value = None
        super().__init__(parent, title=title)

    def body(self, master):
        tk.Label(master, text="Command-line args (as you'd type them after the script name):").pack(
            anchor="w", padx=4, pady=(4, 0)
        )
        self.entry = tk.Entry(master, width=80)
        self.entry.insert(0, self.initial_value)
        self.entry.pack(padx=4, pady=4)
        return self.entry

    def apply(self):
        self.result_value = self.entry.get()


class AddDeviceDialog(simpledialog.Dialog):
    """Lets the user add another device instance: pick which iocs/*_ioc.py
    type it runs, and a unique name for it - two ITC503s, say, need two
    different device_ids even though both run itc503_ioc.py."""

    def __init__(self, parent, ioc_types: list[str], suggested_name: str):
        self.ioc_types = ioc_types
        self.suggested_name = suggested_name
        self.result_type: str | None = None
        self.result_name: str | None = None
        super().__init__(parent, title="Add device instance")

    def body(self, master):
        tk.Label(master, text="Device type:").grid(row=0, column=0, sticky="w", padx=4, pady=(4, 0))
        self.type_var = tk.StringVar(value=self.ioc_types[0] if self.ioc_types else "")
        self.type_combo = ttk.Combobox(
            master, textvariable=self.type_var, values=self.ioc_types, state="readonly", width=30
        )
        self.type_combo.grid(row=1, column=0, sticky="w", padx=4, pady=(0, 8))
        self.type_combo.bind("<<ComboboxSelected>>", self._on_type_changed)

        tk.Label(master, text="Name (must be unique):").grid(row=2, column=0, sticky="w", padx=4)
        self.name_var = tk.StringVar(value=self.suggested_name)
        self.name_entry = tk.Entry(master, textvariable=self.name_var, width=32)
        self.name_entry.grid(row=3, column=0, sticky="w", padx=4, pady=(0, 4))

        tk.Label(
            master,
            text="Starts disabled with blank args - there's no default address\nto guess for a second unit. Fill in its args, then enable it.",
            justify="left",
            fg="#57606a",
        ).grid(row=4, column=0, sticky="w", padx=4, pady=(4, 4))

        return self.type_combo

    def _on_type_changed(self, _event) -> None:
        # Re-suggest a name matching the newly-picked type, but only if the
        # user hasn't already typed something of their own.
        if self.name_var.get() == self.suggested_name or not self.name_var.get().strip():
            self.name_var.set(self.type_var.get())
            self.suggested_name = self.type_var.get()

    def validate(self) -> bool:
        if not self.type_var.get():
            messagebox.showerror("Add device", "Pick a device type.", parent=self)
            return False
        if not self.name_var.get().strip():
            messagebox.showerror("Add device", "Enter a name for this device instance.", parent=self)
            return False
        return True

    def apply(self):
        self.result_type = self.type_var.get()
        self.result_name = self.name_var.get().strip()


class SupervisorApp(tk.Tk):
    def __init__(self, deps_already_ok: bool = False) -> None:
        super().__init__()
        self.title("FastQ EPICS Supervisor")
        self.geometry("1100x650")

        # Imported here, not at module level: these pull in pyyaml (via
        # config.py's load_config/save_config), which is only guaranteed
        # to be installed once _bootstrap_dependencies() below has run.
        from config import DEFAULT_CONFIG_PATH, load_config
        from process_manager import ProcessSupervisor

        self.config_obj = load_config(DEFAULT_CONFIG_PATH)  # written only when the user changes something
        self.output_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.supervisor = ProcessSupervisor(self.config_obj, output_queue=self.output_queue)

        self._tick_count = 0
        self._deps_ready = deps_already_ok
        self._closing = False  # set True in _on_close - see _tick()'s reschedule guard

        self._build_ui()
        self._refresh_table(rebuild=True)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if deps_already_ok:
            # Bootstrap already confirmed everything's installed - skip
            # straight to starting the enabled devices instead of running
            # the pip check a second time.
            self.deps_status_var.set("Dependencies OK")
            self.after(200, self._start_all)
        else:
            self.after(200, self._check_dependencies_then_start)
        self.after(TICK_MS, self._tick)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=6, pady=6)

        ttk.Button(toolbar, text="Start selected", command=self._start_selected).pack(side="left")
        ttk.Button(toolbar, text="Stop selected", command=self._stop_selected).pack(side="left", padx=(4, 0))
        ttk.Button(toolbar, text="Restart selected", command=self._restart_selected).pack(side="left", padx=(4, 0))
        ttk.Button(toolbar, text="Edit args...", command=self._edit_args_selected).pack(side="left", padx=(4, 0))
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(toolbar, text="Add device...", command=self._add_device).pack(side="left")
        ttk.Button(toolbar, text="Delete selected", command=self._delete_selected).pack(side="left", padx=(4, 0))
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(toolbar, text="Start all enabled", command=self._start_all).pack(side="left")
        ttk.Button(toolbar, text="Stop all", command=self._stop_all).pack(side="left", padx=(4, 0))
        ttk.Button(toolbar, text="Rescan for new devices", command=self._rescan_now).pack(side="left", padx=(4, 0))
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(toolbar, text="Open in dashboard", command=self._open_monitor).pack(side="left")
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(toolbar, text="Check/install dependencies", command=self._check_dependencies_then_start).pack(
            side="left"
        )

        self.deps_status_var = tk.StringVar(value="Checking dependencies...")
        ttk.Label(toolbar, textvariable=self.deps_status_var).pack(side="right")

        columns = ("type", "enabled", "status", "pid", "restarts", "args")
        self.tree = ttk.Treeview(self, columns=columns, show="tree headings", height=12)
        self.tree.heading("#0", text="Device")
        self.tree.heading("type", text="Type")
        self.tree.heading("enabled", text="Enabled")
        self.tree.heading("status", text="Status")
        self.tree.heading("pid", text="PID")
        self.tree.heading("restarts", text="Restarts")
        self.tree.heading("args", text="Args")
        self.tree.column("#0", width=160, anchor="w")
        self.tree.column("type", width=130, anchor="w")
        self.tree.column("enabled", width=70, anchor="center")
        self.tree.column("status", width=220, anchor="w")
        self.tree.column("pid", width=70, anchor="center")
        self.tree.column("restarts", width=70, anchor="center")
        self.tree.column("args", width=380, anchor="w")
        self.tree.pack(fill="x", padx=6, pady=(0, 6))
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double_click)

        log_frame = ttk.Frame(self)
        log_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        ttk.Label(log_frame, text="Combined log (newest at bottom):").pack(anchor="w")

        text_frame = ttk.Frame(log_frame)
        text_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(text_frame, height=18, wrap="none", state="disabled")
        scroll_y = ttk.Scrollbar(text_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll_y.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll_y.pack(side="right", fill="y")

        ttk.Button(log_frame, text="Clear log view", command=self._clear_log).pack(anchor="e", pady=(4, 0))

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------

    def _refresh_table(self, rebuild: bool = False) -> None:
        if rebuild:
            # delete()+insert() drops the Treeview's selection, so save and
            # restore it around the rebuild - otherwise every periodic
            # rescan (see _rescan_now, called every ~10s) silently
            # deselects whatever row was selected.
            selected = self.tree.selection()
            self.tree.delete(*self.tree.get_children())
            for device_id in sorted(self.supervisor.devices):
                self.tree.insert("", "end", iid=device_id, text=device_id)
            still_present = [device_id for device_id in selected if self.tree.exists(device_id)]
            if still_present:
                self.tree.selection_set(still_present)

        for device_id, managed in self.supervisor.devices.items():
            if not self.tree.exists(device_id):
                self.tree.insert("", "end", iid=device_id, text=device_id)
            self.tree.item(
                device_id,
                values=(
                    managed.config.ioc_script,
                    "Yes" if managed.config.enabled else "No",
                    managed.status,
                    managed.pid or "",
                    managed.restart_count,
                    managed.config.args,
                ),
                tags=(self._status_tag(managed.status),),
            )
        for status, color in STATUS_COLORS.items():
            self.tree.tag_configure(status, foreground=color)

    @staticmethod
    def _status_tag(status: str) -> str:
        for key in STATUS_COLORS:
            if status.startswith(key):
                return key
        return "stopped"

    def _selected_device_id(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def _on_tree_click(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row_id:
            return
        if col == "#2":  # "enabled" column - click to toggle
            managed = self.supervisor.devices[row_id]
            self.supervisor.set_enabled(row_id, not managed.config.enabled)
            self._refresh_table()

    def _on_tree_double_click(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if row_id and col == "#6":  # "args" column
            self._edit_args(row_id)

    # ------------------------------------------------------------------
    # Toolbar actions
    # ------------------------------------------------------------------

    def _start_selected(self) -> None:
        device_id = self._selected_device_id()
        if device_id:
            self.supervisor.devices[device_id].restart_count = 0
            self.supervisor.start(device_id)
            self._refresh_table()

    def _stop_selected(self) -> None:
        device_id = self._selected_device_id()
        if device_id:
            self.supervisor.stop(device_id)
            self._refresh_table()

    def _restart_selected(self) -> None:
        device_id = self._selected_device_id()
        if device_id:
            self.supervisor.devices[device_id].restart_count = 0
            self.supervisor.restart(device_id)
            self._refresh_table()

    def _edit_args_selected(self) -> None:
        device_id = self._selected_device_id()
        if device_id:
            self._edit_args(device_id)

    def _edit_args(self, device_id: str) -> None:
        managed = self.supervisor.devices[device_id]
        dialog = ArgsDialog(self, f"Args for {device_id}", managed.config.args)
        if dialog.result_value is not None:
            self.supervisor.set_args(device_id, dialog.result_value)
            self._refresh_table()

    def _add_device(self) -> None:
        from config import discover_ioc_scripts, unique_device_id, IOCS_DIR

        ioc_types = discover_ioc_scripts(IOCS_DIR)
        if not ioc_types:
            messagebox.showinfo("Add device", "No iocs/*_ioc.py files found to add an instance of.")
            return
        suggested = unique_device_id(ioc_types[0], self.supervisor.devices)
        dialog = AddDeviceDialog(self, ioc_types, suggested)
        if dialog.result_type is None:
            return  # cancelled

        device_id = dialog.result_name
        if device_id in self.supervisor.devices:
            messagebox.showerror("Add device", f'"{device_id}" already exists - pick a different name.')
            return

        self.supervisor.add_device(dialog.result_type, device_id=device_id)
        self._refresh_table(rebuild=True)
        self._append_log(
            "supervisor",
            f"Added device {device_id} (type {dialog.result_type}) - disabled with blank args; "
            "edit its args, then enable it to start.",
        )
        # Select the new row and jump straight to editing its args, since
        # it needs a real address before it can do anything.
        self.tree.selection_set(device_id)
        self.tree.see(device_id)
        self._edit_args(device_id)

    def _open_monitor(self) -> None:
        """Open the selected device's page of the web dashboard (FASTQ_DASHBOARD, default
        http://localhost:8088 - the control host) in the browser."""
        import webbrowser

        url = os.environ.get("FASTQ_DASHBOARD", "http://localhost:8088").rstrip("/") + "/#devices"
        device_id = self._selected_device_id()
        prefix = self._extract_prefix(self.supervisor.devices[device_id].config.args) if device_id else None
        if prefix:
            url += "?d=" + prefix.rstrip(":").split(":")[-1].lower()
        webbrowser.open(url)

    @staticmethod
    def _extract_prefix(args: str) -> str | None:
        from ioc_host.common.naming import prefixes

        found = prefixes(args)
        return found[0] if found else None

    def _delete_selected(self) -> None:
        device_id = self._selected_device_id()
        if not device_id:
            return
        managed = self.supervisor.devices[device_id]
        running = managed.process is not None and managed.process.poll() is None
        warning = "\n\nIt's currently running - this will stop it first." if running else ""
        if not messagebox.askyesno(
            "Delete device",
            f'Remove "{device_id}" (type {managed.config.ioc_script}) from the supervisor?{warning}\n\n'
            "Its launch args are not saved anywhere else - if you re-add a device of this "
            "type later, you'll need to re-enter them.",
        ):
            return
        self.supervisor.remove(device_id)
        self._refresh_table(rebuild=True)
        self._append_log("supervisor", f"Deleted device {device_id}.")

    def _start_all(self) -> None:
        for device_id, managed in self.supervisor.devices.items():
            if managed.config.enabled:
                self.supervisor.start(device_id)
        self._refresh_table()

    def _stop_all(self) -> None:
        for device_id in list(self.supervisor.devices):
            self.supervisor.stop(device_id)
        self._refresh_table()

    def _rescan_now(self) -> None:
        new_ids = self.supervisor.sync_with_config()
        # Only rebuild (which risks a selection hiccup even with the
        # save/restore above) when the set of rows actually changed; the
        # common case - nothing new - just refreshes values in place.
        self._refresh_table(rebuild=bool(new_ids))
        for device_id in new_ids:
            managed = self.supervisor.devices[device_id]
            if managed.config.args.strip():
                self._append_log(
                    "supervisor",
                    f"New device discovered: {device_id} (enabled, starting with its default args - edit if needed)",
                )
            else:
                self._append_log(
                    "supervisor",
                    f"New device discovered: {device_id} (enabled, args empty - edit and it will start)",
                )
            if self._deps_ready:
                self.supervisor.start(device_id)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # Dependencies
    # ------------------------------------------------------------------

    def _check_dependencies_then_start(self) -> None:
        self.deps_status_var.set("Checking dependencies...")
        threading.Thread(target=self._dependency_worker, daemon=True).start()

    def _dependency_worker(self) -> None:
        missing = deps.missing_packages()
        if not missing:
            self.output_queue.put(("supervisor", "All dependencies already installed."))
            self.deps_status_var.set("Dependencies OK")
            self._deps_ready = True
            self.output_queue.put(("__start_all__", ""))
            return

        self.deps_status_var.set(f"Installing missing packages: {', '.join(missing)}...")
        self.output_queue.put(("supervisor", f"Missing packages: {', '.join(missing)} - installing..."))
        code = deps.install_requirements(lambda line: self.output_queue.put(("pip", line)))
        if code == 0:
            self.deps_status_var.set("Dependencies OK")
            self._deps_ready = True
            self.output_queue.put(("__start_all__", ""))
        else:
            self.deps_status_var.set("Dependency install FAILED - see log")
            self.output_queue.put(("supervisor", "pip install failed - fix manually, then use 'Check/install dependencies' again."))

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        self.supervisor.tick()

        drained = 0
        while drained < 500:  # cap per tick so a noisy IOC can't freeze the GUI
            try:
                device_id, line = self.output_queue.get_nowait()
            except queue.Empty:
                break
            drained += 1
            if device_id == "__start_all__":
                self._start_all()
                continue
            self._append_log(device_id, line)

        self._refresh_table()

        self._tick_count += 1
        if self._tick_count % RESCAN_EVERY_TICKS == 0:
            self._rescan_now()

        # Same "invalid command name" hazard as _BootstrapWindow._poll:
        # only reschedule while the window isn't being torn down, so a
        # pending after() callback can't fire against an already-destroyed
        # widget once _on_close() has run.
        if not self._closing:
            self.after(TICK_MS, self._tick)

    def _append_log(self, device_id: str, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{device_id}] {line}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        self._closing = True
        if messagebox.askyesno(
            "Stop IOCs?",
            "Stop all running IOCs before closing the supervisor?\n\n"
            "Choosing No leaves them running as orphaned processes - "
            "they will keep running, but nothing will restart them if "
            "they crash once this window is gone.",
        ):
            self.supervisor.shutdown_all()
        self.destroy()


class _BootstrapWindow(tk.Tk):
    """Minimal window shown only while checking/installing dependencies -
    deliberately built from nothing but tkinter + ioc_host/supervisor/deps.py (both safe to
    use with zero third-party packages installed), so it can never fail
    with the same "needs a package we haven't installed yet" problem it
    exists to solve. Calls on_done(deps_ok) once finished and closes itself."""

    def __init__(self, on_done) -> None:
        super().__init__()
        self.title("FastQ EPICS Supervisor - starting")
        self.geometry("640x360")
        self._on_done = on_done
        self._closed_by_user = False
        # Background thread only ever pushes onto this queue; every Tkinter
        # call happens on the main thread via _poll(), same rule
        # process_manager.py follows for its subprocess reader threads.
        self._events: "queue.Queue[tuple]" = queue.Queue()

        tk.Label(self, text="Checking dependencies...", anchor="w").pack(fill="x", padx=8, pady=(8, 4))
        self.text = tk.Text(self, height=18, state="disabled")
        self.text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._run)
        self.after(100, self._poll)

    def _on_close(self) -> None:
        self._closed_by_user = True
        self.destroy()

    def _log(self, line: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")
        self.update_idletasks()

    def _run(self) -> None:
        threading.Thread(target=self._worker, daemon=True).start()

    def _poll(self) -> None:
        """Runs on the main/GUI thread only - the one place this window
        touches its own widgets. Drains whatever the worker thread queued.

        Reschedules itself via after() only while the window is still
        alive and hasn't received its one "finish" event yet. Without that
        second check, _finish() schedules self.destroy() via a *separate*
        after() call (see below) while this loop keeps rescheduling itself
        every 100ms in the meantime - so an already-pending _poll() call
        can fire after destroy() has run, which Tkinter reports as
        `invalid command name "..._poll"` (the widget's underlying Tcl
        command is gone). The worker thread only ever sends one "finish"
        event as its last message, so it's safe to just stop polling then."""
        try:
            while True:
                event = self._events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._log(event[1])
                elif kind == "finish":
                    self._closed_by_user = True  # not literally "by user", but: stop rescheduling
                    self._finish(event[1], event[2])
        except queue.Empty:
            pass
        if not self._closed_by_user:
            self.after(100, self._poll)

    def _worker(self) -> None:
        try:
            missing = deps.missing_packages()
            if not missing:
                self._events.put(("finish", True, "All dependencies already installed."))
                return
            self._events.put(("log", f"Missing packages: {', '.join(missing)} - installing..."))
            code = deps.install_requirements(lambda line: self._events.put(("log", line)))
            if code == 0:
                self._events.put(("finish", True, "Dependencies installed."))
            else:
                self._events.put(
                    (
                        "finish",
                        False,
                        f"pip install exited with code {code} - see above. Fix manually "
                        "(e.g. run 'pip install -r requirements-ioc-host.txt' yourself and read the "
                        "actual error), then re-run start_ioc_host.bat.",
                    )
                )
        except Exception:
            trace = traceback.format_exc()
            self._events.put(("finish", False, f"Unexpected error while checking dependencies:\n{trace}"))

    def _finish(self, ok: bool, message: str) -> None:
        self._log(message)
        if ok:
            self.after(400, lambda: (self.destroy(), self._on_done(True)))
        else:
            self._log("\nClose this window when you're done reading the error above.")
            # Leave the window open so the error is readable; on_done still
            # runs once the user closes it, so main() can exit cleanly
            # rather than the process just hanging forever.
            self.protocol("WM_DELETE_WINDOW", lambda: (self.destroy(), self._on_done(False)))


def main() -> None:
    if "--config" in sys.argv[1:-1]:  # same option as --headless; config.py reads it at import
        os.environ["FASTQ_IOC_CONFIG"] = str(Path(sys.argv[sys.argv.index("--config") + 1]).resolve())
    try:
        result: dict = {}

        def on_bootstrap_done(ok: bool) -> None:
            result["ok"] = ok

        bootstrap = _BootstrapWindow(on_bootstrap_done)
        bootstrap.mainloop()

        if not result.get("ok"):
            print("Dependency setup did not complete successfully - not starting the supervisor.")
            sys.exit(1)

        app = SupervisorApp(deps_already_ok=True)
        app.mainloop()
    except Exception:
        # Printed to stdout/stderr so it's visible in the console
        # start_ioc_host.bat opens, and the nonzero exit makes that
        # window's "if errorlevel 1 pause" keep it open to show this.
        traceback.print_exc()
        try:
            messagebox.showerror("FastQ EPICS Supervisor - failed to start", traceback.format_exc())
        except Exception:
            pass  # tkinter itself may be what's broken - the printed traceback is the fallback
        sys.exit(1)


if __name__ == "__main__":
    main()
