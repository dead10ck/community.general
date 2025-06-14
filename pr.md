This PR makes several improvements to the `cargo` module.

Currently, to fetch current state, this module runs `cargo install --list` and
parses the text output. This is changed to instead read the `.crates2.json`
file, which is both more reliable and contains more metadata about the installed
crates.

Several additional options are added which correspond to cargo CLI arguments:

* `bin`
* `profile`
* `features`
* `default_features`
* `git`
* `argv`

Additionally, support for diffing is added.
