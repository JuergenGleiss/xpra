# This file is part of Xpra.
# Copyright (C) 2010-2024 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os.path
from threading import Lock
from typing import Any
from collections.abc import Callable

from xpra.os_util import gi_import, WIN32
from xpra.util.env import envint, envbool
from xpra.util.thread import start_thread
from xpra.server.background_worker import add_work_item
from xpra.log import Logger

GLib = gi_import("GLib")
Gio = gi_import("Gio")

log = Logger("menu")

MENU_WATCHER = envbool("XPRA_MENU_WATCHER", not WIN32)
MENU_RELOAD_DELAY = envint("XPRA_MENU_RELOAD_DELAY", 5)
EXPORT_MENU_DATA = envbool("XPRA_EXPORT_MENU_DATA", True)


def noicondata(menu_data: dict) -> dict:
    newdata = {}
    for k, v in menu_data.items():
        if k in ("IconData", b"IconData"):
            continue
        if isinstance(v, dict):
            newdata[k] = noicondata(v)
        else:
            newdata[k] = v
    return newdata


singleton = None


def get_menu_provider():
    global singleton
    if singleton is None:
        singleton = MenuProvider()
    return singleton


class MenuProvider:
    __slots__ = (
        "dir_watchers", "menu_reload_timer",
        "on_reload", "menu_data", "desktop_sessions", "load_lock",
    )

    def __init__(self):
        self.dir_watchers: dict[str, Any] = {}
        self.menu_reload_timer = 0
        self.on_reload: list[Callable] = []
        self.menu_data: dict[str, Any] | None = None
        self.desktop_sessions: dict[str, Any] | None = None
        self.load_lock = Lock()

    def setup(self) -> None:
        if not EXPORT_MENU_DATA:
            return
        if MENU_WATCHER:
            self.setup_menu_watcher()
        self.load_menu_data()

    def cleanup(self) -> None:
        self.on_reload = []
        self.cancel_menu_reload()
        self.cancel_dir_watchers()

    def setup_menu_watcher(self) -> None:
        try:
            self.do_setup_menu_watcher()
        except Exception as e:
            log("threaded_setup()", exc_info=True)
            log.error("Error setting up menu watcher:")
            log.estr(e)

    def do_setup_menu_watcher(self) -> None:
        def directory_changed(*args):
            log(f"directory_changed{args}")
            self.schedule_menu_reload()

        from xpra.platform.paths import get_system_menu_dirs
        for menu_dir in get_system_menu_dirs():
            if not os.path.exists(menu_dir) or menu_dir in self.dir_watchers:
                continue
            gfile = Gio.File.new_for_path(menu_dir)
            monitor = gfile.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            monitor.connect("changed", directory_changed)
            self.dir_watchers[menu_dir] = monitor
        if self.dir_watchers:
            log.info("watching for applications menu changes in:")
            for wd in self.dir_watchers.keys():
                log.info(f" {wd!r}")

    def cancel_dir_watchers(self) -> None:
        dw = self.dir_watchers
        self.dir_watchers = {}
        for monitor in dw.values():
            monitor.cancel()

    def load_menu_data(self, force_reload: bool = False) -> None:
        # start loading in a thread,
        # as this may take a while and
        # so server startup can complete:
        def load() -> None:
            try:
                self.get_menu_data(force_reload)
                self.get_desktop_sessions()
            except ImportError as e:
                log.warn("Warning: cannot load menu data")
                log.warn(f" {e}")
            except Exception:
                log.error("Error loading menu data", exc_info=True)
            finally:
                self.clear_cache()

        start_thread(load, "load-menu-data", True)

    def get_menu_data(self, force_reload=False, remove_icons=False, wait=True) -> dict[str, Any]:
        log("get_menu_data%s", (force_reload, remove_icons, wait))
        if not EXPORT_MENU_DATA:
            return {}
        menu_data = self.menu_data
        if self.load_lock.acquire(wait):  # pylint: disable=consider-using-with
            menu_data = self.menu_data
            try:
                if menu_data is None or force_reload:
                    from xpra.platform.menu_helper import load_menu  # pylint: disable=import-outside-toplevel
                    menu_data = self.menu_data = load_menu()
                    add_work_item(self.got_menu_data)
            finally:
                self.load_lock.release()
        if remove_icons and self.menu_data:
            menu_data = noicondata(self.menu_data)
        return menu_data or {}

    def got_menu_data(self) -> bool:
        log("got_menu_data(..) on_reload=%s", self.on_reload)
        for cb in self.on_reload:
            cb(self.menu_data)
        return False

    def clear_cache(self) -> None:
        from xpra.platform.menu_helper import clear_cache  # pylint: disable=import-outside-toplevel
        log("%s()", clear_cache)
        clear_cache()

    def cancel_menu_reload(self) -> None:
        xmrt = self.menu_reload_timer
        if xmrt:
            self.menu_reload_timer = 0
            GLib.source_remove(xmrt)

    def schedule_menu_reload(self) -> None:
        self.cancel_menu_reload()
        self.menu_reload_timer = GLib.timeout_add(MENU_RELOAD_DELAY * 1000, self.menu_reload)

    def menu_reload(self) -> bool:
        self.menu_reload_timer = 0
        log("menu_reload()")
        self.load_menu_data(True)
        return False

    def get_menu_icon(self, category_name: str, app_name: str) -> tuple[str, bytes]:
        menu_data = self.get_menu_data()
        if not menu_data:
            return "", b""
        category = menu_data.get(category_name)
        if not category:
            log("get_menu_icon: invalid menu category '%s'", category_name)
            return "", b""
        if not app_name:
            return category.get("IconType"), category.get("IconData")
        entries = category.get("Entries")
        if not entries:
            log("get_menu_icon: no entries for category '%s'", category_name)
            return "", b""
        app = entries.get(app_name)
        if not app:
            log("get_menu_icon: no matching application for '%s' in category '%s'",
                app_name, category_name)
            return "", b""
        return app.get("IconType", ""), app.get("IconData", b"")

    def get_desktop_sessions(self, force_reload: bool = False, remove_icons: bool = False) -> dict[str, Any]:
        if force_reload or self.desktop_sessions is None:
            from xpra.platform.menu_helper import load_desktop_sessions  # pylint: disable=import-outside-toplevel
            self.desktop_sessions = load_desktop_sessions()
        desktop_sessions = self.desktop_sessions
        if remove_icons:
            desktop_sessions = noicondata(desktop_sessions)
        return desktop_sessions

    def get_desktop_menu_icon(self, sessionname: str) -> tuple[str, bytes]:
        desktop_sessions = self.get_desktop_sessions(False) or {}
        de = desktop_sessions.get(sessionname, {})
        return de.get("IconType", ""), de.get("IconData")

    def get_info(self, _proto) -> dict[str, Any]:
        return self.get_menu_data(remove_icons=True)
