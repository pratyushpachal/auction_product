## Repository layout

Each Python component has its own folder containing its code and public imports. Scripts and tests also have one folder per file. Framework configuration and the run.py entry point retain their standard locations.

```text
src/kkr_scout/<component>/<component>.py
frontend/src/main/main.jsx
frontend/src/styles/styles.css
frontend/scripts/ui-check/ui-check.mjs
scripts/<script-name>/<script-name>.py
tests/<test-name>/<test-name>.py
notebooks/uploaded_sample_eda/uploaded_sample_eda.ipynb
data/raw/cricsheet/
design/
migrations/
.github/workflows/
```

Clone with `git clone https://github.com/pratyushpachal/auction_product.git`, enter `auction_product`, and follow the README setup commands. Virtual environments, node_modules, database files, build outputs, local logs and pasted chat instructions are excluded from Git.
