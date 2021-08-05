# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2017       Paul Culley <paulr2787@gmail.com>
# Copyright (C) 2018       Serge Noiraud
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

import gzip
import json
import os
from copy import deepcopy
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import gramps
from gi.repository import Gtk
from gramps.gen.config import config as configman
from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gen.db import KEY_TO_CLASS_MAP, DbTxn
from gramps.gen.db.base import DbReadBase
from gramps.gen.db.dbconst import TXNADD, TXNDEL, TXNUPD
from gramps.gen.db.txn import DbTxn
from gramps.gen.db.utils import import_as_dict
from gramps.gen.lib.primaryobj import BasicPrimaryObject as GrampsObject
from gramps.gen.lib.serialize import to_json
from gramps.gen.merge.diff import diff_dbs
from gramps.gen.user import User
from gramps.gui.dialog import ErrorDialog, QuestionDialog2, WarningDialog
from gramps.gui.managedwindow import ManagedWindow
from gramps.gui.plug.tool import BatchTool, ToolOptions
from gramps.gui.utils import ProgressMeter

try:
    _trans = glocale.get_addon_translator(__file__)
except ValueError:
    _trans = glocale.translation
_ = _trans.gettext
ngettext = _trans.ngettext


OBJ_LST = [
    "Family",
    "Person",
    "Citation",
    "Event",
    "Media",
    "Note",
    "Place",
    "Repository",
    "Source",
    "Tag",
]

# actions: add, delete, update, merge - local/remote
A_ADD_LOC = "add_local"
A_ADD_REM = "add_remote"
A_DEL_LOC = "del_local"
A_DEL_REM = "del_remote"
A_UPD_LOC = "upd_local"
A_UPD_REM = "upd_remote"
A_MRG_REM = "mrg_remote"

Action = Tuple[int, str, str, Optional[GrampsObject], Optional[GrampsObject]]
Actions = List[Action]


class WebApiSyncTool(BatchTool):
    """Main class for the Web API Sync tool."""

    def __init__(self, dbstate, user, options_class, name, *args, **kwargs) -> None:
        super().__init__(dbstate, user, options_class, name)
        self.config = configman.register_manager("webapisync")
        self.config.register("credentials.url", "")
        self.config.register("credentials.username", "")
        self.config.register("credentials.password", "")
        self.config.load()
        if self.fail:
            return
        db1 = dbstate.db
        db2 = self.get_remote_db()
        if db2 is None:
            return
        self.sync = WebApiSyncDiffHandler(db1, db2, user=self._user)
        self.actions = self.sync.get_actions()
        self.diff_dialog()

    def get_remote_db(self) -> Optional[DbReadBase]:
        """Download the remote data and return it as in-memory database."""
        login = LoginDialog(
            self._user.uistate,
            url=self.config.get("credentials.url"),
            username=self.config.get("credentials.username"),
            password=self.config.get("credentials.password"),
        )
        credentials = login.run()
        if credentials is None:
            return None
        url, username, password = credentials
        self.config.set("credentials.url", url)
        self.config.set("credentials.username", username)
        self.config.set("credentials.password", password)
        self.config.save()
        self._progress = ProgressMeter(
            _("Web API Sync"), _("Downloading remote data...")
        )
        self.api = WebApiHandler(url, username, password, download_callback=self._progress.step)
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

    def handle_server_errors(self, callback: Callable):
        """Handle server errors while executing a function."""
        try:
            return callback()
        except HTTPError as exc:
            if exc.code == 401:
                ErrorDialog(_("Server authorization error."))
            else:
                ErrorDialog(_("Error connecting to server."))
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

    def commit(self):
        """Commit all changes to the databases."""
        msg = "Apply Web API Sync changes"
        with DbTxn(msg, self.sync.db1) as trans1:
            with DbTxn(msg, self.sync.db2) as trans2:
                self.sync.commit_actions(self.actions, trans1, trans2)
                self.api.commit(trans2)


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


class WebApiHandler:
    """Web API connection handler."""

    def __init__(self, url: str, username: str, password: str, download_callback: Optional[Callable] = None) -> None:
        """Initialize given URL, user name, and password."""
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self._access_token: Optional[str] = None
        self.download_callback = download_callback

    @property
    def access_token(self) -> str:
        """Get the access token."""
        if not self._access_token:
            self.fetch_token()
        return self._access_token

    def fetch_token(self) -> str:
        """Fetch an access token."""
        data = json.dumps({"username": self.username, "password": self.password})
        req = Request(
            f"{self.url}/token/",
            data=data.encode(),
            headers={"Content-Type": "application/json"},
        )
        res = urlopen(req)
        try:
            res_json = json.load(res)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.url = f"{self.url}/api"
            return self.fetch_token()
        self._access_token = res_json["access_token"]

    def download_xml(self, retry: bool = True) -> Path:
        """Download an XML export and return the path of the temp file."""
        req = Request(
            f"{self.url}/exporters/gramps/file",
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        try:
            res = urlopen(req)
            temp = NamedTemporaryFile(delete=False)
            chunk_size = 1024
            chunk = res.read(chunk_size)
            temp.write(chunk)
            while chunk:
                if self.download_callback is not None:
                    self.download_callback()
                chunk = res.read(chunk_size)
                temp.write(chunk)
        except HTTPError as exc:
            if exc.code == 401 and retry:
                # in case of 401, retry once with a new token
                sleep(1)  # avoid server-side rate limit
                self.fetch_token()
                return self.download_xml(retry=False)
            raise
        temp.close()
        unzipped_name = f"{temp.name}.gramps"
        with open(unzipped_name, "wb") as fu:
            with gzip.open(temp.name) as fz:
                fu.write(fz.read())
        os.remove(temp.name)
        return Path(unzipped_name)

    def commit(self, trans):
        """Commit the changes to the remote database."""
        payload = transaction_to_json(trans)
        print(payload)  # FIXME


class WebApiSyncDiffHandler:
    """Class managing the difference between two databases."""

    def __init__(self, db1: DbReadBase, db2: DbReadBase, user: User) -> None:
        self.db1 = db1
        self.db2 = db2
        self.user = user
        self._diff_dbs = self.get_diff_dbs()
        self._latest_common_timestamp = self.get_latest_common_timestamp()

    def get_diff_dbs(
        self,
    ) -> Tuple[
        List[Tuple[str, GrampsObject, GrampsObject]],
        List[Tuple[str, GrampsObject]],
        List[Tuple[str, GrampsObject]],
    ]:
        """Return a database diff tuple: changed, missing from 1, missing from 2."""
        return diff_dbs(self.db1, self.db2, user=self.user)

    @property
    def missing_from_db1(self) -> Set[GrampsObject]:
        """Get list of objects missing in db1."""
        return set([obj for (obj_type, obj) in self._diff_dbs[1]])

    @property
    def missing_from_db2(self) -> Set[GrampsObject]:
        """Get list of objects missing in db2."""
        return set([obj for obj_type, obj in self._diff_dbs[2]])

    @property
    def differences(self) -> Set[Tuple[GrampsObject, GrampsObject]]:
        """Get list of objects differing between the two databases."""
        return set([(obj1, obj2) for (obj_type, obj1, obj2) in self._diff_dbs[0]])

    def get_latest_common_timestamp(self) -> int:
        """Get the timestamp of the latest common object."""
        dates = [
            self._get_latest_common_timestamp(class_name) or 0 for class_name in OBJ_LST
        ]
        return max(dates)

    def _get_latest_common_timestamp(self, class_name: str) -> int:
        """Get the timestamp of the latest common object of given type."""
        handles_func = self.db1.method("get_%s_handles", class_name)
        handle_func = self.db1.method("get_%s_from_handle", class_name)
        # all handles in db1
        all_handles = set(handles_func())
        # all handles missing in db2
        missing_in_db2 = set(
            [
                obj.handle
                for obj in self.missing_from_db2
                if obj.__class__.__name__ == class_name
            ]
        )
        # all handles of objects that are different
        different = set(
            [
                obj1.handle
                for obj1, obj2 in self.differences
                if obj1.__class__.__name__ == class_name
            ]
        )
        # handles of all objects that are the same
        same_handles = all_handles - missing_in_db2 - different
        if not same_handles:
            return None
        date = 0
        for handle in same_handles:
            obj = handle_func(handle)
            date = max(date, obj.change)
        return date

    @property
    def modified_in_db1(self) -> Set[Tuple[GrampsObject, GrampsObject]]:
        """Objects that have been modifed in db1."""
        return set(
            [
                (obj1, obj2)
                for (obj1, obj2) in self.differences
                if obj1.change > self._latest_common_timestamp
                and obj2.change <= self._latest_common_timestamp
            ]
        )

    @property
    def modified_in_db2(self) -> Set[Tuple[GrampsObject, GrampsObject]]:
        """Objects that have been modifed in db1."""
        return set(
            [
                (obj1, obj2)
                for (obj1, obj2) in self.differences
                if obj1.change <= self._latest_common_timestamp
                and obj2.change > self._latest_common_timestamp
            ]
        )

    @property
    def modified_in_both(self) -> Set[Tuple[GrampsObject, GrampsObject]]:
        """Objects that have been modifed in both databases."""
        return self.differences - self.modified_in_db1 - self.modified_in_db2

    @property
    def added_to_db1(self) -> Set[GrampsObject]:
        """Objects that have been added to db1."""
        return set(
            [
                obj
                for obj in self.missing_from_db2
                if obj.change > self._latest_common_timestamp
            ]
        )

    @property
    def added_to_db2(self) -> Set[GrampsObject]:
        """Objects that have been added to db2."""
        return set(
            [
                obj
                for obj in self.missing_from_db1
                if obj.change > self._latest_common_timestamp
            ]
        )

    @property
    def deleted_from_db1(self) -> Set[GrampsObject]:
        """Objects that have been deleted from db1."""
        return self.missing_from_db1 - self.added_to_db2

    @property
    def deleted_from_db2(self) -> Set[GrampsObject]:
        """Objects that have been deleted from db2."""
        return self.missing_from_db2 - self.added_to_db1

    def get_summary(self):
        """Get a dictionary summarizing the changes."""

        def obj_info(obj):
            return {"handle": obj, "_class": obj.__class__.__name__}

        return {
            "added to db1": [obj_info(obj) for obj in self.added_to_db1],
            "added to db2": [obj_info(obj) for obj in self.added_to_db2],
            "deleted from db1": [obj_info(obj) for obj in self.deleted_from_db1],
            "deleted from db2": [obj_info(obj) for obj in self.deleted_from_db2],
            "modified in db1": [obj_info(obj) for obj in self.modified_in_db1],
            "modified in db2": [obj_info(obj) for obj in self.modified_in_db2],
            "modified in both": [obj_info(obj) for obj in self.modified_in_both],
        }

    def get_actions(self) -> Actions:
        """Get a list of objects and corresponding actions."""
        lst = []
        for (obj1, obj2) in self.modified_in_both:
            lst.append((A_MRG_REM, obj1.handle, obj1.__class__.__name__, obj1, obj2))
        for obj in self.added_to_db1:
            lst.append((A_ADD_REM, obj.handle, obj.__class__.__name__, obj, None))
        for obj in self.added_to_db2:
            lst.append((A_ADD_LOC, obj.handle, obj.__class__.__name__, None, obj))
        for obj in self.deleted_from_db1:
            lst.append((A_DEL_REM, obj.handle, obj.__class__.__name__, None, obj))
        for obj in self.deleted_from_db2:
            lst.append((A_DEL_LOC, obj.handle, obj.__class__.__name__, obj, None))
        for (obj1, obj2) in self.modified_in_db1:
            lst.append((A_UPD_REM, obj1.handle, obj1.__class__.__name__, obj1, obj2))
        for (obj1, obj2) in self.modified_in_db2:
            lst.append((A_UPD_LOC, obj1.handle, obj1.__class__.__name__, obj1, obj2))
        return lst

    def commit_action(self, action: Action, trans1: DbTxn, trans2: DbTxn) -> None:
        """Commit an action into local and remote transaction objects."""
        typ, handle, obj_type, obj1, obj2 = action
        if typ == A_DEL_LOC:
            self.db1.method("remove_%s", obj_type)(handle, trans1)
        elif typ == A_DEL_REM:
            self.db2.method("remove_%s", obj_type)(handle, trans2)
        elif typ == A_ADD_LOC:
            self.db1.method("add_%s", obj_type)(obj2, trans1)
        elif typ == A_ADD_REM:
            self.db2.method("add_%s", obj_type)(obj1, trans2)
        elif typ == A_UPD_LOC:
            self.db1.method("commit_%s", obj_type)(obj2, trans1)
        elif typ == A_UPD_REM:
            self.db2.method("commit_%s", obj_type)(obj1, trans2)
        elif typ == A_MRG_REM:
            obj_merged = deepcopy(obj2)
            obj1_nogid = deepcopy(obj1)
            obj1_nogid.gramps_id = None
            obj_merged.merge(obj1_nogid)
            self.db1.method("commit_%s", obj_type)(obj_merged, trans1)
            self.db2.method("commit_%s", obj_type)(obj_merged, trans2)

    def commit_actions(self, actions: Actions, trans1: DbTxn, trans2: DbTxn) -> None:
        """Commit several actions into local and remote transaction objects."""
        for action in actions:
            self.commit_action(action, trans1, trans2)


def transaction_to_json(transaction: DbTxn) -> List[Dict[str, Any]]:
    """Return a JSON representation of a database transaction."""
    out = []
    for recno in transaction.get_recnos(reverse=True):
        key, action, handle, old_data, new_data = transaction.get_record(recno)
        try:
            obj_cls_name = KEY_TO_CLASS_MAP[key]
        except KeyError:
            continue  # this happens for references
        trans_dict = {TXNUPD: "update", TXNDEL: "delete", TXNADD: "add"}
        obj_cls = getattr(gramps.gen.lib, obj_cls_name)
        if old_data:
            old_data = obj_cls().unserialize(old_data)
        if new_data:
            new_data = obj_cls().unserialize(new_data)
        item = {
            "type": trans_dict[action],
            "handle": handle,
            "_class": obj_cls_name,
            "old": json.loads(to_json(old_data)),
            "new": json.loads(to_json(new_data)),
        }
        out.append(item)
    return out
