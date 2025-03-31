DOCUMENTATION = r"""
name: cargo_bins
short_description: read the currently installed binaries from CARGO_HOME
description:
- This lookup returns the contents from a file on the Ansible control node's file system.
options:
_terms:
  description: nothing
"""

import os
from pathlib import Path

from ansible.errors import AnsibleError
from ansible.plugins.lookup import LookupBase
from ansible.utils.display import Display


def _get_cargo_home() -> Path:
    return os.environ.get("CARGO_HOME") or Path.home() / ".cargo"


display = Display()


class LookupModule(LookupBase):
    def run(self, _terms, variables=None, **kwargs):
        self.set_options(var_options=variables, direct=kwargs)

        try:
            return self.cargo_bins()
        except Exception as err:
            raise AnsibleError("error finding cargo binaries", obj=kwargs, orig_exc=err)

    def cargo_bins(self) -> list[Path]:
        cargo_bin = _get_cargo_home() / "bin"

        if not cargo_bin.exists():
            return []

        return list(cargo_bin.iterdir())
