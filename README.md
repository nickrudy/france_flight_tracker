# France fare tracker - SerpApi cross-check

Runs once a day on GitHub Actions. Makes 8 SerpApi Google Flights searches (same criteria as the Expedia check),
and writes results to two new tabs in the Sheet: `SerpApi_Prices` (one row per airline/cabin/leg) and
`SerpApi_Runs` (searches used, for the monthly cap). Existing tabs are never modified.

## Setup
1. Share the Google Sheet with the service account's `client_email` (Editor).
2. Locally: copy `.env.example` to `.env`, fill it in, save the key as `service_account.json`.
   `pip install -r requirements.txt && python serpapi_fares.py`
3. GitHub repo -> Settings -> Secrets and variables -> Actions:
   - Secrets: `SERPAPI_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the whole JSON file)
   - Variable: `SHEET_ID`
4. Actions tab -> "SerpApi fare check" -> Run workflow to test.

## Notes
- Budget: 8 searches/day = ~240/month; the script stops at MONTHLY_CAP (245) using the SerpApi_Runs tab.
- Google price is searched for 1 adult and doubled for 2 travelers.
- Google doesn't show fare tier or reliable bag fees, so bag fees are reused from the latest Expedia row for the same airline/cabin.
