"""Run the control panel:  .venv/bin/python -m ui   (Windows: .venv\\Scripts\\python.exe -m ui)
Then open http://127.0.0.1:8765"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import webbrowser

from aiohttp import web

from core import hostos
from core.config import load_settings
from core.db import DB
from ops import claude_runner
from ui.chat import WebChat
from ui.jobs import Jobs
from ui.server import make_app
from ui.supervisor import OpsSupervisor, Prefs

HOST = "127.0.0.1"


async def amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    port = int(os.environ.get("JARVIS_UI_PORT", "8765"))
    settings = load_settings()
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if hostos.is_windows():  # mode 0o700 does nothing there; limit the folder to this user (once per start)
        hostos.restrict_to_user(settings.state_dir)
    db = DB(settings.db_path)  # the browser chat logs its Claude usage here, next to Ops's
    db.init()
    claude_runner.attach_db(db)
    sup = OpsSupervisor(settings, Prefs(settings.state_dir / "ui.json"))
    stop = asyncio.Event()
    restart = {"wanted": False}

    def request_restart() -> None:
        restart["wanted"] = True
        stop.set()

    app = make_app(settings, sup, WebChat(settings), Jobs(), port=port, on_restart=request_restart, db=db)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HOST, port).start()
    url = f"http://{HOST}:{port}"
    logging.getLogger("ui").info("JARVIS control panel on %s", url)
    sup.adopt()
    if sup.prefs.get("resume_ops", False):  # we restarted ourselves after an update while JARVIS was running
        sup.prefs.set("resume_ops", False)
        logging.getLogger("ui").info("resuming JARVIS after update: %s", (await sup.start())["message"])
    else:
        await sup.autostart_if_wanted()
    if os.environ.get("JARVIS_UI_OPEN") == "1":
        webbrowser.open(url)
    hostos.install_stop_signals(asyncio.get_running_loop(), stop.set)
    await stop.wait()
    await sup.shutdown()
    await runner.cleanup()
    if restart["wanted"]:  # same interpreter, fresh code
        hostos.restart_process([sys.executable, "-m", "ui"])


if __name__ == "__main__":
    asyncio.run(amain())
