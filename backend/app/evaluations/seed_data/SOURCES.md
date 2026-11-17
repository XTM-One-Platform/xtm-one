# Sources and licences of the built-in evaluation documents

Every document a built-in dataset attaches to its entries is listed here with
its origin and licence. A document may only be added if Filigran owns it or it
is published under a Creative Commons licence; record it in this table in the
same change.

No document file is committed: each one is declared as structured text in its
dataset's seed file and rendered at seed time by `app/evaluations/documents.py`
(the PDFs with the standard library, the scanned pages with Pillow). The text
below the "Source" column is therefore the whole of the document.

All names, organisations, products, people, telephone numbers, identifiers and
events in these documents are fictional. IP addresses are taken from the
documentation ranges of RFC 5737 (198.51.100.0/24, 203.0.113.0/24), domains
from the reserved example names of RFC 2606 (`example.com`, `example.net`,
`example.org`) and the telephone number from the range Ofcom reserves for
fiction (0808 157 0000 to 0808 157 0999). The hashes are random hexadecimal
strings, not the digest of any file.

| Dataset | File | Format | Source | Author | Licence |
| --- | --- | --- | --- | --- | --- |
| `ocr-benchmark` | `northwind-threat-advisory.pdf` | Text PDF | `seed_data/datasets/ocr-benchmark.json`, entry 1 | Filigran SAS, written for this benchmark | Same licence as this repository (Apache License 2.0, Copyright Filigran SAS) |
| `ocr-benchmark` | `copper-heron-indicator-annex.pdf` | Text PDF | `seed_data/datasets/ocr-benchmark.json`, entry 2 | Filigran SAS, written for this benchmark | Same licence as this repository (Apache License 2.0, Copyright Filigran SAS) |
| `ocr-benchmark` | `avis-de-securite-fr.pdf` | Text PDF | `seed_data/datasets/ocr-benchmark.json`, entry 3 | Filigran SAS, written for this benchmark | Same licence as this repository (Apache License 2.0, Copyright Filigran SAS) |
| `ocr-benchmark` | `incident-report-ir-2026-031.pdf` | Text PDF, two pages | `seed_data/datasets/ocr-benchmark.json`, entry 4 | Filigran SAS, written for this benchmark | Same licence as this repository (Apache License 2.0, Copyright Filigran SAS) |
| `ocr-benchmark` | `scanned-memo-phishing-wave.png` | Scanned page (PNG) | `seed_data/datasets/ocr-benchmark.json`, entry 5 | Filigran SAS, written for this benchmark | Same licence as this repository (Apache License 2.0, Copyright Filigran SAS) |
| `ocr-benchmark` | `scanned-patch-status.png` | Scanned page (PNG) | `seed_data/datasets/ocr-benchmark.json`, entry 6 | Filigran SAS, written for this benchmark | Same licence as this repository (Apache License 2.0, Copyright Filigran SAS) |

The scanned pages are drawn in Aileron Regular, the default font bundled with
Pillow, which is dedicated to the public domain (CC0 1.0). The PDFs reference
the standard Helvetica fonts by name and embed no font program.
