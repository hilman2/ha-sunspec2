"""pysunspec2, embedded and maintained here as a fork.

Origin: https://github.com/sunspec/pysunspec2, tag v1.3.6, commit
03aaa7f85f2dedaa28a9b4a6110d21f99b1886ff. The model definitions under
``models/json`` come from ``sunspec/models``; the commit they match is
recorded in ``models/UPSTREAM``, and the "Model definitions" workflow
brings them forward every second month. Both are Apache 2.0, see the
LICENSE files next to this one and next to the models; a few source files
carry SunSpec's older MIT header, which stays with them.

What is embedded: the SunSpec device and model layer (``device``, ``mdef``,
``mb``, ``smdx``), the Modbus TCP and RTU clients (``modbus``) and the file
client the test suite uses (``file``). Left out: the spreadsheet and Excel
tooling, the TLS test fixtures and the pymodbus-based test server.

Changes against upstream, so far only the ones the move itself needed:

* Imports between the modules are relative, so the package works from
  inside the integration. Every touched file says so in its first line.

Why a fork rather than the pip package: the integration works around the
transport layer in several places (a Modbus TCP transaction id that is
never checked, a connect timeout that stays on the socket, no way to force
multi-register writes) and upstream accepts fixes slowly and only after a
signed CLA. Owning the code lets the fixes land where the bug is.
"""

VERSION = "1.3.6"
