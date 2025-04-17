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
      - When this is omitted, currently installed crates are returned.
    type: list
    elements: str
  path:
    description: The base path where to install the Rust packages. Cargo automatically appends V(/bin). In other words, V(/usr/local)
      becomes V(/usr/local/bin).
    type: path
  features:
    description: Features to install. If O(default_features=false) is given,
      then these are the only features chosen; otherwise, they are in addition
      to the crate's default features.
    type: list
    elements: str
    default: []
    version_added: 11.0.0
  default_features:
    description: Whether the crate's default features should be installed. If
      set to V(false), only features from O(features) will be installed.
    type: bool
    default: true
    version_added: 11.0.0
  bin:
    description: Install only the specified binary
    type: str
    required: false
    version_added: 10.7.0
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
    description:
  git:
    description:
      - Crate from a git repository.
    type: dict
    version_added: 10.7.0
    options:
      url:
        description:
          - URL of the git repository.
        type: str
        required: true
      tag:
        description:
          - Tag of the git repository.
        type: str
        required: false
      branch:
        description:
          - Branch of the git repository.
        type: str
        required: false
      rev:
        description:
          - Revision (specific commit hash) of the git repository.
        type: str
        required: false
  argv:
    description:
      - Custom list of arguments to pass to C(cargo).
      - Useful for any options not explicitly supported by this module.
      - When this is used, no assumptions can be made about whether the package
        would need to be reinstalled, so changes are always assumed.
    type: list
    elements: str
    required: false
    version_added: 9.1.0
requirements:
  - cargo installed
"""

EXAMPLES = r"""
- name: Collect installed crates
  community.general.cargo:
  register: cargo_installed

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

- name: Install "uv" Rust package from git repo
  community.general.cargo:
    name: uv
    state: latest
    git: https://github.com/astral-sh/uv
    tag: 0.6.11

- name: Install "starship" Rust package with only the given features
  community.general.cargo:
    name: starship
    state: latest
    default_features: false
    features:
      - notify
      - gix-max-perf

- name: Install package with custom CLI args
  community.general.cargo:
    name: mypackage
    state: latest
    argv:
      - --registry
      - myregistry
      - --target-dir
      - /some/path
"""

import json
import os
import re
import sys
import typing
from pathlib import Path
from typing import Collection, Optional, Tuple, Union
from urllib.parse import parse_qs, urlparse

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.common.text.converters import to_text


# runtime compatibility with Python < 3.8
if sys.version_info >= (3, 9):
    package_type = dict[str, Union[str, dict[str, str]]]
    packages_type = dict[str, package_type]
else:
    package_type = dict
    packages_type = dict


class Cargo:
    def __init__(self, module: AnsibleModule, **kwargs):
        self.module = module
        self._installed: packages_type = {}
        self._latest: packages_type = {}
        self._cargo_home = self._get_cargo_home()
        self._crates_json = self._cargo_home / ".crates2.json"

        self.executable = [kwargs["executable"] or module.get_bin_path("cargo", True)]
        self.names = kwargs["name"]
        self.path = kwargs["path"]
        self.bin = kwargs["bin"]
        self.state = kwargs["state"]
        self.version = kwargs["version"]
        self.locked = kwargs["locked"]
        self.directory = kwargs["directory"]
        self.features = kwargs["features"]
        self.default_features = kwargs["default_features"]
        self.git: dict[str, str] = kwargs["git"]

        self.argv = kwargs["argv"]

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
        pkgspec_pattern = re.compile(r"(?P<name>[^ ]+) +(?P<version>[^ ]+) +\((?P<kind>[^+]+)\+(?P<url>[^)]+)\)")

        installed = {}

        for pkgid, meta in crates.items():
            match = pkgspec_pattern.search(pkgid)

            if not match:
                self.module.fail_json(msg=f"unexpected format: '{pkgid}'")

            pkg_parts = match.groupdict()

            # only return packages which are specified in the module invocation
            if self.names and pkg_parts["name"] not in self.names:
                continue

            pkg_url = pkg_parts.get("url") and urlparse(pkg_parts.get("url"))

            if pkg_parts.get("kind") == "path" and pkg_url and pkg_url.path:
                pkg_parts["directory"] = pkg_url.path

            elif pkg_parts.get("kind") == "git":
                git_meta: dict[str, str] = {}

                pkg_url_query = (
                    pkg_url
                    and pkg_url.query
                    and typing.cast(dict[str, list[str]], parse_qs(pkg_url.query))
                )

                # this populates `tag`
                if pkg_url_query and isinstance(pkg_url_query, dict):
                    git_meta.update(typing.cast(dict[str, str], pkg_url_query))

                if pkg_url and pkg_url.fragment:
                    # despite the documented format in the above package ID spec,
                    # for git packages, the actual cargo implementation uses a
                    # commit hash in the URL fragment
                    #
                    # see https://github.com/rust-lang/cargo/blob/c5f58e97c995652a870e0007501b602245a0bdff/src/cargo/core/source_id.rs#L714
                    git_meta["rev"] = to_text(pkg_url.fragment)

                if not git_meta:
                    self.module.fail_json(
                        msg=f"unexpected git package identifier: {pkg_url}"
                    )

                pkg_parts["git"] = git_meta

            if not isinstance(meta, dict):
                self.module.fail_json(msg=f"unexpected metadata in {self._crates_json}: '{meta}'")

            no_default_features = meta.pop("no_default_features", False)
            meta["default_features"] = not no_default_features

            bin_stats = {}

            for bin in meta.get("bins", []):
                bin_path = self._cargo_home / "bin" / bin

                if not os.path.isfile(bin_path):
                    continue

                stats = os.stat(bin_path)
                stat_obj = {k: getattr(stats, k) for k in dir(stats) if k.startswith("st_")}
                bin_stats[bin] = stat_obj

            meta["bin_stats"] = bin_stats

            pkg_parts.update(meta)

            installed[pkg_parts["name"]] = pkg_parts

        return installed

    def install(self, packages: Optional[Collection[str]] = None):
        cmd = ["install"]
        cmd.extend(packages or self.names)

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

        if not self.default_features:
            cmd.append("--no-default-features")

        if self.git:
            if (packages and len(packages) > 1) or len(self.names) > 1:
                self.module.fail_json(
                    msg="Cannot do multiple git installs at a time. Please specify only one package."
                )

            cmd.extend(["--git", self.git["url"]])

            if self.git.get("tag"):
                cmd.extend(["--tag", self.git["tag"]])

            if self.git.get("branch"):
                cmd.extend(["--branch", self.git["branch"]])

            if self.git.get("rev"):
                cmd.extend(["--rev", self.git["rev"]])

        if self.bin:
            cmd.extend(["--bin", self.bin])

        if self.argv:
            cmd.extend(self.argv)

        return self._exec(cmd)

    def get_latest(self, package: package_type, cache: bool = True) -> package_type:
        if cache and package["name"] in self._latest:
            return self._latest[typing.cast(str, package["name"])]

        if self.directory:
            latest = self.get_source_directory_version(package)
        elif self.git:
            if self.git.get("rev"):
                latest = package | {"rev": self.git["rev"]}
            else:
                latest = self.get_latest_git_oid(package)
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
            self.module.fail_json(msg=f"No published version for package '{package['name']}' found")

        return package | {"version": typing.cast(re.Match, match).group(1)}

    def get_source_directory_version(self, installed_package: package_type) -> package_type:
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
            (pkg for pkg in manifest["packages"] if pkg["name"] == installed_package["name"]),
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

    def get_latest_git_oid(self, package: package_type) -> package_type:
        cmd = [
            "git",
            "ls-remote",
            self.git["url"],
        ]

        if self.git.get("tags"):
            ref = f"refs/tags/{self.git.get('tags')}"
        elif self.git.get("branch"):
            ref = f"refs/heads/{self.git.get('branch')}"

        cmd.append(ref)

        _, out, _ = self.module.run_command(cmd, check_rc=True)
        out = out.strip()

        if not out:
            self.module.fail_json(
                msg=f"remote {self.git['url']} does not have ref: {ref}"
            )

        out_parts = out.strip().split("\t")

        if len(out_parts) != 2:
            self.module.fail_json(
                msg=f"got unexpected output from git ls-remote: {out}"
            )

        latest_rev = out_parts[0]
        return package | {
            "git": (
                {key: val for key, val in self.git.items() if val} | {"rev": latest_rev}
            )
        }

    def uninstall(self, packages=None):
        cmd = ["uninstall"]
        cmd.extend(packages or self.names)
        return self._exec(cmd)


def _diff_installed_packages(before: packages_type, after: packages_type) -> list[dict[str, Optional[package_type]]]:
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
        name=dict(required=False, type="list", elements="str"),
        path=dict(type="path"),
        bin=dict(type="str"),
        state=dict(default="present", choices=["present", "absent", "latest"]),
        version=dict(type="str"),
        locked=dict(default=False, type="bool"),
        directory=dict(type="path"),
        features=dict(default=[], type="list", elements="str"),
        default_features=dict(default=True, type="bool"),
        git=dict(
            type="dict",
            options=dict(
                url=dict(required=True, type="str"),
                tag=dict(type="str"),
                branch=dict(type="str"),
                rev=dict(type="str"),
            ),
        ),
        argv=dict(type="list", elements="str"),
    )

    module = AnsibleModule(
        argument_spec=arg_spec,
        mutually_exclusive=[
            ("version", "git", "directory"),
        ],
        supports_check_mode=True,
    )

    names = module.params["name"]
    state = module.params["state"]
    version = module.params["version"]
    directory = module.params["directory"]
    features = module.params["features"]
    default_features = module.params["default_features"]
    git = module.params["git"]
    argv = module.params["argv"]
  
    diff = []

    cargo = Cargo(module, **module.params)
    installed_packages = cargo.get_installed()

    if not names:
        result = {
            "changed": False,
            "invocation": module.params,
            "installed": installed_packages,
        }

        module.exit_json(**result)

    if module.params.get("bin") and len(names) > 1:
        module.fail_json(msg="Cannot install multiple crates with 'bin'")

    if directory is not None and not os.path.isdir(directory):
        module.fail_json(msg="Source directory does not exist")

    # Set LANG env since we parse stdout
    module.run_command_environ_update = dict(LANG="C", LC_ALL="C", LC_MESSAGES="C", LC_CTYPE="C")

    out, err = None, None
    new_installed_packages = installed_packages.copy()

    if state == "present":
        # for state == present, we only want to ensure that the input git
        # parameters are true; but when we fetch the installed package data, it
        # includes everything, including tag and rev together, for example. so
        # we are only interested in the git metadata that is relevant to what
        # the user specified
        git_fields = set(git.keys()) if git else {}
        git_installed_fields = {
            package_name: (
                {
                    key: val
                    for key, val in installed_packages[package_name]
                    .get("git", {})
                    .items()
                    if package_name in installed_packages and key in git_fields
                }
            )
            for package_name in names
        }

        to_install = [
            package_name
            for package_name in names
            if (package_name not in installed_packages)
            or (version and version != installed_packages[package_name]["version"])
            or (git and git != git_installed_fields.get(package_name))
            or (features and features != installed_packages[package_name]["features"])
            or (
                default_features
                and default_features
                != installed_packages[package_name]["default_features"]
            )
            or argv
        ]

        if to_install:
            out, err = cargo.install(to_install)

            package_params = {
                field: module.params.get(field)
                for field in [
                    "version",
                    "features",
                    "default_features",
                    "git",
                ]
                if module.params.get(field)
            }

            if module.check_mode:
                for package_name in to_install:
                    # if it is not installed and we don't specify a version,
                    # we can fetch the latest version to provide a fuller diff
                    if package_name not in installed_packages and not version:
                        after_package = package_params | cargo.get_latest({"name": package_name})
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
        to_uninstall = [package_name for package_name in names if package_name in installed_packages]

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
    changed = bool(diff) or (state != "absent" and argv)

    result = {
        "changed": changed,
        "stdout": out,
        "stderr": err,
        "invocation": module.params,
        "installed": new_installed_packages,
    }

    if argv:
        result["warnings"] = result.get("warnings", []) + [
            "changes are always assumed when using argv"
        ]

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
