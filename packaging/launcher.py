"""Entry point for the PyInstaller-bundled `nightcool.exe`.

Runs the daemon (polling loop) and the FastAPI server in the same process so
non-technical users get a single executable that does everything. The daemon
runs in a thread; FastAPI runs uvicorn on the main thread. The user's
config.yaml + state.json live next to the executable.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path


def _bundle_dir() -> Path:
    """Return the directory the user's config + state should live in."""
    if getattr(sys, "frozen", False):
        # PyInstaller: data files are in sys._MEIPASS, but the user-editable
        # config + state live next to the executable so an upgrade doesn't
        # nuke them.
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _bootstrap_config(target: Path) -> None:
    """If config.yaml is missing, copy a minimal starter alongside the exe."""
    if target.exists():
        return
    starter = """# NightCool starter config.
location:
  address: "PUT YOUR ADDRESS HERE"
  timezone: "America/Denver"
schedule:
  mon: { target_f: 68, leave_at: "07:30" }
  tue: { target_f: 68, leave_at: "07:30" }
  wed: { target_f: 68, leave_at: "07:30" }
  thu: { target_f: 68, leave_at: "07:30" }
  fri: { target_f: 68, leave_at: "07:30" }
  sat: { target_f: 68, home_all_day: true }
  sun: { target_f: 68, home_all_day: true }
windows:
  - { id: br1, name: "Bedroom", exposure: exposed, security: secure }
notifications:
  service: console
"""
    target.write_text(starter, encoding="utf-8")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bundle = _bundle_dir()
    config_path = bundle / "config.yaml"
    state_path = bundle / "state.json"
    _bootstrap_config(config_path)

    os.chdir(bundle)

    from nightcool.config import load_config
    from nightcool.daemon import run_daemon
    from nightcool.web import run_server

    cfg = load_config(config_path)

    daemon_thread = threading.Thread(
        target=run_daemon, args=(cfg, state_path), daemon=True, name="nightcool-daemon"
    )
    daemon_thread.start()

    def _open_browser() -> None:
        import time
        time.sleep(1.5)
        webbrowser.open(f"http://{cfg.web.host}:{cfg.web.port}")

    threading.Thread(target=_open_browser, daemon=True).start()
    run_server(cfg, state_path, config_path=config_path)


if __name__ == "__main__":
    main()
