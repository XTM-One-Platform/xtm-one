# SPDX-License-Identifier: Apache-2.0

"""The one lock every use of PDFium in the process holds.

PDFium (``pypdfium2``) is not thread-safe: two threads of the parse executor
inside it at once abort the process. Every use - opening a document, reading
its page objects, rendering, closing - holds :data:`PDFIUM_LOCK`
(``builtin_tools.document_vision``, ``kb_figures``); work that needs no PDFium
(encoding a rendered image, parsing with ``pypdf``) runs outside it.
"""

import threading

PDFIUM_LOCK = threading.Lock()
