from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
import os, redis, random, time, re, threading, cv2, numpy as np, struct

app = FastAPI()

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

# ---- HTML snippets ----
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
def home():
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
def stream():
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
