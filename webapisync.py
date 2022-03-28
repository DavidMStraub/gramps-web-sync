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

import threading
from datetime import datetime
from typing import Callable, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from gi.repository import Gtk, GLib, GObject
from gramps.gen.config import config as configman
from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gen.const import URL_MANUAL_PAGE, ICON, SPLASH
from gramps.gen.db import DbTxn
from gramps.gen.db.base import DbReadBase
from gramps.gen.db.utils import import_as_dict
from gramps.gui.dialog import ErrorDialog, QuestionDialog2, OkDialog
from gramps.gui.managedwindow import ManagedWindow
from gramps.gui.plug.tool import BatchTool, ToolOptions
from gramps.gui.utils import ProgressMeter
from gramps.cli.user import User

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


class WebApiSyncTool(BatchTool, ManagedWindow):
    """Main class for the Web API Sync tool."""

    def __init__(self, dbstate, user, options_class, name, *args, **kwargs) -> None:
        BatchTool.__init__(self, dbstate, user, options_class, name)
        ManagedWindow.__init__(self, user.uistate, [], self.__class__)

        self.dbstate = dbstate
        self.callback = self.uistate.pulse_progressbar

        self.config = configman.register_manager("webapisync")
        self.config.register("credentials.url", "")
        self.config.register("credentials.username", "")
        self.config.register("credentials.timestamp", 0)
        self.config.load()

        self.assistant = Gtk.Assistant()
        self.set_window(self.assistant, None, _("Web API Sync"))
        self.setup_configs("interface.webapisync", 780, 600)

        self.assistant.connect("close", self.do_close)
        self.assistant.connect("cancel", self.do_close)
        self.assistant.connect("apply", self.apply)
        self.assistant.connect("prepare", self.prepare)

        self.intro = IntroductionPage(self.assistant)
        self.add_page(self.intro, Gtk.AssistantPageType.INTRO, _("Introduction"))

        self.url = self.config.get("credentials.url")
        self.username = self.config.get("credentials.username")
        self.password = self.get_password()
        self.loginpage = LoginPage(
            self.assistant,
            url=self.url,
            username=self.username,
            password=self.password,
        )
        self.add_page(self.loginpage, Gtk.AssistantPageType.CONTENT, _("Login"))

        self.progress_page = ProgressPage(self.assistant)
        self.add_page(self.progress_page, Gtk.AssistantPageType.PROGRESS, _("Progress"))

        # self.results = ResultsPage(self.assistant)
        # self.add_page(self.results, Gtk.AssistantPageType.CONTENT, _("Results"))

        self.confirmation = ConfirmationPage(self.assistant)
        self.add_page(
            self.confirmation, Gtk.AssistantPageType.CONFIRM, _("Final confirmation")
        )
        self.conclusion = ConclusionPage(self.assistant)
        self.add_page(self.conclusion, Gtk.AssistantPageType.SUMMARY, _("Summary"))

        self.show()
        self.assistant.set_forward_page_func(self.forward_page, None)

        self.api = None

        self.db1 = dbstate.db
        self.db2 = None
        self._download_timestamp = 0
        self.actions = None
        self.sync = None

    def build_menu_names(self, obj):
        """Override :class:`.ManagedWindow` method."""
        return (_("Web API Sync"), None)

    def do_close(self, assistant):
        """Close the assistant."""
        position = self.window.get_position()  # crock
        self.assistant.hide()
        self.window.move(position[0], position[1])
        self.close()

    def forward_page(self, page, data):
        """Specify the next page to be displayed."""
        if self.conclusion.error:
            return 4
        return page + 1

    def add_page(self, page, page_type, title=""):
        """Add a page to the assistant."""
        page.show_all()
        self.assistant.append_page(page)
        self.assistant.set_page_title(page, title)
        self.assistant.set_page_type(page, page_type)

    def prepare(self, assistant, page):
        """Run page preparation code."""
        page.update_complete()
        if page == self.progress_page:
            self.save_credentials()
            url, username, password = self.get_credentials()
            self.api = self.handle_server_errors(
                WebApiHandler, url, username, password, None
            )
            if self.api is None:
                return None
            self.progress_page.label.set_text("Fetching remote data...")
            t = threading.Thread(target=self.async_compare_dbs)
            t.start()
        elif page == self.confirmation:
            self.confirmation.prepare(self.actions)
        elif page == self.conclusion:
            if not self.conclusion.error:
                self.conclusion.label.set_text(
                    f"Successfully synchronized {len(self.actions)} objects."
                )
                self.conclusion.set_complete()

    def apply(self, assistant):
        """Apply the changes."""
        try:
            self.commit()
        except:
            self.handle_error("Unexpected error while applying changes.")

    def get_password(self):
        """Get a stored password."""
        url = self.config.get("credentials.url")
        username = self.config.get("credentials.username")
        if not url or not username:
            return None
        return get_password(url, username)

    def handle_error(self, message):
        """Handle an error message during sync."""
        self.conclusion.error = True
        self.conclusion.label.set_text(message)  #
        self.assistant.next_page()
        self.conclusion.set_complete()

    def handle_unchanged(self):
        """Return a message if nothing has changed."""
        self.handle_error(_("Your Tree and import are the same."))
        self.save_timestamp()

    def async_compare_dbs(self):
        """Download the remote data and import it to an in-memory database."""
        # store timestamp just before downloading the XML
        self._download_timestamp = datetime.now().timestamp()
        GLib.idle_add(self.get_diff_actions)

    def get_diff_actions(self):
        """Download the remote data, import it and compare it to local."""
        path = self.handle_server_errors(self.api.download_xml)
        db2 = import_as_dict(str(path), self._user)
        path.unlink()  # delete temporary file
        self.db2 = db2
        self.progress_page.label.set_text("Comparing local and remote data...")
        timestamp = self.config.get("credentials.timestamp") or None
        self.sync = WebApiSyncDiffHandler(
            self.db1, self.db2, user=self._user, last_synced=timestamp
        )
        self.actions = self.sync.get_actions()
        self.progress_page.label.set_text("")
        self.progress_page.set_complete()
        if len(self.actions) == 0:
            self.handle_unchanged()
        else:
            self.assistant.next_page()

    def handle_server_errors(self, callback: Callable, *args):
        """Handle server errors while executing a function."""
        try:
            return callback(*args)
        except HTTPError as exc:
            if exc.code == 401:
                self.handle_error(_("Server authorization error."))
            elif exc.code == 403:
                self.handle_error(
                    _("Server authorization error: insufficient permissions.")
                )
            elif exc.code == 404:
                self.handle_error(_("Error: URL not found."))
            elif exc.code == 409:
                self.handle_error(
                    _(
                        "Unable to synchronize changes to server: objects have been modified."
                    )
                )
            else:
                self.handle_error(_("Error %s while connecting to server.") % exc.code)
            return None
        except URLError:
            self.handle_error(_("Error connecting to server."))
            return None
        except ValueError:
            self.handle_error(_("Error while parsing response from server."))
            return None

    def save_credentials(self):
        """Save the login credentials."""
        url = self.loginpage.url.get_text()
        url = self.sanitize_url(url)
        username = self.loginpage.username.get_text()
        password = self.loginpage.password.get_text()
        if url != self.config.get("credentials.url"):
            # if URL changed, clear last sync timestamp
            self.config.set("credentials.timestamp", 0)
        self.config.set("credentials.url", url)
        self.config.set("credentials.username", username)
        set_password(url, username, password)
        self.config.save()

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
                _("Continue with HTTP"),
                _("Use HTTPS"),
                parent=self.window,
            )
            if not question.run():
                return url.replace("http", "https")
        return url

    def get_credentials(self):
        """Get a tuple of URL, username, and password."""
        return (
            self.config.get("credentials.url"),
            self.config.get("credentials.username"),
            self.loginpage.password.get_text(),
        )

    def commit(self):
        """Commit all changes to the databases."""
        msg = "Apply Web API Sync changes"
        with DbTxn(msg, self.sync.db1) as trans1:
            with DbTxn(msg, self.sync.db2) as trans2:
                self.sync.commit_actions(self.actions, trans1, trans2)
                self.handle_server_errors(self.api.commit, trans2)
        self.save_timestamp()

    def save_timestamp(self):
        """Save last sync timestamp."""
        self.config.set("credentials.timestamp", self._download_timestamp)
        self.config.save()


class Page(Gtk.Box):
    """Page base class."""

    def __init__(self, assistant: Gtk.Assistant):
        """Initialize self."""
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL)
        self.assistant = assistant
        self._complete = False

    def set_complete(self):
        """Set as complete."""
        self._complete = True
        self.update_complete()

    @property
    def complete(self):
        return self._complete

    def update_complete(self):
        """Set the current page's complete status."""
        page_number = self.assistant.get_current_page()
        current_page = self.assistant.get_nth_page(page_number)
        self.assistant.set_page_complete(current_page, self.complete)


class IntroductionPage(Page):
    """A page containing introductory text."""

    def __init__(self, assistant):
        super().__init__(assistant)
        label = Gtk.Label(label=self.__get_intro_text())
        label.set_line_wrap(True)
        label.set_use_markup(True)
        label.set_max_width_chars(60)

        self.pack_start(label, False, False, 0)
        self._complete = True

    def __get_intro_text(self):
        """Return the introductory text."""
        return _(
            "This tool allows to synchronize the currently opened "
            "family tree with a remote family tree served by Gramps Web.\n\n"
            "The tool assumes that the two trees are derivatives of each other, "
            "i.e. one of the two was created from a Gramps XML (not GEDCOM!) "
            "export of the other.\n\n"
            "After successful synchronization, the two trees will be identical. "
            "Modifications will be propagated based on timestamps. "
            "You will be prompted for confirmation before any changes are made "
            "to the local or remote trees.\n\n"
            "If you instead want to merge two significantly different trees "
            "with the option to make manual modifications, use the Import Merge "
            "Tool instead."
        )


class LoginPage(Page):
    """A page to log in."""

    def __init__(self, assistant, url, username, password):
        super().__init__(assistant)
        self.set_spacing(12)

        grid = Gtk.Grid()
        grid.set_row_spacing(6)
        grid.set_column_spacing(6)
        self.add(grid)

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

        self.url.connect("changed", self.on_entry_changed)
        self.username.connect("changed", self.on_entry_changed)
        self.password.connect("changed", self.on_entry_changed)

    @property
    def complete(self):
        url = self.url.get_text()
        username = self.username.get_text()
        password = self.password.get_text()
        if url and username and password:
            return True
        return False

    def on_entry_changed(self, widget):
        self.update_complete()


class ProgressPage(Page):
    """A page."""

    def __init__(self, assistant):
        super().__init__(assistant)
        label = Gtk.Label(label="")
        label.set_line_wrap(True)
        label.set_use_markup(True)
        label.set_max_width_chars(60)
        self.label = label
        self.pack_start(self.label, False, False, 0)


class ConfirmationPage(Page):
    """Page showing the differences before applying them."""

    # def diff_dialog(self) -> bool:
    #     """Edit the automatically generated actions via user interaction."""
    #     dialog = DiffDetailDialog(self._user.uistate, self.actions, on_ok=self.commit)
    #     dialog.show()

    def __init__(self, assistant):
        super().__init__(assistant)
        self.store = Gtk.TreeStore(str, str)

        # tree view
        self.tree_view = Gtk.TreeView(model=self.store)

        for i, col in enumerate(["ID", "Content"]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(col, renderer, text=i)
            self.tree_view.append_column(column)

        # scrolled window
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.add(self.tree_view)

        self.pack_start(scrolled_window, True, True, 0)

    def prepare(self, actions: Actions):
        """Convert the actions list to a tree store."""
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
            iter1 = self.store.append(None, [label1, ""])
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
                    iter2 = self.store.append(iter1, [label2, ""])
                    for row in rows:
                        self.store.append(iter2, row)

        # expand first level
        for i, row in enumerate(self.store):
            self.tree_view.expand_row(Gtk.TreePath(i), False)

        self.set_complete()


class ConclusionPage(Page):
    """The conclusion page."""

    def __init__(self, assistant):
        super().__init__(assistant)
        self.error = False
        label = Gtk.Label(label="")
        label.set_line_wrap(True)
        label.set_use_markup(True)
        label.set_max_width_chars(60)
        self.label = label
        self.pack_start(self.label, False, False, 0)


class WebApiSyncOptions(ToolOptions):
    """Options for Web API Sync."""
