from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
import os, redis, random, time, re, threading, cv2, numpy as np, struct, asyncio, base64
from starlette.middleware.sessions import SessionMiddleware
from User_Authentication import load_env, authenticate_user_sql
from typing import Optional, Union
from urllib.parse import quote_plus
from ParkingLot_Database_Utils import pool, get_connection_pool
from datetime import datetime, date, timedelta, timezone
import zoneinfo
import httpx
import csv, io
from fastapi.responses import StreamingResponse
from zipfile import ZipFile, ZIP_DEFLATED

# ----- Configuration ----------------------------
PID         = 12345678                                  # demo PID for stream
LIST_KEY    = f"raw_buffer_{PID}_Video1"
ZSET_KEY    = f"res_buffer_{PID}_Video1"
POLL_MS     = 200                                        # ms
MAX_WAIT_SEC= 30
# -----------------------------------------------

load_env("./.env")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET_KEY", "super-secret"))

redis_url = os.getenv("REDIS_URL", "redis://redis-stack:6379/0")
r_txt = redis.Redis.from_url(redis_url)
r_bin = redis.Redis.from_url(redis_url, decode_responses=False)

latest_pair: tuple[np.ndarray, np.ndarray] | None = None
pending: dict[float, tuple[np.ndarray, float]] = {}

templates = Jinja2Templates(directory = "templates")

pool = get_connection_pool()


def export_suffix(days: str) -> str:
    """Map query 'days' to a readable filename suffix."""
    if days == "all":
        return "all_time"
    if days == "365":
        return "12months"
    if days == "30":
        return "30days"
    if days == "7":
        return "7days"
    try:
        n = int(days)
        return f"{n}days"
    except:
        return days.replace("-", "_")


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, error: Optional[str] = None):
    modified_html = LOGIN_HTML
    
    if error:
        # Replace the hidden class and insert the error message (correcting quotes)
        modified_html = modified_html.replace(
            "<div id='error-message' class='alert alert-danger d-none' role='alert'></div>",
            f"<div id='error-message' class='alert alert-danger d-block' role='alert'>{error}</div>"
        )
    return modified_html

@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if authenticate_user_sql(username, password):
        request.session["authenticated"] = True
        return RedirectResponse(url="/stream", status_code=303)
    else:
        # Pass error message via query parameter
        error_message = "Invalid username or password."
        return RedirectResponse(url=f"/login?error={quote_plus(error_message)}", status_code=303)

@app.get("/logout")
def logout(request: Request):
    request.session.pop("authenticated", None)
    return RedirectResponse(url="/login", status_code=303)

# @app.get('/stream', response_class=HTMLResponse)
# def stream(request: Request):
#     if not request.session.get("authenticated"):
#         return RedirectResponse(url="/login", status_code=303)
#     return HTMLResponse(STREAM_HTML)

@app.get('/stream', response_class=HTMLResponse)
def stream(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login", status_code=303)
    # old: return HTMLResponse(STREAM_HTML)
    return templates.TemplateResponse("stream.html", {"request": request})

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login", status_code=303)
    # old: return STREAM_HTML
    return templates.TemplateResponse("stream.html", {"request": request})

@app.get('/frames')
async def frames(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login", status_code=303)
    try:
        if latest_pair is None:
            return Response(status_code=204)
        raw_img, res_img = latest_pair
    except asyncio.TimeoutError:
        return Response(status_code=204)
    b1, b2 = img_to_b64(raw_img), img_to_b64(res_img)
    if not b1 or not b2:
        return Response(status_code=204)
    return JSONResponse({'raw': b1, 'res': b2}, headers={'Cache-Control':'no-store'})

api_cache = {}
CACHE_DURATION_SECONDS = 3600
@app.get('/api/get-stall-numbers')
# Use FastAPI's type hints for automatic validation
async def get_all_stall_numbers(lot_id: int = 1):
    current_time = time.time()
    
    
    if lot_id in api_cache:
        cache_entry = api_cache[lot_id]
        is_cache_valid = (current_time - cache_entry["last_fetched"]) < CACHE_DURATION_SECONDS
        if "stall_ids" in cache_entry and is_cache_valid:
            print(f"Serving stall IDs for lot {lot_id} from cache.")
            return cache_entry["stall_ids"]

    try:
        conn = pool.getconn()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            SELECT stall_id, stall_number from public.stalls 
            WHERE lot_id = %s 
            ORDER BY CAST(stall_number AS INTEGER);
            """, (lot_id,))
        rows = cur.fetchall()
        
        
        stalls = [{"id": row[0], "number": row[1]} for row in rows]
    
        api_cache[lot_id] = {
            "stall_ids": stalls,
            "last_fetched": current_time
        }
        return stalls
        
    except Exception as e:
        print(f"SQL command execution error: {e}")
        # --- FIX 3: Raise a proper HTTP Exception on error ---
        raise HTTPException(status_code=500, detail="Database query failed")
        
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)

@app.get("/api/availability/today")
async def get_availability_today(lot_id: int):
    """API endpoint to get the number of available spots throughout today, in 30-min intervals."""
    conn = None
    cur = None
    local_tz = zoneinfo.ZoneInfo("America/Edmonton")
    local_now = datetime.now(local_tz)
    start_of_day_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_day_utc = start_of_day_local.astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)
    end_of_day_utc = now_utc

    sql = """
    WITH bounds AS (
        SELECT
            %s::timestamptz AS start_utc,
            %s::timestamptz AS now_utc
    ),
    end_bin AS (
        SELECT
            date_trunc('hour', now_utc)
            + floor(extract(minute FROM now_utc)/30) * interval '30 minutes' AS end_bin_utc
        FROM bounds
    ),
    grid AS (
        SELECT gs AS ts_utc
        FROM bounds, end_bin,
            generate_series(
                (SELECT start_utc   FROM bounds),
                (SELECT end_bin_utc FROM end_bin),      -- inclusive current bin
                interval '30 minutes'
            ) gs
    ),
    snap AS (
        SELECT
            timestamp AT TIME ZONE 'UTC' AS ts_utc,
            array_length(available_stalls, 1) AS avail
        FROM public.availabilitysnapshots, bounds
        WHERE lot_id = %s
            AND timestamp >= (SELECT start_utc FROM bounds)
            AND timestamp <  (SELECT now_utc   FROM bounds)  -- don't look into the future
    ),
    binned AS (
        SELECT
            date_trunc('hour', ts_utc)
            + floor(extract(minute FROM ts_utc)/30) * interval '30 minutes' AS bin_utc,
            avail,
            ts_utc,
            row_number() OVER (
            PARTITION BY date_trunc('hour', ts_utc)
                        + floor(extract(minute FROM ts_utc)/30) * interval '30 minutes'
            ORDER BY ts_utc DESC
            ) AS rn
        FROM snap
    )
    SELECT g.ts_utc, b.avail
    FROM grid g
    LEFT JOIN binned b ON b.bin_utc = g.ts_utc AND b.rn = 1
    ORDER BY g.ts_utc;
    """

    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute(sql, (start_of_day_utc, now_utc, lot_id))
        rows = cur.fetchall()
        chart_data = {
            "labels": [ts.astimezone(local_tz).strftime("%I:%M %p") for ts, _ in rows],
            # Replace None with 0 so empty bins show as 0 available spots
            "data": [(avail if avail is not None else 0) for _, avail in rows]
        }
        return chart_data
    except Exception as e:
        print(f"SQL command execution error: {e}")
        return JSONResponse({"error": "Database query failed"}, status_code=500)
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)

    

@app.get("/api/stall_durations")
async def get_stall_durations(lot_id: int):
    """API endpoint to get total parking duration for all stalls in a specific lot."""
    conn = None
    cur = None
    
    #UTC time: today
    # utc_now = datetime.now(timezone.utc)
    # start_of_day = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    #local time zone: today
    local_tz = zoneinfo.ZoneInfo("America/Edmonton")
    local_now = datetime.now(local_tz)
    start_of_day_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_day_utc = start_of_day_local.astimezone(timezone.utc)
    end_of_day_utc = start_of_day_utc + timedelta(days=1)
    
    cap_utc = min(datetime.now(timezone.utc), end_of_day_utc)

    # test_date = date(2025, 7, 30) # For example, yesterday
    # start_of_day = datetime.combine(test_date, datetime.min.time())
    
    # end_of_day = start_of_day + timedelta(days=1)
    
    sql = """
        SELECT 
            s.stall_id,
            s.stall_number,
            COALESCE(
                SUM(
                    EXTRACT(
                        EPOCH FROM (
                            COALESCE(LEAST(ps.exit_timestamp, %s), %s) - ps.entry_timestamp
                        )
                    )
                ) / 3600.0,
            0) AS total_duration
        FROM public.stalls s
        LEFT JOIN public.parkingsessions ps 
            ON s.stall_id = ps.stall_id
            AND ps.entry_timestamp >= %s
            AND ps.entry_timestamp < %s
            -- no exit filter: we include ongoing sessions
        WHERE s.lot_id = %s
        GROUP BY s.stall_id, s.stall_number
        ORDER BY CAST(s.stall_number AS INTEGER);
    """
    
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute(sql, (cap_utc, cap_utc, start_of_day_utc, end_of_day_utc, lot_id))
        rows = cur.fetchall()

        stalls_data = [
            {"id": row[0], "number": row[1], "duration": round(float(row[2]), 2)}
            for row in rows
        ]
        return stalls_data
    except Exception as e:
        print(f"Database error: {e}")
        raise HTTPException(status_code=500, detail="Could not retrieve data")
    finally:
        if cur: 
            cur.close()
        if conn: 
            pool.putconn(conn)


# === CSV export for today's availability timeline =========================
@app.get("/api/availability_today_csv")
async def availability_today_csv(lot_id: int):
    """
    Returns a CSV with two columns:
      timestamp (HH:MM AM/PM, local time) | available_spots
    """
    chart = await get_availability_today(lot_id)           # reuse existing query

    def csv_rows():
        buf = io.StringIO(); w = csv.writer(buf)
        w.writerow(["timestamp", "available_spots"]); yield buf.getvalue()
        buf.seek(0); buf.truncate(0)

        for ts, spots in zip(chart["labels"], chart["data"]):
            w.writerow([ts, spots]); yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    today = datetime.now(zoneinfo.ZoneInfo("America/Edmonton")).strftime("%Y-%m-%d")
    headers = {
        "Content-Disposition": f'attachment; filename="availability_lot{lot_id}_{today}.csv"'
    }
    return StreamingResponse(csv_rows(), media_type="text/csv", headers=headers)


@app.get("/api/stall_durations_csv")
async def get_stall_durations_csv(lot_id: int):
    stalls_data = await get_stall_durations(lot_id)

    def csv_rows():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["stall_number", "total_duration_h"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)

        for s in stalls_data:
            writer.writerow([s["number"], s["duration"]])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    headers = {
        "Content-Disposition": f'attachment; filename="stall_durations_lot{lot_id}.csv"'
    }
    return StreamingResponse(csv_rows(), media_type="text/csv", headers=headers)



@app.get("/api/stall_history_csv")
async def get_stall_history_csv(lot_id: int, stall_id: int, days: str = "7"):
    if days not in ("7", "30", "365", "all"):
        raise HTTPException(400, "days must be 7, 30, 365 or 'all'")

    conn, cur = None, None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute("SELECT stall_number FROM public.stalls WHERE stall_id = %s", (stall_id,))
        row = cur.fetchone()
        stall_number = row[0] if row else stall_id
    finally:
        if cur: cur.close()
        if conn: pool.putconn(conn)

    hist = await get_stall_history(stall_id, days)

    def csv_rows():
        buf = io.StringIO(); csvw = csv.writer(buf)
        csvw.writerow(["date", "occupied_hours"]); yield buf.getvalue()
        buf.seek(0); buf.truncate(0)
        for lbl, hrs in zip(hist["labels"], hist["data"]):
            csvw.writerow([lbl, hrs]); yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    suffix = export_suffix(days)
    headers = {
        "Content-Disposition":
        f'attachment; filename="lot{lot_id}_stall_{stall_number}_{suffix}.csv"'
    }

    return StreamingResponse(csv_rows(), media_type="text/csv", headers=headers)


@app.get("/api/stall_histories_zip")
async def stall_histories_zip(lot_id: int, days: str = "7"):
    if days not in ("7", "30", "365", "all"):
        raise HTTPException(400, "days must be 7, 30, 365 or 'all'")

    conn = cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute("""
            SELECT stall_id, stall_number
            FROM public.stalls
            WHERE lot_id = %s
            ORDER BY CAST(stall_number AS INTEGER)
        """, (lot_id,))
        stalls = cur.fetchall()  # [(stall_id, stall_number), ...]

        suffix = export_suffix(days)
        mem = io.BytesIO()
        with ZipFile(mem, "w", ZIP_DEFLATED) as z:
            for sid, snum in stalls:
                # reuse your existing endpoint logic
                hist = await get_stall_history(sid, days)

                # write one CSV per stall
                s_buf = io.StringIO()
                w = csv.writer(s_buf)
                w.writerow(["date", "occupied_hours"])
                for lbl, hrs in zip(hist["labels"], hist["data"]):
                    w.writerow([lbl, hrs])   # labels are already "Aug 12, 2025"
                z.writestr(f"lot{lot_id}_stall_{snum}_{suffix}.csv", s_buf.getvalue())

        mem.seek(0)
        headers = {"Content-Disposition": f'attachment; filename="lot{lot_id}_stall_histories_{suffix}.zip"'}
        return StreamingResponse(mem, media_type="application/zip", headers=headers)

    except Exception as e:
        print("stall_histories_zip error:", e)
        raise HTTPException(500, "Failed to build ZIP")
    finally:
        if cur: cur.close()
        if conn: pool.putconn(conn)



@app.get("/dashboard/lot/{lot_id}", response_class=HTMLResponse)
async def get_dashboard(request: Request, lot_id: int):
    """Renders the dashboard by calling the stall durations API internally."""
    
    # 2. Construct the full URL to your own API endpoint
    api_url = f"{request.base_url}api/stall_durations?lot_id={lot_id}"
    
    try:
        # call the existing coroutine directly
        duration_data = await get_stall_durations(lot_id)

        # 4. Pass the fetched data to the Jinja2 template
        return templates.TemplateResponse("all_stalls_dashboard.html", {
            "request": request,
            "duration_data": duration_data,
            "lot_id": lot_id
        })

    except httpx.RequestError as e:
        print(f"HTTP request error: {e}")
        return HTMLResponse("<h1>Error: Could not connect to the API service.</h1>", status_code=503)
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return HTMLResponse("<h1>An unexpected error occurred.</h1>", status_code=500)



@app.get("/dashboard/stall/{stall_id}", response_class=HTMLResponse)
async def get_single_stall_dashboard(request: Request, stall_id: int):
    """Renders a detail page for a single stall using its unique ID."""
    
    # You can now use the stall_id to fetch specific data from the database
    stall_data = {
        "id": stall_id,        
    }

    return templates.TemplateResponse("single_stall_dashboard.html", {
        "request": request,
        "stall_data": stall_data
    })

@app.get("/api/stall-history")
async def get_stall_history(stall_id: int, days: str = "7"):
    """
    Daily occupied hours for a stall, correctly including today and sessions
    that cross midnight / are still open.
    """
    conn = cur = None

    # --- Parse days arg ---
    if days == "all":
        days_int = None
    else:
        try:
            days_int = int(days)
        except ValueError:
            raise HTTPException(400, "days must be 7, 30, 365 or 'all'")
        if days_int not in (7, 30, 365):
            raise HTTPException(400, "days must be 7, 30, 365 or 'all'")

    local_tz = zoneinfo.ZoneInfo("America/Edmonton")
    local_now = datetime.now(local_tz)
    # end bound = now (local) -> convert to UTC once for SQL
    end_utc = local_now.astimezone(timezone.utc)

    try:
        conn = pool.getconn()
        cur  = conn.cursor()

        # Determine start of range (local midnight) based on days/all
        if days_int is None:
            # all-time: start from the day of the earliest session (local)
            cur.execute("""
                SELECT MIN((entry_timestamp AT TIME ZONE %s)::date)
                FROM public.parkingsessions
                WHERE stall_id = %s
            """, ("America/Edmonton", stall_id))
            min_local_date = cur.fetchone()[0]
            if min_local_date is None:
                # no data at all -> return a single "today" zero
                return {
                    "labels": [local_now.date().strftime("%b %d, %Y")],
                    "data":   [0.0],
                    "kpi":    {"total":"0.0 hrs","avg":"0.0 hrs","busiest":"N/A"}
                }
            start_local_dt = datetime(min_local_date.year, min_local_date.month, min_local_date.day,
                                      tzinfo=local_tz)
        else:
            # last N days including today: start at local midnight N-1 days ago
            start_local_dt = (local_now.replace(hour=0, minute=0, second=0, microsecond=0)
                              - timedelta(days=days_int - 1))

        start_utc = start_local_dt.astimezone(timezone.utc)

        # SQL: build per-day grid (UTC based), intersect with sessions, sum overlaps
        sql = """
        WITH bounds AS (
            SELECT %s::timestamptz AS start_utc,
                   %s::timestamptz AS end_utc
        ),
        day_grid AS (
            SELECT generate_series(
                       (SELECT start_utc FROM bounds),
                       (SELECT end_utc   FROM bounds),
                       interval '1 day'
                   ) AS day_start_utc
        ),
        sess AS (
            SELECT
              ps.entry_timestamp                         AS entry_utc,
              COALESCE(ps.exit_timestamp,
                       (SELECT end_utc FROM bounds))      AS exit_utc
            FROM public.parkingsessions ps, bounds b
            WHERE ps.stall_id = %s
              -- session intersects the [start,end) window:
              AND ps.entry_timestamp <  b.end_utc
              AND COALESCE(ps.exit_timestamp, b.end_utc) > b.start_utc
        ),
        perday AS (
            SELECT
                g.day_start_utc,
                CASE
                WHEN s.entry_utc IS NULL THEN interval '0 second'
                ELSE GREATEST(
                        interval '0 second',
                        LEAST(s.exit_utc, g.day_start_utc + interval '1 day')
                        - GREATEST(s.entry_utc, g.day_start_utc)
                    )
                END AS overlap
            FROM day_grid g
            LEFT JOIN sess s
                ON s.entry_utc < g.day_start_utc + interval '1 day'
            AND s.exit_utc  > g.day_start_utc
            )
        SELECT
          ((day_start_utc AT TIME ZONE 'America/Edmonton')::date) AS local_date,
          COALESCE(SUM(EXTRACT(EPOCH FROM overlap)),0)/3600.0     AS hours
        FROM perday
        GROUP BY local_date
        ORDER BY local_date;
        """

        cur.execute(sql, (start_utc, end_utc, stall_id))
        rows = cur.fetchall()  # [(date, hours), ...]

        # Build a full continuous local date range (ensures today appears)
        dates_to_hours = {d: float(h) for d, h in rows}
        last_local_date = local_now.date()

        # inclusive range
        all_dates = []
        d = start_local_dt.date()
        while d <= last_local_date:
            all_dates.append(d)
            d += timedelta(days=1)

        labels = [d.strftime("%b %d, %Y") for d in all_dates]
        data   = [round(dates_to_hours.get(d, 0.0), 2) for d in all_dates]

        # KPIs
        period_len = max(1, len(data))
        total_duration = round(sum(data), 1)
        avg_duration   = round(total_duration / period_len, 1)
        if data:
            busiest_val = max(data)
            busiest_idx = data.index(busiest_val) if busiest_val > 0 else -1
            busiest_lbl = labels[busiest_idx] if busiest_idx >= 0 else "N/A"
        else:
            busiest_lbl = "N/A"

        return {
            "labels": labels,
            "data":   data,
            "kpi": {
                "total":   f"{total_duration} hrs",
                "avg":     f"{avg_duration} hrs",
                "busiest": busiest_lbl
            }
        }

    except Exception as e:
        print("get_stall_history error:", e)
        raise HTTPException(status_code=500, detail="Could not retrieve data")
    finally:
        if cur: cur.close()
        if conn: pool.putconn(conn)


# ---- HTML snippets ----
LOGIN_HTML = """<!DOCTYPE html><html><head>
<meta charset='utf-8'><title>Login</title>
<link href='https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css' rel='stylesheet'>
</head><body class='bg-light d-flex justify-content-center py-5'>
<div class='card shadow-sm w-100' style='max-width:400px;'>
 <div class='card-body'>
  <h5 class='mb-4'>Login</h5>
  <div id='error-message' class='alert alert-danger d-none' role='alert'></div>
  <form method='post' action='/login'>
   <div class='mb-3'>
    <label class='form-label'>Username</label>
    <input type='text' class='form-control' name='username' required>
   </div>
   <div class='mb-3'>
    <label class='form-label'>Password</label>
    <input type='password' class='form-control' name='password' required>
   </div>
   <button type='submit' class='btn btn-primary w-100'>Login</button>
  </form>
 </div>
</div>
</body></html>"""


# ----- Stream helper functions -----------------------------------
def deserialize_frame(buf: bytes):
    off = 0
    slen = struct.unpack_from('<i', buf, off)[0]; off += 4
    fid = float(buf[off:off+slen].decode()); off += slen
    mtype = struct.unpack_from('<i', buf, off)[0]; off += 4
    rows = struct.unpack_from('<i', buf, off)[0]; off += 4
    cols = struct.unpack_from('<i', buf, off)[0]; off += 4
    channels = ((mtype >> 3) & 0x3F) + 1
    size = rows*cols*channels
    img = np.frombuffer(buf[off:off+size], dtype=np.uint8).reshape((rows, cols, channels))
    if img.ndim == 3 and img.shape[2] == 2:
        img = cv2.cvtColor(img, cv2.COLOR_YUV2BGR_YUY2)
    return fid, img

def img_to_b64(img: np.ndarray):
    ok, enc = cv2.imencode('.jpg', img)
    if not ok:
        return None
    return 'data:image/jpeg;base64,' + base64.b64encode(enc.tobytes()).decode()


@app.get("/", include_in_schema=False)
def home_redirect():
    return RedirectResponse(url="/stream", status_code=307)

# ----- Background tasks -----------------------------------------------
async def list_consumer():
    loop = asyncio.get_running_loop()
    while True:
        _, raw = await loop.run_in_executor(None, lambda: r_bin.blpop(LIST_KEY, 0))
        fid, img = deserialize_frame(raw)
        pending[fid] = (img, loop.time())

async def zset_matcher():
    loop = asyncio.get_running_loop()
    while True:
        now = loop.time()
        for fid in list(pending.keys()):
            raw_img, ts = pending[fid]
            zdata = await loop.run_in_executor(None,
                     lambda fid=fid: r_bin.zrangebyscore(ZSET_KEY, fid, fid, 0, 1))
            if zdata:
                loop.run_in_executor(None, r_bin.zrem, ZSET_KEY, zdata[0])
                _, res_img = deserialize_frame(zdata[0])
                try:
                    global latest_pair
                    latest_pair = (raw_img, res_img)
                except asyncio.QueueFull:
                    pass
                del pending[fid]
            elif now - ts > MAX_WAIT_SEC:
                del pending[fid]
        await asyncio.sleep(POLL_MS/1000)

@app.on_event('startup')
async def startup_tasks():
    asyncio.create_task(list_consumer())
    asyncio.create_task(zset_matcher())
