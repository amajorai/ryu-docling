# Fixed positive Docling PDFs

These synthetic one-page documents contain no customer data. They were generated
for the local conversion proof and retained unchanged; runtime tests do not need
their original ReportLab/Pillow generation dependencies.

- `layout.pdf`: text heading, description and a ruled two-column Region/Total
  table with row EU/12. SHA256:
  `a6f5083ea7b43909a711d5da00a0a37a7e98b364f28d8c87897b25ecd61538fb`.
- `scanned.pdf`: an image-only page containing Ryu Document Parsing, Docling
  scanned local conversion proof, and Region EU Total 12. SHA256:
  `5861392c388d6877de15d20dbd8fa0e85e6d56efb9a45f2f707cfd176e4a3d56`.

The positive HTTP suite verifies these bytes before submitting them to the real
sidecar. Do not replace either fixture with mocked extracted text.
