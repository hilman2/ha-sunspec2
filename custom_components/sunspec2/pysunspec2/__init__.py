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

Changes against upstream:

* Imports between the modules are relative, so the package works from
  inside the integration. Every touched file says so in its first line.
* ``ModbusClientTCP`` numbers its requests and checks the transaction id
  and the function code of every response. A frame with another id is
  dropped as the late answer to an earlier request; upstream sent id 0
  in every frame and took whatever came back (v0.31.0). The drop is
  logged as a warning on this module's logger (v0.31.2).
* A repeating group whose count has to be inferred from the model length
  is measured by its full instance length, nested groups included, so
  model 705 scans (sunspec/pysunspec2#120, v2026.9.1).
* A peer that closes the TCP connection raises
  ``ModbusClientConnectionClosed`` and the dead socket is dropped;
  upstream reported it as a response timeout (v2026.9.1).

Why a fork rather than the pip package: the integration works around the
transport layer in several places (a Modbus TCP transaction id that is
never checked, a connect timeout that stays on the socket, no way to force
multi-register writes) and upstream accepts fixes slowly and only after a
signed CLA. Owning the code lets the fixes land where the bug is.
"""

VERSION = "1.3.6"
