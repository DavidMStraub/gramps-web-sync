# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2021       David Straub
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

"""Gramps addon to synchronize with a Gramps Web API server."""

from typing import Callable, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from gi.repository import Gtk
from gramps.gen.config import config as configman
from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gen.db import DbTxn
from gramps.gen.db.base import DbReadBase
from gramps.gen.db.utils import import_as_dict
from gramps.gui.dialog import ErrorDialog, QuestionDialog2, OkDialog
from gramps.gui.managedwindow import ManagedWindow
from gramps.gui.plug.tool import BatchTool, ToolOptions
from gramps.gui.utils import ProgressMeter

from const import (
    A_ADD_LOC,
    A_ADD_REM,
    A_DEL_LOC,
    A_DEL_REM,
    A_MRG_REM,
    A_UPD_LOC,
    A_UPD_REM,
    Actions,
)
from diffhandler import WebApiSyncDiffHandler
from webapihandler import WebApiHandler

try:
    _trans = glocale.get_addon_translator(__file__)
except ValueError:
    _trans = glocale.translation
_ = _trans.gettext
ngettext = _trans.ngettext


def get_password(service: str, username: str) -> Optional[str]:
    """If keyring is installed, return the user's password or None."""
    try:
        import keyring
    except ImportError:
        return None
    return keyring.get_password(service, username)


def set_password(service: str, username: str, password: str) -> None:
    """If keyring is installed, store the user's password."""
    try:
        import keyring
    except ImportError:
        return None
    keyring.set_password(service, username, password)


class WebApiSyncTool(BatchTool):
    """Main class for the Web API Sync tool."""

    def __init__(self, dbstate, user, options_class, name, *args, **kwargs) -> None:
        super().__init__(dbstate, user, options_class, name)
        # load config
        self.config = configman.register_manager("webapisync")
        self.config.register("credentials.url", "")
        self.config.register("credentials.username", "")
        self.config.load()
        if self.fail:
            return
        # load remote db asking for the login credentials
        db1 = dbstate.db
        db2 = self.get_remote_db()
        if db2 is None:
            return
        # get the necessary sync actions
        self.sync = WebApiSyncDiffHandler(db1, db2, user=self._user)
        self.actions = self.sync.get_actions()
        if not self.actions:
            return self.unchanged_dialog()
        # ask the user to confirm
        self.diff_dialog()

    def get_remote_db(self) -> Optional[DbReadBase]:
        """Download the remote data and return it as in-memory database."""
        url = self.config.get("credentials.url")
        username = self.config.get("credentials.username")
        if username:
            password = get_password(url, username)
        else:
            password = None
        login = LoginDialog(
            self._user.uistate, url=url, username=username, password=password,
        )
        credentials = login.run()
        if credentials is None:
            return None
        url, username, password = credentials
        self.config.set("credentials.url", url)
        self.config.set("credentials.username", username)
        set_password(url, username, password)
        self.config.save()
        self._progress = ProgressMeter(
            _("Web API Sync"), _("Downloading remote data...")
        )
        self.api = WebApiHandler(
            url, username, password, download_callback=self._progress.step
        )
        path = self.handle_server_errors(self.api.download_xml)
        self._progress.close()
        if path is None:
            return None
        self._progress = ProgressMeter(
            _("Web API Sync"), _("Processing remote data...")
        )
        # importxml uses the user.callback(percentage) for progress
        # not compatible with usual user progress. So bypass step()
        self._user.callback_function = self._progress_step
        db2 = import_as_dict(str(path), self._user)
        self._progress.close()
        path.unlink()  # delete temporary file
        return db2

    def _progress_step(self, percent):
        """Hack to allow import XML callback progress to work."""
        self._progress._ProgressMeter__pbar_index = percent - 1.0
        self._progress.step()

    def handle_server_errors(self, callback: Callable, *args):
        """Handle server errors while executing a function."""
        try:
            return callback(*args)
        except HTTPError as exc:
            if exc.code == 401:
                ErrorDialog(_("Server authorization error."))
            elif exc.code == 403:
                ErrorDialog(_("Server authorization error: insufficient permissions."))
            elif exc.code == 409:
                ErrorDialog(
                    _(
                        "Unable to synchronize changes to server: objects have been modified."
                    )
                )
            else:
                ErrorDialog(_("Error %s while connecting to server.") % exc.code)
            return None
        except URLError:
            ErrorDialog(_("Error connecting to server."))
            return None
        except ValueError:
            ErrorDialog(_("Error while parsing response from server."))
            return None

    def diff_dialog(self) -> bool:
        """Edit the automatically generated actions via user interaction."""
        dialog = DiffDetailDialog(self._user.uistate, self.actions, on_ok=self.commit)
        dialog.show()

    def unchanged_dialog(self):
        """Return a dialog if nothing has changed."""
        OkDialog(_("Your Tree and import are the same."))

    def commit(self):
        """Commit all changes to the databases."""
        msg = "Apply Web API Sync changes"
        with DbTxn(msg, self.sync.db1) as trans1:
            with DbTxn(msg, self.sync.db2) as trans2:
                self.sync.commit_actions(self.actions, trans1, trans2)
                self.handle_server_errors(self.api.commit, trans2)


class LoginDialog(ManagedWindow):
    """Login dialog."""

    def __init__(self, uistate, url=None, username=None, password=None):
        """Initialize self."""
        self.title = _("Login")
        super().__init__(uistate, [], self.__class__, modal=True)
        dialog = Gtk.Dialog(transient_for=uistate.window)
        dialog.set_default_size(500, -1)
        grid = Gtk.Grid()
        grid.set_border_width(6)
        grid.set_row_spacing(6)
        grid.set_column_spacing(6)
        label = Gtk.Label(label=_("Server URL: "))
        grid.attach(label, 0, 0, 1, 1)
        self.url = Gtk.Entry()
        if url:
            self.url.set_text(url)
        self.url.set_hexpand(True)
        self.url.set_input_purpose(Gtk.InputPurpose.URL)
        grid.attach(self.url, 1, 0, 1, 1)
        label = Gtk.Label(label=_("Username: "))
        grid.attach(label, 0, 1, 1, 1)
        self.username = Gtk.Entry()
        if username:
            self.username.set_text(username)
        self.username.set_hexpand(True)
        grid.attach(self.username, 1, 1, 1, 1)
        label = Gtk.Label(label=_("Password: "))
        grid.attach(label, 0, 2, 1, 1)
        self.password = Gtk.Entry()
        if password:
            self.password.set_text(password)
        self.password.set_hexpand(True)
        self.password.set_visibility(False)
        self.password.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        grid.attach(self.password, 1, 2, 1, 1)
        dialog.vbox.pack_start(grid, True, True, 0)
        dialog.add_buttons(
            _("_Cancel"), Gtk.ResponseType.CANCEL, _("Login"), Gtk.ResponseType.OK
        )
        self.set_window(dialog, None, self.title)

    def run(self) -> Optional[Tuple[str, str, str]]:
        """Run the dialog and return the credentials or None."""
        self.show()
        response = self.window.run()
        url = self.url.get_text()
        url = self.sanitize_url(url)
        username = self.username.get_text()
        password = self.password.get_text()
        self.close()
        if response == Gtk.ResponseType.CANCEL or url is None:
            return None
        elif response == Gtk.ResponseType.OK:
            return url, username, password

    def sanitize_url(self, url: str) -> Optional[str]:
        """Warn if http and prepend https if missing."""
        parsed_url = urlparse(url)
        if parsed_url.scheme == "":
            # if no httpX given, prepend https!
            url = f"https://{url}"
        elif parsed_url.scheme == "http":
            question = QuestionDialog2(
                _("Continue without transport encryption?"),
                _(
                    "You have specified a URL with http scheme. "
                    "If you continue, your password will be sent "
                    "in clear text over the network. "
                    "Use only for local testing!"
                ),
                _("Continue"),
                _("Abort"),
                parent=self.window,
            )
            if not question.run():
                return None
        return url


class DiffDetailDialog(ManagedWindow):
    """Dialog showing the differences before applying them."""

    def __init__(self, uistate, actions: Actions, on_ok: Callable):
        self.title = _("Differences")
        self.on_ok = on_ok
        super().__init__(uistate, [], self.__class__, modal=True)
        # window
        window = Gtk.Window()
        window.set_default_size(600, 400)
        window.set_border_width(6)

        # tree view
        self.store = self.actions_to_store(actions)
        view = Gtk.TreeView(model=self.store)
        # expand first level
        for i, row in enumerate(self.store):
            view.expand_row(Gtk.TreePath(i), False)

        for i, col in enumerate(["ID", "Content"]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(col, renderer, text=i)
            view.append_column(column)

        # scrolled window
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.add(view)

        # buttons
        cancel_btn = Gtk.Button(_("Cancel"))
        ok_btn = Gtk.Button(_("Apply"))
        button_box = Gtk.ButtonBox(Gtk.Orientation.HORIZONTAL)
        button_box.pack_start(cancel_btn, False, False, 0)
        button_box.pack_start(ok_btn, False, False, 0)

        # button callbacks
        cancel_btn.connect("clicked", self._on_cancel)
        ok_btn.connect("clicked", self._on_ok)

        # grid
        box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 5)
        box.set_margin_top(5)
        box.set_margin_start(5)
        box.set_margin_end(5)
        box.set_margin_bottom(5)
        box.pack_start(scrolled_window, True, True, 0)
        box.pack_start(button_box, False, False, 0)

        window.add(box)
        self.set_window(window, None, self.title)

    def _on_cancel(self, widget):
        self.close()

    def _on_ok(self, widget):
        self.close()
        self.on_ok()

    def actions_to_store(self, actions: Actions) -> Gtk.TreeStore:
        """Convert the actions list to a tree store."""
        store = Gtk.TreeStore(str, str)
        action_labels = {
            _("Local changes"): {
                _("Added"): A_ADD_REM,
                _("Deleted"): A_DEL_REM,
                _("Modified"): A_UPD_REM,
            },
            _("Remote changes"): {
                _("Added"): A_ADD_LOC,
                _("Deleted"): A_DEL_LOC,
                _("Modified"): A_UPD_LOC,
            },
            _("Simultaneous changes"): {_("Modified"): A_MRG_REM},
        }

        for label1, v1 in action_labels.items():
            iter1 = store.append(None, [label1, ""])
            for label2, action_type in v1.items():
                rows = []
                for action in actions:
                    _type, handle, class_name, obj1, obj2 = action
                    if _type == action_type:
                        if obj1 is not None:
                            gid = obj1.gramps_id
                        else:
                            gid = obj2.gramps_id
                        obj_details = [class_name, gid]
                        rows.append(obj_details)
                if rows:
                    label2 = f"{label2} ({len(rows)})"
                    iter2 = store.append(iter1, [label2, ""])
                    for row in rows:
                        store.append(iter2, row)
        return store


class WebApiSyncOptions(ToolOptions):
    """Options for Web API Sync."""

