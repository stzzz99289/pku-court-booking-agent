# Debugging workspace

This directory is for local, disposable investigation artifacts. Its contents
are ignored by Git except for this guide.

- `post-submit/` — timestamped HTML, JSON, and screenshots from booking failures
- `smoke/` — screenshots and captures from local smoke tests
- `probes/` — focused diagnostic logs and payloads
- `studies/` — downloaded site assets and UI/reverse-engineering experiments
- `samples/` — manually supplied reference images

Production order-proof screenshots do **not** belong here. The webapp stores
them privately under `data/order_proofs/`, which is also ignored by Git and is
maintained automatically.
