# Third-Party Notices

## Dungeon System

Copyright © 2026 Киселюк Павел Геннадьевич. Dungeon System is licensed under the MIT License; see [LICENSE](LICENSE).

## Runtime

The runtime uses the Python standard library supplied by the user's Python interpreter. This repository does not vendor or redistribute CPython or standard-library binaries. Python distributions retain their own notices and license terms; see the [Python license documentation](https://docs.python.org/3/license.html) (including the Python-2.0 license identifier for CPython).

The default STDIO runtime remains standard-library-only. The opt-in experimental
HTTP coordinator additionally requires `aiohttp==3.14.5`, declared in
[requirements-http.txt](tools/codex-plugin/dungeon-system/requirements-http.txt).
Its distribution declares `Apache-2.0 AND MIT`; separately installed transitive
packages retain their own licenses. The tested inventory and license-metadata
boundary are recorded in the [HTTP validation report](docs/integrations/dungeon-shared-http-validation.md).
Neither aiohttp nor its installed dependencies are vendored or relicensed here.

## Development and validation dependencies

These direct dependencies are used by tests or publication-validation tooling, not by the Dungeon System runtime. They are listed in [requirements-dev.txt](requirements-dev.txt), and their package distributions are not vendored or shipped here.

| Package | Version | Purpose | License / upstream notice |
| --- | --- | --- | --- |
| jsonschema | 4.26.0 | Test dependency | MIT; [upstream COPYING](https://github.com/python-jsonschema/jsonschema/blob/main/COPYING) |
| PyYAML | 6.0.3 | Public skill-validation development dependency | MIT; [upstream LICENSE](https://github.com/yaml/pyyaml/blob/main/LICENSE) |

Installing development requirements may resolve transitive packages. Their own license terms and notices continue to apply. The resolved transitive inventory is a separate release-metadata check; none of those distributions is vendored by this repository.

The local verification environment resolved these additional distributions:

| Package | Version observed | Distribution license metadata |
| --- | --- | --- |
| attrs | 26.1.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| referencing | 0.37.0 | MIT |
| rpds-py | 2026.9.1 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |

These are observations, not a lockfile or a claim to relicense their code.
Installers retain the distributions' own licenses. No dependency binaries,
package caches or installed source trees are part of this source publication.

## External integrations

Rust_Makepad_Toolkit is an external execution/tooling integration. Its source, runtime, container images, and other distributions are not included in this repository, and the project does not claim endorsement by its maintainers.

Scion is an optional external Apache-2.0 capability, pinned for the recorded
runtime checks to CarterT27/scion commit
`7d445a0744e30f4dcb36a794b0ce42648157e1b9`. Dungeon supplies its own adapter and
disposable test helpers; it does not vendor or redistribute upstream Scion
source, schemas, binaries or server packages under MIT. See the
[Scion integration guide](docs/integrations/scion.md) for upstream attribution
and the separately installed runtime boundary.

The bounded source inventory found no vendored third-party library code; the repository's `libs/` directory contains only its README.

This notice is an informational source inventory, not a legal guarantee or a substitute for confirming ownership and the notices attached to separately installed packages.
