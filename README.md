# Shh! - Campus Quiet Tracker

A Flask + SQLite web app for tracking how quiet or loud spots on campus are.

## Run it

```bash
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5000 in your browser (works fine on a phone if
you run it on a laptop and visit the laptop's local IP from your phone on
the same wifi, e.g. http://192.168.1.23:5000).

## How it works

- **Dashboard (`/`)**: every location on campus, sorted quietest-first, with
  a color-coded 1-10 badge (green = quiet, red = loud).
- **Rate a spot (`/location/<id>`)**: students who are physically there use
  the slider to submit a 1-10 rating. Ratings are timestamped and stored in
  `quiet_tracker.db` (created automatically on first run).
- **Live average**: a location's displayed level is the average of its
  ratings from the last 14 days (`RATING_WINDOW_DAYS` in `app.py`).
- **Prediction fallback**: if a location has no recent ratings, the app
  guesses a level by falling back through, in order:
  1. past ratings for that same spot around the same hour of day
  2. any past ratings for that spot, any time
  3. ratings from other locations of the same category (e.g. other
     "study" spaces) as a proxy
  4. a hand-set baseline default for that type of space
  The dashboard tags these guesses "predicted" so students know it's an
  estimate, not a live report.
- **JSON API (`/api/locations`)**: same data as the dashboard (including
  photo counts), machine readable, if you want to build a phone widget or
  Discord bot on top later.
- **Photos (`/location/<id>` page)**: students can snap or upload a photo of
  a spot right from that location's page, with an optional caption (e.g.
  "packed at lunch"). Photos are saved to the `uploads/` folder and tagged
  to that location, then shown in a gallery on the page so anyone can see
  what a "quiet" or "loud" rating actually looks like in person. On mobile
  browsers the file picker opens the camera directly.
  - Photos for one location: `GET /api/location/<id>/photos` (JSON: id, image
    URL, caption, timestamp) if you want to pull the data into another app.
  - Files are served back out at `/uploads/<filename>`.

## Customizing locations

Edit the `SEED_LOCATIONS` list near the top of `app.py` (name, category,
baseline noise guess) before the first run, or add rows directly to the
`locations` table in `quiet_tracker.db` afterward.
