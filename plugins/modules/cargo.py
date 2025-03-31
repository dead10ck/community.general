#!/usr/bin/python
# Copyright (c) 2021 Radek Sprta <mail@radeksprta.eu>
# Copyright (c) 2024 Colin Nolan <cn580@alumni.york.ac.uk>
# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: cargo
short_description: Manage Rust packages with cargo
version_added: 4.3.0
description:
  - Manage Rust packages with cargo.
author: "Radek Sprta (@radek-sprta)"
extends_documentation_fragment:
  - community.general.attributes
attributes:
  check_mode:
    support: full
  diff_mode:
    support: none
options:
  executable:
    description:
      - Path to the C(cargo) installed in the system.
      - If not specified, the module looks for C(cargo) in E(PATH).
    type: path
    version_added: 7.5.0
  name:
    description:
      - The name of a Rust package to install.
    type: list
    elements: str
    required: true
  path:
    description: The base path where to install the Rust packages. Cargo automatically appends V(/bin). In other words, V(/usr/local)
      becomes V(/usr/local/bin).
    type: path
  version:
    description: The version to install. If O(name) contains multiple values, the module tries to install all of them in this
      version.
    type: str
  locked:
    description:
      - Install with locked dependencies.
      - This is only used when installing packages.
    type: bool
    default: false
    version_added: 7.5.0
  state:
    description:
      - The state of the Rust package.
    type: str
    default: present
    choices: ["present", "absent", "latest"]
  directory:
    description:
      - Path to the source directory to install the Rust package from.
      - This is only used when installing packages.
    type: path
    version_added: 9.1.0
  features:
    description:
      - List of features to activate.
      - This is only used when installing packages.
    type: list
    elements: str
    default: []
    version_added: 11.0.0
requirements:
  - cargo installed
"""

EXAMPLES = r"""
- name: Install "ludusavi" Rust package
  community.general.cargo:
    name: ludusavi

- name: Install "ludusavi" Rust package with locked dependencies
  community.general.cargo:
    name: ludusavi
    locked: true

- name: Install "ludusavi" Rust package in version 0.10.0
  community.general.cargo:
    name: ludusavi
    version: '0.10.0'

- name: Install "ludusavi" Rust package to global location
  community.general.cargo:
    name: ludusavi
    path: /usr/local

- name: Remove "ludusavi" Rust package
  community.general.cargo:
    name: ludusavi
    state: absent

- name: Update "ludusavi" Rust package its latest version
  community.general.cargo:
    name: ludusavi
    state: latest

- name: Install "ludusavi" Rust package from source directory
  community.general.cargo:
    name: ludusavi
    directory: /path/to/ludusavi/source

- name: Install "serpl" Rust package with ast_grep feature
  community.general.cargo:
    name: serpl
    features:
      - ast_grep
"""

import json
import os
import re
import sys
import typing
from pathlib import Path
from typing import Collection, Optional, Tuple, Union
from urllib.parse import urlparse

from ansible.module_utils.basic import AnsibleModule


class Cargo:
    def __init__(self, module: AnsibleModule, **kwargs):
        self.module = module
        self._installed: packages_type = {}
        self._latest: packages_type = {}
        self._cargo_home = self._get_cargo_home()
        self._crates_json = self._cargo_home / ".crates2.json"

        self.executable = [kwargs["executable"] or module.get_bin_path("cargo", True)]
        self.name = kwargs["name"]
        self.path = kwargs["path"]
        self.state = kwargs["state"]
        self.version = kwargs["version"]
        self.locked = kwargs["locked"]
        self.directory = kwargs["directory"]
        self.features = kwargs["features"]

    @property
    def path(self):
        return self._path

    @path.setter
    def path(self, path):
        if path is not None and not os.path.isdir(path):
            self.module.fail_json(msg=f"Path {path} is not a directory")
        self._path = path

    def _exec(self, args, run_in_check_mode=False, check_rc=True) -> Tuple[str, str]:
        if not self.module.check_mode or (self.module.check_mode and run_in_check_mode):
            cmd = self.executable + args
            rc, out, err = self.module.run_command(cmd, check_rc=check_rc)
            return out, err
        return "", ""

    def _get_cargo_home(self) -> Path:
        return Path(os.environ.get("CARGO_HOME") or (Path.home() / ".cargo"))

    def get_installed(self) -> packages_type:
        if not os.path.exists(self._crates_json):
            return {}

        with open(self._crates_json) as crates_f:
            crates = json.load(crates_f)["installs"]

        # see https://doc.rust-lang.org/cargo/reference/pkgid-spec.html
        pkgspec_pattern = re.compile(
            r"(?P<name>[^ ]+) +(?P<version>[^ ]+) +\((?P<kind>[^+]+)\+(?P<url>[^)]+)\)"
        )

        installed = {}

        for pkgid, meta in crates.items():
            match = pkgspec_pattern.search(pkgid)

            if not match:
                self.module.fail_json(msg=f"unexpected format: '{pkgid}'")

            pkg_parts = typing.cast(re.Match[str], match).groupdict()

            # only return packages which are specified in the module invocation
            if pkg_parts["name"] not in self.name:
                continue

            pkg_url = pkg_parts.get("url") and urlparse(pkg_parts.get("url"))

            if pkg_parts.get("kind") == "path" and pkg_url and pkg_url.path:
                pkg_parts["directory"] = pkg_url.path

            if not isinstance(meta, dict):
                self.module.fail_json(
                    msg=f"unexpected metadata in {self._crates_json}: '{meta}'"
                )

            no_default_features = meta.pop("no_default_features", False)
            meta["default_features"] = not no_default_features

            bin_stats = {}

            for bin in meta.get("bins", []):
                bin_path = self._cargo_home / "bin" / bin

                if not os.path.isfile(bin_path):
                    continue

                stats = os.stat(bin_path)
                stat_obj = {
                    k: getattr(stats, k) for k in dir(stats) if k.startswith("st_")
                }
                bin_stats[bin] = stat_obj

            if bin_stats:
                meta["bin_stats"] = bin_stats

            pkg_parts.update(meta)

            installed[pkg_parts["name"]] = pkg_parts

        return installed

    def install(self, packages: Optional[Collection[str]] = None):
        cmd = ["install"]
        cmd.extend(packages or self.name)

        if self.locked:
            cmd.append("--locked")

        if self.path:
            cmd.append("--root")
            cmd.append(self.path)

        if self.version:
            cmd.append("--version")
            cmd.append(self.version)

        if self.directory:
            cmd.append("--path")
            cmd.append(self.directory)

        if self.features:
            cmd += ["--features", ",".join(self.features)]
        return self._exec(cmd)

    def get_latest(self, package: package_type, cache: bool = True) -> package_type:
        if cache and package["name"] in self._latest:
            return self._latest[typing.cast(str, package["name"])]

        if self.directory:
            latest = self.get_source_directory_version(package)
        else:
            latest = self.get_latest_published_version(package)

        if cache:
            self._latest[typing.cast(str, package["name"])] = latest

        return latest

    def get_latest_published_version(self, package: package_type) -> package_type:
        cmd = ["search", package["name"], "--limit", "1"]
        data, _err = self._exec(cmd, run_in_check_mode=True, check_rc=True)
        match = re.search(r'"(.+)"', data)

        if not match:
            self.module.fail_json(
                msg=f"No published version for package '{package['name']}' found"
            )

        return package | {"version": typing.cast(re.Match, match).group(1)}

    def get_source_directory_version(
        self, installed_package: package_type
    ) -> package_type:
        cmd = [
            "metadata",
            "--format-version",
            "1",
            "--no-deps",
            "--manifest-path",
            os.path.join(self.directory, "Cargo.toml"),
        ]

        data, _err = self._exec(cmd, True, False)
        manifest = json.loads(data)

        directory_package = next(
            (
                pkg
                for pkg in manifest["packages"]
                if pkg["name"] == installed_package["name"]
            ),
            None,
        )

        if not directory_package:
            all_directory_packages = [x["name"] for x in manifest["packages"]]
            self.module.fail_json(
                msg=f"Package {installed_package['name']} not defined in source, found: {all_directory_packages}"
            )

        return installed_package | {
            "version": typing.cast(package_type, directory_package)["version"],
            "directory": self.directory,
        }

    def uninstall(self, packages=None):
        cmd = ["uninstall"]
        cmd.extend(packages or self.name)
        return self._exec(cmd)


def _diff_installed_packages(
    before: packages_type, after: packages_type
) -> list[dict[str, Optional[package_type]]]:
    union = []

    for package in list(before.values()) + list(after.values()):
        if package not in union:
            union.append(package)

    sym_diff = set()

    for package in union:
        name = typing.cast(str, package["name"])
        before_pkg = before.get(name)
        after_pkg = after.get(name)

        if before_pkg and after_pkg and before_pkg == after_pkg:
            continue

        if before_pkg:
            sym_diff.add(before_pkg["name"])

        if after_pkg:
            sym_diff.add(after_pkg["name"])

    return [
        {
            "before": before.get(typing.cast(str, package)),
            "after": after.get(typing.cast(str, package)),
        }
        for package in sym_diff
    ]


def main():
    arg_spec = dict(
        executable=dict(type="path"),
        name=dict(required=True, type="list", elements="str"),
        path=dict(type="path"),
        state=dict(default="present", choices=["present", "absent", "latest"]),
        version=dict(type="str"),
        locked=dict(default=False, type="bool"),
        directory=dict(type="path"),
        features=dict(default=[], type="list", elements="str"),
    )
    module = AnsibleModule(argument_spec=arg_spec, supports_check_mode=True)
    names = module.params["name"]
    state = module.params["state"]
    version = module.params["version"]
    directory = module.params["directory"]
    diff = []

    if module.params.get("bin") and len(names) > 1:
        module.fail_json(msg="Cannot install multiple crates with 'bin'")

    if not names:
        module.fail_json(msg="Package name must be specified")

    if directory is not None and not os.path.isdir(directory):
        module.fail_json(msg="Source directory does not exist")

    # Set LANG env since we parse stdout
    module.run_command_environ_update = dict(LANG="C", LC_ALL="C", LC_MESSAGES="C", LC_CTYPE="C")

    cargo = Cargo(module, **module.params)
    out, err = None, None

    installed_packages = cargo.get_installed()
    new_installed_packages = installed_packages.copy()

    if state == "present":
        to_install = [
            package_name
            for package_name in names
            if (package_name not in installed_packages)
            or (version and version != installed_packages[package_name]["version"])
        ]

        if to_install:
            out, err = cargo.install(to_install)

            package_params = {
                field: module.params.get(field)
                for field in [
                    "version",
                    "features",
                    "default_features",
                ]
                if module.params.get(field)
            }

            if module.check_mode:
                for package_name in to_install:
                    # if it is not installed and we don't specify a version,
                    # we can fetch the latest version to provide a fuller diff
                    if package_name not in installed_packages and not version:
                        after_package = package_params | cargo.get_latest(
                            {"name": package_name}
                        )
                    else:
                        before_package = installed_packages.get(package_name)
                        after_package = before_package | package_params

                    new_installed_packages[package_name] = after_package
            else:
                new_installed_packages = cargo.get_installed()

    elif state == "latest":
        if module.check_mode:
            for package_name in names:
                installed = installed_packages.get(package_name)
                latest = cargo.get_latest(installed or {"name": package_name})
                new_installed_packages[package_name] = latest
        else:
            out, err = cargo.install(names)
            new_installed_packages = cargo.get_installed()
    elif state == "absent":
        to_uninstall = [
            package_name for package_name in names if package_name in installed_packages
        ]

        if to_uninstall:
            out, err = cargo.uninstall(to_uninstall)

            if module.check_mode:
                new_installed_packages = {
                    package_name: package
                    for package_name, package in installed_packages.items()
                    if package_name not in to_uninstall
                }
            else:
                new_installed_packages = cargo.get_installed()

    else:
        module.fail_json(msg=f"unknown state: {state}")

    diff = _diff_installed_packages(installed_packages, new_installed_packages)

    result = {
        "changed": bool(diff),
        "stdout": out,
        "stderr": err,
        "invocation": module.params,
        "installed": new_installed_packages,
    }

    if module._diff:
        # Remove `bin_stats` from the returned diff. They are too verbose
        # to be very useful, but needed to detect changes to the actual
        # installed binaries.
        for diff_obj in diff:
            diff_obj["before"].pop("bin_stats", None)
            diff_obj["after"].pop("bin_stats", None)

        result["diff"] = diff

    module.exit_json(**result)


if __name__ == "__main__":
    main()
