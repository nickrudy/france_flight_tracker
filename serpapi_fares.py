#!/usr/bin/env python3
"""Daily Google Flights cross-check (via SerpApi) for the 2027 France trip.

Writes one row per airline/cabin/leg to the SerpApi_Prices tab and one summary
row per run to SerpApi_Runs. Never touches Prices, Combos, Alerts_Log or Settings.
Uses 8 SerpApi searches per run; refuses to run past MONTHLY_CAP searches/month.
"""
import json
import os
import sys
import datetime as dt
from zoneinfo import ZoneInfo

import requests
import gspread

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SHEET_ID = os.environ["SHEET_ID"]
SERPAPI_KEY = os.environ["SERPAPI_KEY"]
MONTHLY_CAP = int(os.environ.get("MONTHLY_CAP", "245"))  # free tier is 250
TRAVELERS = 2
BAGS_PER_PERSON = 1
OUT_DATE, RET_DATE = "2027-07-07", "2027-07-16"

PRICES_TAB, RUNS_TAB, EXPEDIA_TAB = "SerpApi_Prices", "SerpApi_Runs", "Prices"
PRICES_HEADER = [
    "Date_Key", "Time_CT", "Leg", "Route", "Mkt_Airline", "Flights", "Stops", "Cabin",
    "Depart", "Arrive", "Layover_Notes", "Google_Price_pp_USD", "Total_2_USD",
    "Bag1_Fee_pp_USD", "Bag_Adj_Total_2_USD", "Expedia_Bag_Adj_Latest_USD",
    "Diff_vs_Expedia_%", "Reference_Only", "Source", "Notes",
]
RUNS_HEADER = ["Date_Key", "Time_CT", "Searches_Used", "Rows_Written", "Errors"]

# Same 8 searches the Expedia check runs. travel_class: 1 economy, 2 premium economy.
# stops: 1 = nonstop only, 2 = one stop or fewer (SerpApi google_flights codes).
SEARCHES = [
    dict(leg="OUT", dep="ORD", arr="CDG", date=OUT_DATE, cabin="ECON", tc=1, stops_param=1, max_stops=0, airlines=["UA", "AF", "AA"]),
    dict(leg="OUT", dep="ORD", arr="CDG", date=OUT_DATE, cabin="PREM", tc=2, stops_param=1, max_stops=0, airlines=["UA", "AF", "AA"]),
] + [
    dict(leg="RET", dep="NCE", arr="ORD", date=RET_DATE, cabin=c, tc=tc, stops_param=2, max_stops=1, airlines=[a])
    for c, tc in (("ECON", 1), ("PREM", 2)) for a in ("UA", "AF", "BA")
]


def now_ct():
    return dt.datetime.now(ZoneInfo("America/Chicago"))


def open_sheet():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        gc = gspread.service_account_from_dict(json.loads(raw))
    else:
        gc = gspread.service_account(filename=os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    return gc.open_by_key(SHEET_ID)


def get_tab(sh, title, header):
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=2000, cols=len(header))
    if not ws.row_values(1):
        ws.update(range_name="A1", values=[header])
    return ws


def searches_used_this_month(runs_ws, yyyymm):
    total = 0
    for row in runs_ws.get_all_values()[1:]:
        try:
            if int(row[0]) // 100 == yyyymm:
                total += int(row[2])
        except (ValueError, IndexError):
            continue
    return total


def expedia_reference(sh):
    """Latest Expedia bag fee and bag-adjusted low per (leg, airline, cabin)."""
    try:
        rows = sh.worksheet(EXPEDIA_TAB).get("A2:V", value_render_option="UNFORMATTED_VALUE")
    except gspread.WorksheetNotFound:
        return {}
    best = {}  # key -> (date_key, bag_adj, bag_fee)
    for r in rows:
        r = r + [""] * (22 - len(r))
        try:
            date_key, leg, airline, cabin = int(r[0]), r[2], r[4], r[8]
            bag_fee, bag_adj = float(r[15] or 0), float(r[17])
        except (ValueError, TypeError):
            continue
        cabin = "PREM" if "PREM" in str(cabin).upper() else "ECON"
        key = (leg, airline, cabin)
        cur = best.get(key)
        if cur is None or date_key > cur[0] or (date_key == cur[0] and bag_adj < cur[1]):
            best[key] = (date_key, bag_adj, bag_fee)
    return {k: {"bag_adj": v[1], "bag_fee": v[2]} for k, v in best.items()}


def serp_search(s):
    params = dict(
        engine="google_flights", departure_id=s["dep"], arrival_id=s["arr"],
        outbound_date=s["date"], type=2, travel_class=s["tc"], adults=1,
        stops=s["stops_param"], include_airlines=",".join(s["airlines"]),
        currency="USD", hl="en", gl="us", sort_by=2, deep_search="true",
        api_key=SERPAPI_KEY,
    )
    resp = requests.get("https://serpapi.com/search.json", params=params, timeout=120)
    if not resp.ok:  # don't re-raise: requests' message would include the URL and the API key
        raise RuntimeError(f"HTTP {resp.status_code} from SerpApi")
    data = resp.json()
    if os.environ.get("DEBUG_DUMP"):  # save the raw response next to the script for troubleshooting (*.json is git-ignored)
        name = f'debug_{s["leg"]}_{s["cabin"]}_{"-".join(s["airlines"])}.json'
        with open(name, "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in data.items() if k != "search_parameters"}, fh, indent=2)
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data


def cheapest_per_airline(data, s):
    """Cheapest qualifying itinerary per marketing airline (first segment's code)."""
    out = {}
    for it in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        segs, price = it.get("flights") or [], it.get("price")
        if not segs or price is None:
            continue
        carrier = str(segs[0].get("flight_number", "")).split(" ")[0]
        if carrier not in s["airlines"] or len(segs) - 1 > s["max_stops"]:
            continue
        if carrier not in out or price < out[carrier]["price"]:
            lay = "; ".join(f'{l.get("id", l.get("name", "?"))} {l.get("duration", "?")}m'
                            for l in it.get("layovers") or [])
            out[carrier] = dict(
                price=float(price),
                flights="+".join(str(x.get("flight_number", "")).replace(" ", "") for x in segs),
                stops=len(segs) - 1,
                depart=segs[0]["departure_airport"]["time"],
                arrive=segs[-1]["arrival_airport"]["time"],
                layover=lay,
            )
    return out


def main():
    t = now_ct()
    date_key, time_ct = int(t.strftime("%Y%m%d")), t.strftime("%H:%M")
    sh = open_sheet()
    prices_ws = get_tab(sh, PRICES_TAB, PRICES_HEADER)
    runs_ws = get_tab(sh, RUNS_TAB, RUNS_HEADER)

    used = searches_used_this_month(runs_ws, int(t.strftime("%Y%m")))
    if used + len(SEARCHES) > MONTHLY_CAP:
        msg = f"Skipped: {used} searches used this month, cap {MONTHLY_CAP}"
        print(msg)
        runs_ws.append_row([date_key, time_ct, 0, 0, msg])
        return 0

    ref = expedia_reference(sh)
    rows, errors, searches_made = [], [], 0
    for s in SEARCHES:
        label = f'{s["leg"]} {s["cabin"]} {"/".join(s["airlines"])}'
        try:
            data = serp_search(s)
            searches_made += 1  # only count searches SerpApi actually answered
            found = cheapest_per_airline(data, s)
        except Exception as e:  # keep going; record the failure
            errors.append(f"{label}: {e}")
            if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                errors.append("Stopping early: check SERPAPI_KEY")
                break
            continue
        if not found:
            errors.append(f"{label}: no qualifying itinerary")
        for carrier, f in found.items():
            r = ref.get((s["leg"], carrier, s["cabin"]))
            total_2 = f["price"] * TRAVELERS
            fee = r["bag_fee"] if r else 0.0
            adj = total_2 + TRAVELERS * BAGS_PER_PERSON * fee
            exp_adj = r["bag_adj"] if r else ""
            diff = round((adj / exp_adj - 1) * 100, 1) if r and exp_adj else ""
            note = ("Bag fee reused from latest Expedia row; Google price is the cheapest fare, tier not shown"
                    if r else "No Expedia row to take bag fee from; bag fee set to 0")
            rows.append([
                date_key, time_ct, s["leg"], f'{s["dep"]}-{s["arr"]}', carrier, f["flights"],
                f["stops"], s["cabin"], f["depart"], f["arrive"], f["layover"], f["price"],
                total_2, fee, adj, exp_adj, diff, "Y" if carrier == "AA" else "N",
                "SerpApi Google Flights", note,
            ])

    if rows:
        prices_ws.append_rows(rows, value_input_option="USER_ENTERED")
    runs_ws.append_row([date_key, time_ct, searches_made, len(rows), " | ".join(errors)])
    print(f"{len(rows)} rows written, {searches_made} searches used, {len(errors)} issues")
    for e in errors:
        print("  -", e)
    return 1 if searches_made and not rows else 0  # fail the workflow only if nothing usable came back


if __name__ == "__main__":
    sys.exit(main())
