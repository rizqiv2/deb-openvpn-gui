#!/usr/bin/env python3
"""Small Tkinter OpenVPN profile manager for Linux."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from tkinter import END, BOTH, DISABLED, NORMAL, LEFT, RIGHT, X, Y, filedialog, messagebox, simpledialog
import tkinter as tk
from tkinter import ttk


APP_NAME = "openvpngui"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
PROFILE_DIR = CONFIG_DIR / "profiles"
PROFILE_DB = CONFIG_DIR / "profiles.json"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / APP_NAME

COPY_DIRECTIVES = {
    "auth-user-pass",
    "ca",
    "cert",
    "crl-verify",
    "dh",
    "extra-certs",
    "http-proxy-user-pass",
    "key",
    "pkcs12",
    "secret",
    "tls-auth",
    "tls-crypt",
    "tls-crypt-v2",
}


@dataclass
class Profile:
    id: str
    name: str
    config: str
    imported_at: float

    @property
    def path(self) -> Path:
        return Path(self.config).expanduser()


class ProfileStore:
    def __init__(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self.profiles: list[Profile] = []
        self.load()

    def load(self) -> None:
        if not PROFILE_DB.exists():
            self.profiles = []
            return

        try:
            data = json.loads(PROFILE_DB.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = []

        self.profiles = [
            Profile(
                id=item["id"],
                name=item["name"],
                config=item["config"],
                imported_at=float(item.get("imported_at", 0)),
            )
            for item in data
            if item.get("id") and item.get("name") and item.get("config")
        ]

    def save(self) -> None:
        payload = [
            {
                "id": profile.id,
                "name": profile.name,
                "config": profile.config,
                "imported_at": profile.imported_at,
            }
            for profile in self.profiles
        ]
        tmp_path = PROFILE_DB.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(PROFILE_DB)

    def add(self, source_path: Path, name: str) -> Profile:
        name = self.unique_name(name)
        profile_id = f"{slugify(name)}-{uuid.uuid4().hex[:8]}"
        dest_dir = PROFILE_DIR / profile_id
        dest_dir.mkdir(parents=True, exist_ok=False)
        dest_config = dest_dir / source_path.name

        shutil.copy2(source_path, dest_config)
        copy_referenced_files(source_path, dest_dir)

        profile = Profile(
            id=profile_id,
            name=name,
            config=str(dest_config),
            imported_at=time.time(),
        )
        self.profiles.append(profile)
        self.profiles.sort(key=lambda item: item.name.lower())
        self.save()
        return profile

    def unique_name(self, name: str) -> str:
        base_name = name.strip() or "OpenVPN profile"
        existing = {profile.name for profile in self.profiles}
        if base_name not in existing:
            return base_name

        index = 2
        while f"{base_name} {index}" in existing:
            index += 1
        return f"{base_name} {index}"

    def remove(self, profile: Profile) -> None:
        self.profiles = [item for item in self.profiles if item.id != profile.id]
        self.save()

        profile_path = profile.path.resolve()
        try:
            profile_root = profile_path.parent
            if profile_root.parent == PROFILE_DIR.resolve():
                shutil.rmtree(profile_root)
        except OSError:
            pass


class OpenVpnGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("OpenVPN GUI")
        self.geometry("780x520")
        self.minsize(640, 420)

        self.store = ProfileStore()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.active_profile: Profile | None = None
        self.reader_thread: threading.Thread | None = None
        self.pid_file: Path | None = None
        self.status_file: Path | None = None

        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

        self.profile_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Disconnected")
        self.command_var = tk.StringVar(value="Ready")

        self._build_ui()
        self.refresh_profiles()
        self.after(150, self._drain_log_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill=BOTH, expand=True)

        top = ttk.Frame(root)
        top.pack(fill=X)

        ttk.Label(top, text="Profile").pack(side=LEFT)
        self.profile_combo = ttk.Combobox(top, textvariable=self.profile_var, state="readonly")
        self.profile_combo.pack(side=LEFT, fill=X, expand=True, padx=(8, 8))

        ttk.Button(top, text="Import", command=self.import_profile).pack(side=LEFT, padx=(0, 6))
        ttk.Button(top, text="Delete", command=self.delete_profile).pack(side=LEFT)

        actions = ttk.Frame(root)
        actions.pack(fill=X, pady=(12, 8))

        self.connect_button = ttk.Button(actions, text="Connect", command=self.connect)
        self.connect_button.pack(side=LEFT)

        self.disconnect_button = ttk.Button(actions, text="Disconnect", command=self.disconnect, state=DISABLED)
        self.disconnect_button.pack(side=LEFT, padx=(8, 0))

        ttk.Label(actions, textvariable=self.status_var).pack(side=RIGHT)

        status_line = ttk.Label(root, textvariable=self.command_var)
        status_line.pack(fill=X, pady=(0, 8))

        log_frame = ttk.LabelFrame(root, text="OpenVPN Log", padding=8)
        log_frame.pack(fill=BOTH, expand=True)

        self.log_text = tk.Text(log_frame, height=18, wrap="word", state=DISABLED)
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)

        bottom = ttk.Frame(root)
        bottom.pack(fill=X, pady=(8, 0))
        ttk.Button(bottom, text="Clear Log", command=self.clear_log).pack(side=RIGHT)

    def refresh_profiles(self) -> None:
        names = [profile.name for profile in self.store.profiles]
        self.profile_combo["values"] = names
        if names and self.profile_var.get() not in names:
            self.profile_var.set(names[0])
        elif not names:
            self.profile_var.set("")

    def selected_profile(self) -> Profile | None:
        name = self.profile_var.get()
        return next((profile for profile in self.store.profiles if profile.name == name), None)

    def import_profile(self) -> None:
        source = filedialog.askopenfilename(
            title="Import OpenVPN Profile",
            filetypes=[("OpenVPN profiles", "*.ovpn *.conf"), ("All files", "*.*")],
        )
        if not source:
            return

        source_path = Path(source)
        if not source_path.exists():
            messagebox.showerror("Import failed", "The selected profile does not exist.")
            return

        default_name = source_path.stem.replace("_", " ").replace("-", " ").strip() or "OpenVPN profile"
        name = simpledialog.askstring("Profile name", "Save profile as:", initialvalue=default_name, parent=self)
        if not name:
            return

        try:
            profile = self.store.add(source_path, name.strip())
        except OSError as exc:
            messagebox.showerror("Import failed", str(exc))
            return

        self.refresh_profiles()
        self.profile_var.set(profile.name)
        self.append_log(f"Imported profile: {profile.name}\n")

    def delete_profile(self) -> None:
        profile = self.selected_profile()
        if not profile:
            return

        if self.active_profile and self.active_profile.id == profile.id:
            messagebox.showwarning("Profile is active", "Disconnect before deleting the active profile.")
            return

        if not messagebox.askyesno("Delete profile", f"Delete '{profile.name}'?"):
            return

        self.store.remove(profile)
        self.refresh_profiles()
        self.append_log(f"Deleted profile: {profile.name}\n")

    def connect(self) -> None:
        if self.process and self.process.poll() is None:
            return

        profile = self.selected_profile()
        if not profile:
            messagebox.showinfo("No profile", "Import an OpenVPN profile first.")
            return
        if not profile.path.exists():
            messagebox.showerror("Missing profile", f"Profile file not found:\n{profile.path}")
            return

        openvpn = shutil.which("openvpn")
        if not openvpn:
            messagebox.showerror("OpenVPN not found", "Install openvpn first, then try again.")
            return

        self.pid_file = RUNTIME_DIR / f"{profile.id}.pid"
        self.status_file = RUNTIME_DIR / f"{profile.id}.status"
        remove_if_exists(self.pid_file)
        remove_if_exists(self.status_file)

        command = self._openvpn_command(openvpn, profile.path, self.pid_file, self.status_file)
        self.append_log(f"Starting: {' '.join(command)}\n")

        try:
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            messagebox.showerror("Connection failed", str(exc))
            return

        self.active_profile = profile
        self.status_var.set(f"Connecting: {profile.name}")
        self.command_var.set("Waiting for OpenVPN output...")
        self.connect_button.configure(state=DISABLED)
        self.disconnect_button.configure(state=NORMAL)
        self.profile_combo.configure(state=DISABLED)

        self.reader_thread = threading.Thread(target=self._read_process_output, daemon=True)
        self.reader_thread.start()
        self.after(1000, self._watch_process)

    def disconnect(self) -> None:
        if not self.process and not self.pid_file:
            self._set_disconnected()
            return

        self.command_var.set("Disconnecting...")
        pid = read_pid(self.pid_file) if self.pid_file else None

        if pid:
            try:
                subprocess.run(self._kill_command(pid), check=False, timeout=10)
            except OSError as exc:
                self.append_log(f"Failed to stop OpenVPN pid {pid}: {exc}\n")

        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass

        self.after(800, self._watch_process)

    def clear_log(self) -> None:
        self.log_text.configure(state=NORMAL)
        self.log_text.delete("1.0", END)
        self.log_text.configure(state=DISABLED)

    def append_log(self, text: str) -> None:
        self.log_text.configure(state=NORMAL)
        self.log_text.insert(END, text)
        self.log_text.see(END)
        self.log_text.configure(state=DISABLED)

    def _openvpn_command(self, openvpn: str, config: Path, pid_file: Path, status_file: Path) -> list[str]:
        args = [
            openvpn,
            "--config",
            str(config),
            "--writepid",
            str(pid_file),
            "--status",
            str(status_file),
            "5",
            "--verb",
            "3",
        ]

        if os.geteuid() == 0:
            return args

        pkexec = shutil.which("pkexec")
        if pkexec:
            return [pkexec, *args]

        sudo = shutil.which("sudo")
        if sudo:
            return [sudo, *args]

        return args

    def _kill_command(self, pid: int) -> list[str]:
        if os.geteuid() == 0:
            return ["kill", "-TERM", str(pid)]

        pkexec = shutil.which("pkexec")
        if pkexec:
            return [pkexec, "kill", "-TERM", str(pid)]

        sudo = shutil.which("sudo")
        if sudo:
            return [sudo, "kill", "-TERM", str(pid)]

        return ["kill", "-TERM", str(pid)]

    def _read_process_output(self) -> None:
        assert self.process is not None
        if self.process.stdout is None:
            return

        for line in self.process.stdout:
            self.log_queue.put(line)
            lower = line.lower()
            if "initialization sequence completed" in lower:
                self.log_queue.put("__STATUS__:connected")
            elif "auth_failed" in lower or "authentication failed" in lower:
                self.log_queue.put("__STATUS__:auth_failed")

    def _drain_log_queue(self) -> None:
        while True:
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break

            if item == "__STATUS__:connected":
                name = self.active_profile.name if self.active_profile else "profile"
                self.status_var.set(f"Connected: {name}")
                self.command_var.set("VPN tunnel is up.")
            elif item == "__STATUS__:auth_failed":
                self.status_var.set("Authentication failed")
                self.command_var.set("Check the OpenVPN log for credential details.")
            else:
                self.append_log(item)

        self.after(150, self._drain_log_queue)

    def _watch_process(self) -> None:
        if not self.process:
            return

        return_code = self.process.poll()
        if return_code is None:
            self.after(1000, self._watch_process)
            return

        self.append_log(f"\nOpenVPN exited with code {return_code}.\n")
        self._set_disconnected()

    def _set_disconnected(self) -> None:
        self.process = None
        self.active_profile = None
        remove_if_exists(self.pid_file)
        remove_if_exists(self.status_file)
        self.pid_file = None
        self.status_file = None
        self.status_var.set("Disconnected")
        self.command_var.set("Ready")
        self.connect_button.configure(state=NORMAL)
        self.disconnect_button.configure(state=DISABLED)
        self.profile_combo.configure(state="readonly")

    def _on_close(self) -> None:
        if self.process and self.process.poll() is None:
            keep_running = messagebox.askyesno(
                "VPN is connected",
                "OpenVPN is still running. Disconnect before closing?",
            )
            if keep_running:
                self.disconnect()
                self.after(1000, self.destroy)
                return
        self.destroy()


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value or "profile"


def copy_referenced_files(config_path: Path, destination: Path) -> None:
    source_dir = config_path.parent

    try:
        lines = config_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "<")):
            continue

        parts = split_openvpn_line(stripped)
        if len(parts) < 2:
            continue

        directive = parts[0].lower()
        reference = parts[1]
        if directive not in COPY_DIRECTIVES or reference in {"stdin", "none"}:
            continue

        ref_path = Path(reference).expanduser()
        if ref_path.is_absolute():
            continue

        source_file = (source_dir / ref_path).resolve()
        if not source_file.is_file():
            continue

        dest_file = destination / ref_path
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source_file, dest_file)
        except OSError:
            continue


def split_openvpn_line(line: str) -> list[str]:
    try:
        import shlex

        return shlex.split(line, comments=True, posix=True)
    except ValueError:
        return line.split()


def read_pid(path: Path | None) -> int | None:
    if not path or not path.exists():
        return None

    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def remove_if_exists(path: Path | None) -> None:
    if not path:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def main() -> int:
    if sys.platform != "linux":
        print("This GUI is designed for Linux.", file=sys.stderr)

    app = OpenVpnGui()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
