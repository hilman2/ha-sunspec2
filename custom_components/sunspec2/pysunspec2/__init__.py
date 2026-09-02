"""pysunspec2, embedded and maintained here as a fork.

Origin: https://github.com/sunspec/pysunspec2, tag v1.3.6, commit
03aaa7f85f2dedaa28a9b4a6110d21f99b1886ff. The model definitions under
``models/json`` come from ``sunspec/models``; the commit they match is
recorded in ``models/UPSTREAM``, and the "Model definitions" workflow
brings them forward every second month. Both are Apache 2.0, see the
LICENSE files next to this one and next to the models; a few source files
carry SunSpec's older MIT header, which stays with them.

What is embedded: the SunSpec device and model layer (``device``, ``mdef``,
``mb``, ``smdx``), the SunSpec Modbus client layer (``modbus``) and the
file client the test suite uses (``file``). Left out: the spreadsheet and
Excel tooling, the TLS test fixtures, the pymodbus-based test server and,
since v2026.10.0, upstream's own socket and serial clients.

Changes against upstream:

* Imports between the modules are relative, so the package works from
  inside the integration. Every touched file says so in its first line.
* A repeating group whose count has to be inferred from the model length
  is measured by its full instance length, nested groups included, so
  model 705 scans (sunspec/pysunspec2#120, v2026.9.1).
* The transport is ``modbus_connection``, Home Assistant's asyncio Modbus
  library, and the client layer awaits it: points, groups, models and
  devices have ``async_read``, ``async_write`` and ``async_scan`` in
  place of upstream's sync methods, and ``modbus/unit_device.py`` is the
  device over a ``ModbusUnit``. ``modbus/modbus.py`` keeps only the
  request limits, the function codes and the exception classes; the
  ``ModbusClientTCP`` and ``ModbusClientRTU`` that lived there, with the
  transaction id check, the forced function code 16 and the
  ``ModbusClientConnectionClosed`` signal this fork had added to them
  (v0.31.0 to v2026.9.1), went with it. The library does the same jobs:
  it matches replies by transaction id, writes one register with
  function code 16 unless asked otherwise, and reports a dropped link as
  a connection error, which the unit device raises as
  ``ModbusClientConnectionClosed`` (v2026.10.0).

Why a fork rather than the pip package: the integration needs the model
layer to await an asyncio transport, and upstream accepts changes slowly
and only after a signed CLA. Owning the code lets the change land where
it belongs.
"""

VERSION = "1.3.6"
