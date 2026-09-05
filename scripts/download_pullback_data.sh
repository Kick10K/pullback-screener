#!/bin/sh
set -eu

OUT_DIR="${1:-data/raw_yahoo}"
mkdir -p "$OUT_DIR"

download_one() {
  symbol="$1"
  safe_symbol=$(printf '%s' "$symbol" | tr '^=/' '___')
  url="https://query2.finance.yahoo.com/v8/finance/chart/${symbol}?period1=1420070400&period2=1767225600&interval=1d&events=div%2Csplits&includeAdjustedClose=true"
  if [ -s "$OUT_DIR/${safe_symbol}.json" ]; then
    exit 0
  fi
  n=0
  while [ "$n" -lt 4 ]; do
    if curl -A 'Mozilla/5.0' -L --fail --silent --show-error "$url" -o "$OUT_DIR/${safe_symbol}.json.tmp"; then
      mv "$OUT_DIR/${safe_symbol}.json.tmp" "$OUT_DIR/${safe_symbol}.json"
      exit 0
    fi
    n=$((n + 1))
    sleep 2
  done
  rm -f "$OUT_DIR/${safe_symbol}.json.tmp"
  printf 'FAILED %s\n' "$symbol" >&2
  exit 1
}

symbols_file="$OUT_DIR/.symbols.txt"
tail -n +2 config/pullback_universe.csv | cut -d, -f1 > "$symbols_file"
printf '%s\n' '^GSPC' '^KS11' '^KQ11' >> "$symbols_file"

batch_count=0
while IFS= read -r symbol; do
  download_one "$symbol" &
  batch_count=$((batch_count + 1))
  if [ "$batch_count" -ge 6 ]; then
    wait
    batch_count=0
  fi
done < "$symbols_file"
wait
