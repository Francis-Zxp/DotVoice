# Dependency notices

These files retain the dependencies' own licenses. They do not replace the
project's GPL-3.0 license or relicense third-party components.

`DEPENDENCY-SOURCES.json` records each collected component, installed version,
upstream origin, notice path and SHA-256. All filesystem paths are relative.
`UPSTREAM-SOURCES.json` records the original URLs and hashes for notices absent
from their installed package. Cargo notices use the commit recorded by that
exact crate's `.cargo_vcs_info.json`; selectors identifies MPL 2.0 in its source,
so its full license text comes from Mozilla's official MPL 2.0 publication.

To reproduce after installing the locked build dependencies:

```powershell
.venv/Scripts/python.exe tools/collect_licenses.py
```

The collector is offline. It verifies the retained upstream texts against their
recorded hashes, and does not fetch or execute any upstream code. It includes
only installed npm production dependencies, plus Cargo's locked Windows normal
and build dependencies (no dev dependencies). Python notices include the frozen
application's dependencies, PyInstaller's distributed bootloader exception, and
the actual build interpreter's complete license and Windows redistribution
conditions. Existing manually maintained model, HID and protocol notices remain.

OpenSSL, Expat and zlib versions are read from the build interpreter and their
notice texts are from the corresponding upstream version tags. The distributed
libffi DLL and statically linked liblzma do not report a dependable version;
the inventory explicitly leaves those versions unreported and records their
binary hashes. For these two components, the retained upstream texts document
libffi's MIT terms and liblzma's historical public-domain/current 0BSD terms.
Those notice source tags are provenance of the texts, not a claim to have
identified the binary versions. This limitation must not be converted into an
exact-version attribution when regenerating the notices.
