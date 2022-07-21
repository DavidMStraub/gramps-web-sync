# Gramps Web API Sync Addon

Development version of a [Gramps](https://gramps-project.org/blog/) addon to synchronize a local family tree with a [Gramps Web API](https://github.com/gramps-project/gramps-webapi/) instance

## Project status

The addon is ready to be tested by audacious beta testers, but please use on a test database or back up your production database everytime you sync. Please report issues and success!

Once the addon has reached stable status, I will request addition to https://github.com/gramps-project/addons-source/.

## Installation

This addon requires Gramps 5.1 running on Python 3.6 or newer, as well as a Gramps Web API instance running version 0.3.0 or newer.

[As usual](https://www.gramps-project.org/wiki/index.php/5.1_Addons#Manually_installed_Addons), to manually install the addon:

- [Download the files](https://github.com/DavidMStraub/gramps-addon-webapisync/archive/refs/heads/main.zip)
- Unzip and save the contents to your [Gramps User Directory](https://www.gramps-project.org/wiki/index.php/Gramps_5.1_Wiki_Manual_-_User_Directory) in the `gramps51/plugins`
- Restart Gramps

Optional step:

- Install `keyring` (e.g. `sudo apt install python3-keyring` or `pip install keyring`) to allow storing the API password safely in your system's password manager 

## Usage

Once installed, the addon should be availabe in Gramps under Tools > Family Tree Processing > Web API Sync. Once started, and after confirming the dialog that the undo history will be discarded, the tool will ask you for the base URL (example: `https://myapi.mydomain.com/`) of your Web API instance, your username, and password. You need an account with owner privileges To sync changes back to your remote database. The username and URL will be stored in plain text in your Gramps user directory, the password will only be stored if `keyring` is installed (see above).

If there are any changes, the tool will display a confirmation dialog before applying the changes. (At present, the confirmation dialog only shows the Gramps IDs of the affected objects.)

## How it works

This tool is meant to keep a local Gramps database in sync with a remote database served via the Gramps Web API, to allow both local and remote changes (collaborative editing).

It is **not suited**

- To synchronize with a database that is not direct derivative (starting from a database copy or Gramps XML export/import) of the local database
- To merge two databases with a large number of changes on both sides that need manual attention for merging. Use the excellent [Import Merge Tool](https://www.gramps-project.org/wiki/index.php/Import_Merge_Tool) for this purpose.

The principles of operation of this tool are very simple:

- It compares the local and remote databases
- If there are any differences, it checks the timestamp of the latest identical object, let's call it **t**
- If an object changed more recently than **t** exists in one database but not the other, it is synced to both (assume new object)
- If an object changed the last time before **t** is absent in one database, it is deleted in both (assume deleted object)
- If an object is different but changed after **t** only in one database, sync to the other one (assume modified object)
- If an object is different but changed after **t** in both databases, merge them (assume conflicting modification)

This algorithm is simple and robust as it does not require tracking syncrhonization history. However, it works best when you *synchronize often*.

## Media file synchronization

*After* the databases have been synchronized, the tool checks for any new or updated media files. It displays the files missing locally or on the remote server and, upon user confirmation, tries to download and upload the files.

Limitations:

- If a local file has a different checksum from the one stored in the Gramps database (this can happen e.g. for Word files when edited after being added to Gramps), the upload will fail with an error message.
- The tool does not check integrity of all local files, so if a local file exist under the path stored for the media object, but the file is different from the file on the server, the tool will not detect it. Use the Media Verify Addon to detect files with incorrect checksums.
