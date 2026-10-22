# dental monitoring vi5

## Configuration

In PowerShell, create `.env` in the project root by running
`Copy-Item .env.example .env`. Set a unique `SECRET_KEY`, a strong
`ADMIN_PASSWORD`, and the database connection values; do not leave the example
placeholders or required database values blank. The app loads this file
automatically when started with Python or Docker Compose and exits during
startup if any required value is missing.

When deployed behind one trusted reverse proxy, the app uses Werkzeug
`ProxyFix` to honor one value each for the forwarded client address, scheme,
host, port, and URL prefix. Configure the proxy to overwrite these forwarded
headers and restrict direct access to the Gunicorn port; otherwise clients
could supply spoofed forwarded values.

For local development, install dependencies with `pip install -r requirements.txt`
and start with `python run.py`. To run in Docker, use `docker compose up --build`.
The container uses Gunicorn and stores timestamps in the `Asia/Manila` timezone.
Gunicorn runs two workers, and MySQL connections use five-second connect, read,
and write timeouts by default. Scan write transactions allow up to 15 seconds
for the database response, and admin reads allow up to 20 seconds. If a database
operation times out, API requests return HTTP 503 with a retry message rather
than waiting for the Gunicorn worker timeout. Production-sized admin query
performance still needs validation against the deployment database. VI5 scanning reads station and
unit data from `dentalmonitoring.mainboard_*` and writes inspection records to
`dentalmonitoring.mainboard_vi5`.

Before recording VI5, scan the Data Matrix from `mainboard_lasermarking` and
`mainboard_labelling`, then scan the unit serial number. The app extracts the
customer SN from GS1 application identifier `(21)`. The extracted customer SNs
and the complete Data Matrix values must match exactly; the full value is saved
in `mainboard_vi5.product_sn`. Both station records must map that SN to the
scanned serial, and the unit must have passed `progtest`, `soldering`, `vi1`,
`assembly1`, `vi4`, `assembly2` through `assembly6`, `camcalibration`,
`lasermarking1`, and `labelling` in `dentalmonitoring.mainboard_main`. PASS
records are saved to
`dentalmonitoring.mainboard_vi5`. Failed inspections are recorded with serial
suffixes `_1` through `_3` and matching `test_rep` values; a fourth FAIL
attempt is not accepted.