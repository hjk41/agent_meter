# Third-party notices

Agent Meter uses Python, Qt for Python (PySide6 Essentials / Shiboken6), Requests,
urllib3, certifi, charset-normalizer, idna, PySocks and websocket-client, packaged with PyInstaller.

- Python: Python Software Foundation License — https://docs.python.org/3/license.html
- Qt / PySide6 / Shiboken6: LGPL v3 / GPL v3 and third-party licenses — https://doc.qt.io/qtforpython-6/licenses.html
- Requests: Apache 2.0 — https://github.com/psf/requests/blob/main/LICENSE
- urllib3: MIT — https://github.com/urllib3/urllib3/blob/main/LICENSE.txt
- certifi: MPL 2.0 — https://github.com/certifi/python-certifi/blob/master/LICENSE
- charset-normalizer: MIT — https://github.com/jawah/charset_normalizer/blob/master/LICENSE
- idna: BSD 3-Clause — https://github.com/kjd/idna/blob/master/LICENSE.md
- PySocks: BSD — https://github.com/Anorov/PySocks/blob/master/LICENSE
- websocket-client: Apache 2.0 — https://github.com/websocket-client/websocket-client/blob/master/LICENSE
- cryptography: Apache 2.0 / BSD — https://github.com/pyca/cryptography/blob/main/LICENSE
- PyInstaller: GPL with bootloader exception — https://pyinstaller.org/en/stable/license.html

The application source is in this project. The bundled Qt libraries are dynamically
loaded. Third-party source and license notices remain available from the projects above.

Claude browser OAuth protocol research reference:
https://github.com/character-s/claude-usage-bar-windows (BSD-2-Clause).
Agent Meter implements its own PKCE, Windows DPAPI credential storage, proxy routing,
and quota collector; it does not bundle that project's application.

Doubao desktop quota protocol research reference:
https://github.com/Fullstop000/doubao-cli (MIT).
Agent Meter implements an independent Windows launcher, loopback CDP connection,
and sanitized quota reader. It does not bundle doubao-cli.

Doubao independent HTTP quota and local-session protocol research reference:
https://github.com/OxygenAILab/AskDoubao (MIT).
Agent Meter implements a narrowly scoped Windows DPAPI/AES-GCM session reader
and membership-only quota request. It does not bundle AskDoubao or its generation tools.
