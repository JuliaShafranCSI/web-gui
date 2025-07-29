from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
import os, redis, random, time, re, threading, cv2, numpy as np, struct, asyncio, base64
from starlette.middleware.sessions import SessionMiddleware
from User_Authentication import load_env, authenticate_user_sql
from typing import Optional
from urllib.parse import quote_plus
from ParkingLot_Database_Utils import pool, get_connection_pool
from datetime import datetime, date, timedelta
import httpx

# ----- Configuration ----------------------------
PID         = 12345678                                  # demo PID for stream
LIST_KEY    = f"raw_buffer_{PID}_Video1"
ZSET_KEY    = f"res_buffer_{PID}_Video1"
POLL_MS     = 200                                        # ms
MAX_WAIT_SEC= 30
# -----------------------------------------------

load_env("./.env")

app = FastAPI()

app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET_KEY", "super-secret"))

redis_url = os.getenv("REDIS_URL", "redis://redis-stack:6379/0")
r_txt = redis.Redis.from_url(redis_url)
r_bin = redis.Redis.from_url(redis_url, decode_responses=False)

latest_pair: tuple[np.ndarray, np.ndarray] | None = None
pending: dict[float, tuple[np.ndarray, float]] = {}

templates = Jinja2Templates(directory = "templates")


pool = get_connection_pool()

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

@app.get('/stream', response_class=HTMLResponse)
def stream(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login", status_code=303)
    return HTMLResponse(STREAM_HTML)

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
async def get_all_stall_numbers(lot_id=1):
    current_time = time.time()
    if lot_id in api_cache:
        cache_entry = api_cache[lot_id]
        is_cache_valid = (current_time - cache_entry["last_fetched"]) < CACHE_DURATION_SECONDS
    if is_cache_valid:
        print(f"Serving stall IDs for lot {lot_id} from cache.")
        return cache_entry["stall_ids"]
    try:
        conn  = pool.getconn()
        cur = conn.cursor()
        cur.execute(""" SELECT stall_id, stall_number from public.stalls 
                    WHERE lot_id = %s 
                    ORDER BY CAST(stall_number AS INTEGER);""", (lot_id))
        rows = cur.fetchall()
        conn.commit()
        stalls = [{"stall_id": row[0], "stall_number": row[1]} for row in rows]
    
        api_cache[lot_id]={
            "stall_ids": stalls,
            "last_fetched":current_time
        }
        return stalls
    except Exception as e:
        print(f"SQL command execution error: {e}")
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
    
    today = date.today()
    start_of_day = datetime.combine(today, datetime.min.time())
    end_of_day = start_of_day + timedelta(days=1)
    
    sql = """
        SELECT 
            s.stall_number,
            COALESCE(SUM(EXTRACT(EPOCH FROM (ps.exit_timestamp - ps.entry_timestamp))) / 3600.0, 0) as total_duration
        FROM public.stalls s
        LEFT JOIN public.parkingsessions ps ON s.stall_id = ps.stall_id
                                            AND ps.entry_timestamp >= %s 
                                            AND ps.entry_timestamp < %s
                                            AND ps.exit_timestamp IS NOT NULL
        WHERE s.lot_id = %s
        GROUP BY s.stall_id, s.stall_number
        ORDER BY CAST(s.stall_number AS INTEGER);
    """
    
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute(sql, (start_of_day, end_of_day, lot_id))
        rows = cur.fetchall()

        chart_data = {
            "labels": [row[0] for row in rows],
            "data": [round(float(row[1]), 2) for row in rows]
        }
        return chart_data
    except Exception as e:
        print(f"Database error: {e}")
        raise HTTPException(status_code=500, detail="Could not retrieve data")
    finally:
        if cur: 
            cur.close()
        if conn: 
            pool.putconn(conn)

@app.get("/dashboard/lot/{lot_id}", response_class=HTMLResponse)
async def get_dashboard(request: Request, lot_id: int):
    """Renders the dashboard by calling the stall durations API internally."""
    
    # 2. Construct the full URL to your own API endpoint
    api_url = f"{request.base_url}api/stall_durations?lot_id={lot_id}"
    
    try:
        # 3. Use httpx to make an async request to your API
        async with httpx.AsyncClient() as client:
            response = await client.get(api_url)
            response.raise_for_status() # Raise an exception for bad responses (4xx or 5xx)
            chart_data = response.json()

        # 4. Pass the fetched data to the Jinja2 template
        return templates.TemplateResponse("all_stalls_dashboard.html", {
            "request": request,
            "chart_data": chart_data,
            "lot_id": lot_id
        })

    except httpx.RequestError as e:
        print(f"HTTP request error: {e}")
        return HTMLResponse("<h1>Error: Could not connect to the API service.</h1>", status_code=503)
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return HTMLResponse("<h1>An unexpected error occurred.</h1>", status_code=500)








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


STREAM_HTML = """<!DOCTYPE html><html><head>
<meta charset='utf-8'><title>Live Stream</title>
<link href='https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css' rel='stylesheet'>
<style>
body{background:#f8f9fa;}
h4{margin-bottom:2.5rem;}
.wrap{display:flex;justify-content:space-evenly;padding:0 2vw;width:100%;}
img.frame{flex:1 1 50vw;max-width:850px;border:1px solid #ccc;visibility:hidden}
.logout-btn{position:absolute;top:2rem;right:2rem;z-index:10;}
</style>
</head><body class='d-flex flex-column align-items-center py-4'>
<a href='/logout' class='btn btn-danger logout-btn'>Logout</a>
<h4>Live Stream</h4>
<div class='wrap'>
  <img id='raw' class='frame'><img id='res' class='frame'>
</div>
<script>
const raw=document.getElementById('raw'), res=document.getElementById('res');
async function poll(){
  try{
    const resp=await fetch('/frames',{cache:'no-store'});
    if(resp.ok){
      const j=await resp.json();
      raw.src=j.raw; res.src=j.res;
      raw.style.visibility='visible'; res.style.visibility='visible';
    }
  }catch(_){}
  setTimeout(poll,200);
}
poll();
</script>
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


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login", status_code=303)
    return STREAM_HTML

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
