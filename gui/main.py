from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
import os, redis, random, time, re, threading, cv2, numpy as np, struct
from starlette.middleware.sessions import SessionMiddleware
from User_Authentication import load_env, authenticate_user_sql
from typing import Optional
from urllib.parse import quote_plus

load_env("./.env")

app = FastAPI()

app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET_KEY", "super-secret"))

redis_url = os.getenv("REDIS_URL", "redis://redis-stack:6379/0")
r_txt = redis.Redis.from_url(redis_url)
r_bin = redis.Redis.from_url(redis_url, decode_responses=False)

shared_dir = os.getenv("FILES_VOLUME", "/data/shared")
os.makedirs(shared_dir, exist_ok=True)
config_path = os.path.join(shared_dir, "process_config.conf")
file_lock = threading.Lock()

def generate_uid():
    return int(time.time()*1000)%1_000_000 + random.randint(100000, 999999)

def sanitize_rtsp(url):
    return re.sub(r"rtsp://[^@]*@", "rtsp://@", url, count=1)




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
        return RedirectResponse(url="/", status_code=303)
    else:
        # Pass error message via query parameter
        error_message = "Invalid username or password."
        return RedirectResponse(url=f"/login?error={quote_plus(error_message)}", status_code=303)

@app.get("/logout")
def logout(request: Request):
    request.session.pop("authenticated", None)
    return RedirectResponse(url="/login", status_code=303)

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

FORM_HTML = """<!DOCTYPE html><html><head>
<meta charset='utf-8'><title>Add Video Source</title>
<link href='https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css' rel='stylesheet'>
</head><body class='bg-light d-flex justify-content-center py-5'>
<div class='card shadow-sm w-100' style='max-width:800px;'>
 <div class='card-body'>
  <h5 class='mb-4'>Add Video Source</h5>
  <form method='post' action='/add'>
   <div class='mb-4'>
    <label class='form-label'>Video Source</label>
    <input type='url' class='form-control' name='video_url' placeholder='rtsp://...' required>
   </div>
   <button type='submit' class='btn btn-primary px-4'>Add</button>
   <a href='/' class='btn btn-outline-secondary ms-2'>Cancel</a>
  </form>
  <a href='/logout' class='btn btn-danger mt-3'>Logout</a>
 </div>
</div>
</body></html>"""


STREAM_HTML = """<!DOCTYPE html><html><head>
<meta charset='utf-8'><title>Stream</title>
<link href='https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css' rel='stylesheet'>
<style>img{max-width:100%;height:auto;}</style>
</head><body class='bg-light d-flex flex-column align-items-center py-4'>
<h4>Live Stream</h4>
<img id='frame' class='border rounded shadow-sm'/>
<script>
const pid=new URLSearchParams(window.location.search).get('pid');
async function poll(){
  const res=await fetch('/frame?pid='+pid);
  if(res.status===200){
     const blob=await res.blob();
     if(blob.size>0){
       document.getElementById('frame').src=URL.createObjectURL(blob);
     }
  }
  setTimeout(poll,200);
}
poll();
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login", status_code=303)
    return FORM_HTML

@app.post("/add")
def add(video_url: str = Form(...)):
    pid = generate_uid()
    clean_url = sanitize_rtsp(video_url)
    line = f"{pid} TYPE=VIDEO;LABEL=Video1;ANALYSIS_TYPE=SMART_PARKING_BRENTWOOD;USERNAME=root;DATA={clean_url};LOCAL_SAVE_PATH=NONE;\n"
    with file_lock:
        with open(config_path, 'w') as f:
            f.write(line)
    r_txt.publish('video_sources', clean_url)
    return RedirectResponse(f"/stream?pid={pid}", status_code=303)

@app.get("/stream", response_class=HTMLResponse)
def stream(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login", status_code=303)
    return HTMLResponse(STREAM_HTML)

# -------- frame fetching with retry / skip logic ----------
MAX_RETRIES = 5
RETRY_DELAY_MS = 200  # client polls every 200 ms, so server logic is per poll

state = {}  # pid -> dict(index=int, retries=int)

def deserialize(data: bytes):
    off = 0
    strlen = struct.unpack_from('<i', data, off)[0]; off+=4
    s = data[off:off+strlen].decode(); off+=strlen
    mat_type = struct.unpack_from('<i', data, off)[0]; off+=4
    rows = struct.unpack_from('<i', data, off)[0]; off+=4
    cols = struct.unpack_from('<i', data, off)[0]; off+=4
    channels = {0:1, 16:2, 24:3, 32:4}.get(mat_type & 56, 1)
    img_size = rows*cols*channels
    img = np.frombuffer(data[off:off+img_size], dtype=np.uint8).reshape((rows, cols, channels))
    return s, img

@app.get("/frame")
def frame(pid: int):
    st = state.setdefault(pid, {'idx': 0, 'retry': 0})
    key = f"res_buffer_{pid}_Video1"
    idx = st['idx']
    # try fetch exact score
    data = r_bin.zrangebyscore(key, idx, idx, start=0, num=1)
    if not data:
        # if buffer empty, just wait without increasing retry
        if r_bin.zcard(key) == 0:
            return Response(status_code=204)
        # else element missing
        st['retry'] += 1
        if st['retry'] > MAX_RETRIES:
            # skip this index
            st['idx'] += 1
            st['retry'] = 0
        return Response(status_code=204)
    # have frame
    r_bin.zrem(key, data[0])
    st['idx'] += 1
    st['retry'] = 0
    try:
        _, img = deserialize(data[0])
        ok, jpeg = cv2.imencode('.jpg', img)
        if not ok:
            return Response(status_code=204)
        return Response(content=jpeg.tobytes(), media_type='image/jpeg')
    except Exception:
        return Response(status_code=204)
