#!/usr/bin/env python3
"""Concatenate the daily CSVs in data/ into one long panel.

Daily files stay immutable, which keeps the raw record auditable; the panel is
a derived artifact you can rebuild at any time.

    python build_panel.py              -> othoba_panel.csv
    python build_panel.py --wide       -> also othoba_panel_wide.csv (SKU x date)
"""
import glob
import sys

import pandas as pd

files = sorted(glob.glob("data/othoba_*.csv"))
if not files:
    sys.exit("No files in data/")

panel = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
panel = panel.drop_duplicates(subset=["Date", "Product ID"], keep="last")
panel.to_csv("othoba_panel.csv", index=False, encoding="utf-8-sig")

print(f"{len(files)} daily files -> {len(panel)} rows")
print(f"dates    : {panel['Date'].min()} to {panel['Date'].max()} "
      f"({panel['Date'].nunique()} days)")
print(f"products : {panel['Product ID'].nunique()}")
print("wrote othoba_panel.csv")

if "--wide" in sys.argv:
    # The wide SKU x date matrix, matching the Chaldal input layout
    wide = panel.pivot_table(index=["Product ID", "Name", "Amount"],
                             columns="Date", values="Price (BDT)", aggfunc="first")
    wide.to_csv("othoba_panel_wide.csv", encoding="utf-8-sig")
    print(f"wrote othoba_panel_wide.csv  ({wide.shape[0]} SKUs x {wide.shape[1]} dates)")
