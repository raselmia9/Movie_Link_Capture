name: Capture Movies & Stream Links

on:
  workflow_dispatch:
    inputs:
      concurrency:
        description: "Concurrent browser tabs/pages"
        required: false
        default: "10"
      max_items:
        description: "Max new items per run (0 = all)"
        required: false
        default: "0"
  schedule:
    - cron: "0 3 * * *"

permissions:
  contents: write

jobs:
  capture:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install Playwright and Chromium
        run: |
          python -m pip install --upgrade pip
          pip install playwright
          playwright install --with-deps chromium

      - name: Run capture script
        env:
          SOURCE_JSON_URL: "https://raw.githubusercontent.com/raselmia9/Movie-Scraper/refs/heads/main/movies.json"
          OUTPUT_JSON_FILE: "output_movies.json"
          CONCURRENCY: ${{ inputs.concurrency || '10' }}
          MAX_ITEMS: ${{ inputs.max_items || '0' }}
          CHECK_STATUS_200: "true"
        run: |
          python capture_movies.py

      - name: Commit and push updated JSON
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add output_movies.json
          if ! git diff --staged --quiet; then
            git commit -m "Update output_movies.json"
            git push
          else
            echo "No changes to commit."
          fi
