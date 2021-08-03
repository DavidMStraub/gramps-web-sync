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
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from typing import Optional, Tuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from gramps.gen.db.base import DbReadBase
from gramps.gen.db.utils import import_as_dict
from gramps.gui.plug.tool import BatchTool, ToolOptions


class WebApiSyncTool(BatchTool):
    """Main class for the Web API Sync tool."""

    def __init__(self, dbstate, user, options_class, name, *args, **kwargs) -> None:
        super().__init__(dbstate, user, options_class, name)
        if self.fail:
            return
        db1 = dbstate.db
        db2 = self.get_remote_db()
        if db2 is None:
            return
        self.sync = WebApiSyncDiffHandler(db1, db2)

    def get_api_credentials(self) -> Tuple[str, str, str]:
        """Get the API credentials."""
        return "url", "username", "password"  # FIXME

    def get_remote_db(self) -> Optional[DbReadBase]:
        """Download the remote data and return it as in-memory database."""
        url, username, password = self.get_api_credentials()
        self.api = WebApiHandler(url, username, password)
        path = self.api.download_xml()
        db2 = import_as_dict(str(path), self._user)
        path.unlink()  # delete temporary file
        return db2


class WebApiSyncOptions(ToolOptions):
    """Options for Web API Sync."""


class WebApiHandler:
    """Web API connection handler."""

    def __init__(self, url: str, username: str, password: str) -> None:
        """Initialize given URL, user name, and password."""
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self._access_token: Optional[str] = None

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
        try:
            res = urlopen(req)
        except HTTPError:
            raise
        try:
            res_json = json.load(res)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise
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
                chunk = res.read(chunk_size)
                temp.write(chunk)
        except HTTPError as exc:
            print(exc.code)
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

    def apply_changes(self, trans):
        """Apply the changes to the remote database."""
        raise NotImplementedError


class WebApiSyncDiffHandler:
    """Class managing the difference between two databases."""

    def __init__(self, db1: DbReadBase, db2: DbReadBase) -> None:
        self.db1 = db1
        self.db2 = db2
